"""In-memory, independently identified DXLink quotes. No candles or volume API.

See docs/TASTYTRADE_QUOTES.md for primary sources, limits and source boundaries.
All provider errors are fixed codes: provider bodies/URLs may contain credentials.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from threading import RLock
from uuid import uuid4

from desk.risk_terms import EvidenceUnavailable, QuoteProvenance

UTC = timezone.utc
SOURCE = "tastytrade-dxlink"
# Fields requested in FEED_SETUP, per event type. No Candle, Summary or volume field.
FIELDS = {
    "Quote": ("eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize", "bidTime", "askTime"),
    "Trade": ("eventType", "eventSymbol", "price", "size", "time"),
    "Profile": ("eventType", "eventSymbol", "tradingStatus", "haltStartTime", "haltEndTime"),
}
# Fields a server-accepted map must contain before that type can be decoded. Trade
# size is optional (assumption E4): the risk bridge reads price and time only.
REQUIRED = {
    "Quote": FIELDS["Quote"],
    "Trade": ("eventType", "eventSymbol", "price", "time"),
    "Profile": ("eventType", "eventSymbol", "tradingStatus"),
}
CORE = ("Quote", "Trade")       # main FEED channel; an invalid map here stops the session
OPTIONAL = ("Profile",)         # own FEED channel; failures withhold Profile only (E3)
STATUSES = {"ACTIVE", "HALTED", "UNDEFINED"}  # dxFeed TradingStatus
PRECISION = {"Trade": "milliseconds; time of the last regular-trading-hours trade",
             "Quote": "seconds by default; time of the last bid/ask change, not a freshness stamp"}
CLOCK_NOTE = ("Provider source time is later than local receipt. The local clock may lag the provider "
              "clock; no tolerance is applied, so the observation is ineligible. Measure the host offset "
              "with `sntp time.apple.com` (read-only, no clock-setting flags).")


def session_label(at: datetime) -> str:
    from desk.calendar import clock, session, trading_day
    stamp = clock(at)
    if not trading_day(stamp.date()):
        return "CLOSED"
    opened, closed = session(stamp.date())
    return "RTH" if opened <= stamp < closed else "OFF_HOURS"


class QuoteUnavailable(EvidenceUnavailable):
    """Only internally generated, credential-free codes are exposed."""


class FutureSourceTime(QuoteUnavailable):
    def __init__(self, lead: timedelta):
        super().__init__("FUTURE_SOURCE_TIME")
        self.lead = lead


def number(value, *, positive=False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise QuoteUnavailable("INVALID_NUMBER")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise QuoteUnavailable("INVALID_NUMBER") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise QuoteUnavailable("INVALID_NUMBER")
    return result


def optional_number(value) -> Decimal | None:
    """None when the provider marks the value unknown (missing or NaN); corrupt values raise.

    Zero is a value (for example no bid), never silently unavailable.
    """
    if value is None:
        return None
    if isinstance(value, (str, float, Decimal)) and not isinstance(value, bool):
        try:
            if Decimal(str(value)).is_nan():
                return None
        except InvalidOperation:
            raise QuoteUnavailable("INVALID_NUMBER") from None
    return number(value)


def aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise QuoteUnavailable("INVALID_CLOCK")
    return value.astimezone(UTC)


def event_time(value, received: datetime) -> datetime:
    # DXFeed Quote.bidTime/askTime and Trade.time are epoch milliseconds.
    if type(value) is not int or value <= 0:
        raise QuoteUnavailable("SOURCE_TIME_UNAVAILABLE")
    try:
        result = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
    except OverflowError:
        raise QuoteUnavailable("INVALID_SOURCE_TIME") from None
    if result > aware(received):
        # Never clamped to receipt and never accepted (assumption E6).
        raise FutureSourceTime(result - aware(received))
    return result


def optional_time(value, received: datetime) -> datetime | None:
    """A side time of 0/missing is unavailable; other malformed values are corrupt."""
    if value is None or (type(value) is int and value == 0):
        return None
    return event_time(value, received)


def canonical(value: str, kind: str) -> str:
    if not isinstance(value, str):
        raise QuoteUnavailable("IDENTITY_INVALID")
    if kind == "Equity":
        # Explicit provider share-class syntax, not arbitrary punctuation folding.
        value = value.strip().upper().replace("/", ".")
        if not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:\.[A-Z])?", value):
            raise QuoteUnavailable("IDENTITY_INVALID")
        return value
    if kind == "Equity Option":
        from desk.instruments import canonical_symbol
        try:
            return canonical_symbol(value)
        except ValueError:
            raise QuoteUnavailable("IDENTITY_INVALID") from None
    raise QuoteUnavailable("INSTRUMENT_TYPE_UNSUPPORTED")


def equity_provider_symbol(value: str) -> str:
    """tastytrade's Equity symbol for a desk symbol: a share class uses a slash (BRK/B).

    The one provider-boundary conversion for Equity instrument and option-chain lookups
    (Sourced: tastytrade instruments-and-symbology). Never applied to option symbols.
    """
    return canonical(value, "Equity").replace(".", "/")


# Supporting tastytrade metadata kept for a reviewed mapping (not part of the digest).
CAPTURE_FIELDS = ("symbol", "instrument-type", "streamer-symbol", "cusip", "id", "description",
                  "listed-market", "is-etf", "instrument-sub-type", "active")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    provider_symbol: str
    streamer_symbol: str
    instrument_id: str
    kind: str
    identity_digest: str
    received_at: datetime
    underlying: str | None = None
    expiry: date | None = None
    right: str | None = None
    strike: Decimal | None = None
    metadata: tuple = field(default=(), compare=False)
    # Equity classification (child 3, R2): only a real JSON boolean from `is-etf`;
    # None means unavailable, never "common stock". Part of the identity digest.
    is_etf: bool | None = None

    def capture(self) -> dict:
        """Metadata a reviewer compares with Webull's; no credentials, no prices."""
        return {"desk_symbol": self.symbol, "kind": self.kind, "instrument_id": self.instrument_id,
                "is_etf": self.is_etf,
                "streamer_symbol": self.streamer_symbol, "provider_symbol": self.provider_symbol,
                "identity_digest": self.identity_digest, "received_at": self.received_at.isoformat(),
                "provider_fields": dict(self.metadata)}


def instrument(requested: str, row: dict, received: datetime) -> Instrument:
    """Use actual instrument response, including the supplied streamer symbol.

    Option identity is checked against explicit expiry/right/strike/underlying,
    but this partial mapping is deliberately NOT a ContractBook.
    """
    if not isinstance(row, dict):
        raise QuoteUnavailable("IDENTITY_INVALID")
    kind = row.get("instrument-type")
    symbol = canonical(row.get("symbol"), kind)
    if symbol != canonical(requested, kind):
        raise QuoteUnavailable("IDENTITY_MISMATCH")
    streamer = row.get("streamer-symbol")
    if not isinstance(streamer, str) or not streamer or len(streamer) > 100 or any(c.isspace() for c in streamer):
        raise QuoteUnavailable("STREAMER_IDENTITY_MISSING")
    if row.get("active") is not True:
        raise QuoteUnavailable("IDENTITY_INACTIVE_OR_UNKNOWN")
    underlying = expiry = right = strike = is_etf = None
    if kind == "Equity":
        is_etf = row.get("is-etf") if type(row.get("is-etf")) is bool else None
        ident = row.get("cusip") or row.get("id")
        if isinstance(ident, bool) or not isinstance(ident, (str, int)) or not str(ident).strip():
            raise QuoteUnavailable("INSTRUMENT_ID_MISSING")
        ident = str(ident)
    else:
        match = re.fullmatch(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})", symbol)
        if not match:
            raise QuoteUnavailable("OPTION_IDENTITY_INVALID")
        try:
            expiry = date.fromisoformat(row["expiration-date"])
            occ_expiry = datetime.strptime(match[2], "%y%m%d").date()
        except (KeyError, TypeError, ValueError):
            raise QuoteUnavailable("OPTION_IDENTITY_INVALID") from None
        underlying = canonical(row.get("underlying-symbol"), "Equity")
        right, strike = row.get("option-type"), number(row.get("strike-price"), positive=True)
        if (expiry != occ_expiry or right != match[3] or underlying != match[1]
                or strike != Decimal(match[4]) / 1000):
            raise QuoteUnavailable("OPTION_IDENTITY_MISMATCH")
        ident = symbol
    payload = dict(symbol=symbol, provider_symbol=row["symbol"], streamer_symbol=streamer,
                   instrument_id=ident, kind=kind, underlying=underlying,
                   expiry=str(expiry), right=right, strike=str(strike), is_etf=is_etf)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    metadata = tuple((k, row[k]) for k in CAPTURE_FIELDS
                     if k in row and isinstance(row[k], (str, int, bool)) and not isinstance(row[k], float))
    return Instrument(symbol, row["symbol"], streamer, ident, kind, digest, aware(received),
                      underlying, expiry, right, strike, metadata, is_etf)


@dataclass(frozen=True)
class Quote:
    identity: Instrument
    bid: Decimal | None          # None: provider marked it unknown; 0: no bid
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    bid_at: datetime | None      # last bid change (seconds precision), not freshness
    ask_at: datetime | None
    received_at: datetime
    generation: str
    provenance: QuoteProvenance


@dataclass(frozen=True)
class Trade:
    identity: Instrument
    price: Decimal
    size: Decimal | None         # None: unavailable, never filled with zero
    traded_at: datetime
    received_at: datetime
    generation: str
    provenance: QuoteProvenance


@dataclass(frozen=True)
class Profile:
    identity: Instrument
    status: str                  # ACTIVE, HALTED or UNDEFINED as the provider sent it
    halt_start: datetime | None
    halt_end: datetime | None
    received_at: datetime
    generation: str


def _epoch(value) -> datetime | None:
    if type(value) is not int or value <= 0:
        return None
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
    except OverflowError:
        return None


class QuoteService:
    """No persisted live values; a new process/generation requires new events.

    All health/identity/event writers take the same short lock as held_health.
    Network operations must stay outside it. Metadata lifetime is an engineering
    assumption, documented separately from the unchanged risk quote-age policy.
    """
    def __init__(self, *, environment="production", identity_lifetime=timedelta(hours=24)):
        if environment not in {"production", "sandbox"} or identity_lifetime <= timedelta(0):
            raise QuoteUnavailable("INVALID_QUOTE_CONFIG")
        self.environment, self.identity_lifetime = environment, identity_lifetime
        self._lock = RLock()
        self.identities: dict[str, Instrument] = {}
        self._streamers: dict[str, str] = {}
        self._quotes: dict[str, Quote] = {}
        self._trades: dict[str, Trade] = {}
        self._profiles: dict[str, Profile] = {}
        self._watermarks: dict[str, tuple[datetime | None, datetime | None]] = {}
        self._failures: dict[tuple[str, str], str] = {}
        self._future: dict[tuple[str, str], dict] = {}
        self._last_receipt: dict[tuple[str, str], datetime] = {}
        self._kind_state: dict[str, str] = {}
        # Halt latch (child 3, R3): symbol -> evidence of the last HALTED Profile. Only an
        # ACTIVE Profile for that symbol in the current session clears it; delivery
        # failures, UNDEFINED, reconnects, trades and accepted maps never do.
        self._halts: dict[str, dict] = {}
        # Accepted events per (symbol, kind) in the current generation (child 3, R4).
        self._generation_events: dict[tuple[str, str], int] = {}
        self.generation = ""
        self.connected = False
        self.expires_at: datetime | None = None
        self.reason = "NOT_CONNECTED"
        self.events = {"Quote": 0, "Trade": 0, "Profile": 0}
        self.rejected = 0
        self.undecodable: dict[str, int] = {}

    def register(self, identity: Instrument):
        with self._lock:
            conflict = self._streamers.get(identity.streamer_symbol)
            if conflict and conflict != identity.symbol:
                self.invalidate(conflict, "IDENTITY_AMBIGUOUS")
                self.invalidate(identity.symbol, "IDENTITY_AMBIGUOUS")
                raise QuoteUnavailable("IDENTITY_AMBIGUOUS")
            old = self.identities.get(identity.symbol)
            if old and old.identity_digest != identity.identity_digest:
                self._streamers.pop(old.streamer_symbol, None)
                self.invalidate(identity.symbol, "IDENTITY_CHANGED")
            self.identities[identity.symbol] = identity
            self._streamers[identity.streamer_symbol] = identity.symbol
            # A successful identity refresh never revives an earlier quote failure.
            self._failures.pop((identity.symbol, "identity"), None)

    def invalidate(self, symbol: str, reason="IDENTITY_REFRESH_FAILED"):
        with self._lock:
            self._failures[symbol, "identity"] = reason
            self._quotes.pop(symbol, None)
            self._trades.pop(symbol, None)
            self._profiles.pop(symbol, None)
            self._watermarks.pop(symbol, None)

    def begin(self, expires_at: datetime):
        with self._lock:
            self.disconnect("CONNECTING")
            self.generation = uuid4().hex
            self.expires_at = aware(expires_at)
            self._kind_state.clear()
            self._generation_events.clear()

    def ready(self, received: datetime):
        """Transport/channel readiness. A decodable schema is tracked separately."""
        with self._lock:
            if self.expires_at is None or aware(received) >= self.expires_at:
                raise QuoteUnavailable("QUOTE_TOKEN_EXPIRED")
            self.connected, self.reason = True, "CONNECTED"

    def disconnect(self, reason="DISCONNECTED"):
        with self._lock:
            self.connected, self.reason = False, reason
            self._quotes.clear()
            self._trades.clear()
            self._profiles.clear()
            self._watermarks.clear()  # a new snapshot generation sets new ordering marks

    def withhold(self, kind: str, reason: str):
        """A schema change or failure for one event type: its values need new events."""
        with self._lock:
            store = {"Quote": self._quotes, "Trade": self._trades, "Profile": self._profiles}[kind]
            for symbol in list(self.identities):
                store.pop(symbol, None)
                self._failures[symbol, kind] = reason
                if kind == "Quote":
                    self._watermarks.pop(symbol, None)
            self._kind_state[kind] = reason

    def schema_accepted(self, kind: str):
        with self._lock:
            self._kind_state[kind] = "ACCEPTED"

    def kind_state(self, kind: str) -> str:
        with self._lock:
            return self._kind_state.get(kind, "NO_ACCEPTED_MAP")

    def note_undecodable(self, kind: str):
        with self._lock:
            self.undecodable[kind] = self.undecodable.get(kind, 0) + 1

    def _health(self, symbol: str, now: datetime) -> Instrument:
        now = aware(now)
        if not self.connected:
            raise QuoteUnavailable(self.reason)
        if self.expires_at is None or now >= self.expires_at:
            self.disconnect("QUOTE_TOKEN_EXPIRED")
            raise QuoteUnavailable("QUOTE_TOKEN_EXPIRED")
        identity = self.identities.get(symbol)
        if identity is None:
            raise QuoteUnavailable("IDENTITY_UNAVAILABLE")
        if (symbol, "identity") in self._failures:
            raise QuoteUnavailable(self._failures[symbol, "identity"])
        age = now - identity.received_at
        if not timedelta(0) <= age <= self.identity_lifetime:
            raise QuoteUnavailable("IDENTITY_STALE")
        return identity

    def symbol_for(self, streamer_symbol: str) -> str | None:
        """The registered desk symbol for a wire symbol (diagnostic decoding)."""
        with self._lock:
            return self._streamers.get(streamer_symbol)

    def identity(self, symbol: str, now: datetime) -> Instrument:
        """The currently registered, healthy identity (for mapping verification)."""
        with self._lock:
            return self._health(symbol, now)

    def feed(self, kind: str, row: dict, received: datetime):
        """Rejected/out-of-order events never freshen an earlier observation."""
        with self._lock:
            wire = row.get("eventSymbol")
            symbol = self._streamers.get(wire) if isinstance(wire, str) else None
            if symbol is None or kind not in FIELDS:
                self.rejected += 1
                return False
            self._last_receipt[symbol, kind] = aware(received)
            try:
                identity = self._health(symbol, received)
                if row.get("eventType") != kind:
                    raise QuoteUnavailable("EVENT_TYPE_MISMATCH")
                if kind == "Quote":
                    return self._quote(symbol, identity, row, received)
                if kind == "Trade":
                    return self._trade(symbol, identity, row, received)
                return self._profile(symbol, identity, row, received)
            except QuoteUnavailable as exc:
                if isinstance(exc, FutureSourceTime):
                    entry = self._future.get((symbol, kind), {"count": 0})
                    self._future[symbol, kind] = {"count": entry["count"] + 1,
                                                  "lead_ms": exc.lead / timedelta(milliseconds=1),
                                                  "received_at": aware(received).isoformat()}
                self._failures[symbol, kind] = str(exc)
                self.rejected += 1
                return False

    def _accept(self, symbol, kind, store, value):
        store[symbol] = value
        self._failures.pop((symbol, kind), None)
        self.events[kind] += 1
        self._generation_events[symbol, kind] = self._generation_events.get((symbol, kind), 0) + 1
        return True

    def generation_events(self) -> dict[str, dict[str, int]]:
        """Accepted events per symbol and component in the current generation."""
        with self._lock:
            out: dict[str, dict[str, int]] = {}
            for (symbol, kind), count in self._generation_events.items():
                out.setdefault(symbol, {})[kind] = count
            return out

    def _quote(self, symbol, identity, row, received):
        value = Quote(identity, optional_number(row.get("bidPrice")), optional_number(row.get("askPrice")),
                      optional_number(row.get("bidSize")), optional_number(row.get("askSize")),
                      optional_time(row.get("bidTime"), received), optional_time(row.get("askTime"), received),
                      aware(received), self.generation, self.provenance(identity))
        mark_bid, mark_ask = self._watermarks.get(symbol, (None, None))
        sides = ((value.bid_at, mark_bid), (value.ask_at, mark_ask))
        older = any(new is not None and mark is not None and new < mark for new, mark in sides)
        newer = any(new is not None and (mark is None or new > mark) for new, mark in sides)
        if older and not newer:
            # A purely older snapshot is ignorable; the retained quote stays as it was.
            self.rejected += 1
            return False
        self._watermarks[symbol] = tuple(max((t for t in (new, mark) if t is not None), default=None)
                                         for new, mark in sides)
        if older:
            # One side moved back while the other advanced (assumption E5): do not keep
            # the earlier BBO usable and do not assemble a hybrid. Wait for a snapshot at
            # or after both retained watermarks, or a new connection generation.
            self._failures[symbol, "Quote"] = "QUOTE_ORDER_AMBIGUOUS"
            self.rejected += 1
            return False
        return self._accept(symbol, "Quote", self._quotes, value)

    def _trade(self, symbol, identity, row, received):
        price = optional_number(row.get("price"))
        if price is None or price == 0:
            raise QuoteUnavailable("TRADE_PRICE_UNAVAILABLE")
        value = Trade(identity, price, optional_number(row.get("size")), event_time(row.get("time"), received),
                      aware(received), self.generation, self.provenance(identity))
        prior = self._trades.get(symbol)
        if prior and value.traded_at < prior.traded_at:
            self.rejected += 1
            return False
        return self._accept(symbol, "Trade", self._trades, value)

    def _profile(self, symbol, identity, row, received):
        status = row.get("tradingStatus")
        if not isinstance(status, str) or status not in STATUSES:
            raise QuoteUnavailable("PROFILE_STATUS_INVALID")
        value = Profile(identity, status, _epoch(row.get("haltStartTime")), _epoch(row.get("haltEndTime")),
                        aware(received), self.generation)
        if status == "HALTED":
            self._halts[symbol] = {"observed_at": aware(received).isoformat(), "generation": self.generation,
                                   "identity_digest": identity.identity_digest,
                                   "halt_start": value.halt_start.isoformat() if value.halt_start else None}
        elif status == "ACTIVE":
            self._halts.pop(symbol, None)  # positive resumption evidence for this symbol
        return self._accept(symbol, "Profile", self._profiles, value)

    def halted(self, symbol: str) -> dict | None:
        """Retained halt evidence for the symbol, if any (no health requirement)."""
        with self._lock:
            evidence = self._halts.get(symbol)
            return dict(evidence) if evidence else None

    def provenance(self, identity: Instrument, mapping_digest: str | None = None) -> QuoteProvenance:
        return QuoteProvenance(source=SOURCE, environment=self.environment, symbol=identity.symbol,
                               instrument_id=identity.instrument_id, streamer_symbol=identity.streamer_symbol,
                               identity_digest=identity.identity_digest, generation=self.generation,
                               mapping_digest=mapping_digest)

    def trade(self, symbol: str, now: datetime, max_age: timedelta) -> tuple[Trade, QuoteProvenance]:
        with self._lock:
            identity = self._health(symbol, now)
            if symbol in self._halts:
                raise QuoteUnavailable("SECURITY_HALTED")  # a halted print is not current evidence
            if (symbol, "Trade") in self._failures:
                raise QuoteUnavailable(self._failures[symbol, "Trade"])
            value = self._trades.get(symbol)
            if value is None or value.generation != self.generation:
                raise QuoteUnavailable("TRADE_UNAVAILABLE")
            if not timedelta(0) <= aware(now) - value.traded_at <= max_age:
                raise QuoteUnavailable("TRADE_STALE")
            return value, self.provenance(identity)

    def quote(self, symbol: str, now: datetime, max_age: timedelta) -> Quote:
        """Executable two-sided quote only; each unavailable part has its own code."""
        with self._lock:
            self._health(symbol, now)
            if symbol in self._halts:
                raise QuoteUnavailable("SECURITY_HALTED")
            if (symbol, "Quote") in self._failures:
                raise QuoteUnavailable(self._failures[symbol, "Quote"])
            value = self._quotes.get(symbol)
            if value is None or value.generation != self.generation:
                raise QuoteUnavailable("QUOTE_UNAVAILABLE")
            for label, price in (("BID", value.bid), ("ASK", value.ask)):
                if price is None:
                    raise QuoteUnavailable(f"QUOTE_{label}_UNAVAILABLE")
                if price == 0:
                    raise QuoteUnavailable(f"QUOTE_NO_{label}")
            if value.bid_at is None or value.ask_at is None:
                raise QuoteUnavailable("QUOTE_TIME_UNAVAILABLE")
            if any(not timedelta(0) <= aware(now) - at <= max_age for at in (value.bid_at, value.ask_at)):
                # Side-change age above the policy: not, by itself, evidence of a delayed feed.
                raise QuoteUnavailable("QUOTE_STALE")
            if value.bid_size is None or value.ask_size is None:
                raise QuoteUnavailable("QUOTE_SIZE_UNAVAILABLE")
            if value.bid >= value.ask:
                raise QuoteUnavailable("QUOTE_LOCKED" if value.bid == value.ask else "QUOTE_CROSSED")
            if value.bid_size == 0 or value.ask_size == 0:
                raise QuoteUnavailable("QUOTE_ZERO_SIZE")
            return value

    def status(self, symbol: str, now: datetime) -> tuple[str, str]:
        """(ACTIVE | HALTED | UNKNOWN, reason). UNDEFINED and missing Profile are UNKNOWN.

        A retained halt stays HALTED until an ACTIVE Profile clears it (R3).
        """
        with self._lock:
            self._health(symbol, now)
            live = self._profiles.get(symbol)
            if symbol in self._halts:
                if live is not None and live.generation == self.generation and live.status == "HALTED":
                    return "HALTED", "PROFILE_HALTED"
                return "HALTED", "HALT_RETAINED_NO_RESUMPTION_EVIDENCE"
            if (symbol, "Profile") in self._failures:
                return "UNKNOWN", self._failures[symbol, "Profile"]
            value = self._profiles.get(symbol)
            if value is None or value.generation != self.generation:
                return "UNKNOWN", ("PROFILE_UNAVAILABLE" if self.kind_state("Profile") == "ACCEPTED"
                                   else "PROFILE_" + self.kind_state("Profile"))
            if value.status == "UNDEFINED":
                return "UNKNOWN", "PROFILE_STATUS_UNDEFINED"
            return value.status, "PROFILE_" + value.status

    def last_trade(self, symbol: str) -> Trade | None:
        """Diagnostic only (strike selection, reports): never eligible evidence."""
        with self._lock:
            value = self._trades.get(symbol)
            return value if value is not None and value.generation == self.generation else None

    def trade_source(self, clock, max_age):
        """Trusted `(symbol, price, quote_at)` callable for EventRiskSource.

        Use TastytradeRiskSource for quote-backed tickets: it additionally binds
        provenance and mapping, and supplies the final transaction health fence.
        """
        def read(symbol):
            value, _ = self.trade(symbol, clock(), max_age)
            return value.identity.symbol, float(value.price), value.traded_at
        return read

    @contextmanager
    def held_health(self):
        """Local health/identity fence; caller holds it through ticket COMMIT."""
        with self._lock:
            yield self

    def inspect(self, now: datetime, *, max_age=None) -> dict:
        """Separate liveness, identity, receipt, side-change age, trade age and status."""
        if max_age is None:
            from desk.risk import RiskLimits
            max_age = RiskLimits().max_quote_age
        now = aware(now)
        with self._lock:
            checks = []
            for symbol, identity in self.identities.items():
                item = dict(symbol=symbol, provider_symbol=identity.provider_symbol,
                            streamer_symbol=identity.streamer_symbol, instrument_id=identity.instrument_id,
                            kind=identity.kind, generation=self.generation,
                            identity_received_at=identity.received_at.isoformat(),
                            identity_age_seconds=(now - identity.received_at).total_seconds(),
                            mapping="NOT_EVALUATED_BY_DIAGNOSTIC", receipt_session=session_label(now),
                            last_receipt={k: self._last_receipt[symbol, k].isoformat()
                                          for k in FIELDS if (symbol, k) in self._last_receipt})
                item["trade"] = self._trade_view(symbol, now, max_age)
                item["quote"] = self._quote_view(symbol, now, max_age)
                try:
                    status, reason = self.status(symbol, now)
                except QuoteUnavailable as exc:
                    status, reason = ("HALTED", "HALT_RETAINED_NO_RESUMPTION_EVIDENCE") if symbol in self._halts \
                        else ("UNKNOWN", str(exc))
                item["trading_status"] = dict(status=status, reason=reason)
                if symbol in self._halts:
                    item["trading_status"]["halt_evidence"] = dict(self._halts[symbol])
                for kind in ("Trade", "Quote"):
                    if (symbol, kind) in self._future:
                        item[kind.lower()]["clock_uncertainty"] = dict(self._future[symbol, kind],
                                                                       code="FUTURE_SOURCE_TIME", explanation=CLOCK_NOTE)
                checks.append(item)
            return dict(source=SOURCE, environment=self.environment, checked_at=now.isoformat(),
                        connected=self.connected, health_reason=self.reason, generation=self.generation,
                        schema={k: self.kind_state(k) for k in FIELDS}, events=dict(self.events),
                        rejected=self.rejected, undecodable=dict(self.undecodable), checks=checks)

    def _trade_view(self, symbol, now, max_age):
        try:
            value, proof = self.trade(symbol, now, max_age)
            view = dict(status="AVAILABLE", provenance=proof.model_dump())
        except QuoteUnavailable as exc:
            view = dict(status="UNAVAILABLE", reason=str(exc))
            value = self.last_trade(symbol)
        if value is None:
            view["lag_status"] = ("CLOCK_UNCERTAIN_FUTURE_SOURCE_TIME" if (symbol, "Trade") in self._future
                                  else "LAG_EVIDENCE_UNAVAILABLE")
            return view
        age = (now - value.traded_at).total_seconds()
        view.update(price=str(value.price), size=None if value.size is None else str(value.size),
                    size_status="AVAILABLE" if value.size is not None else "UNAVAILABLE",
                    source_at=value.traded_at.isoformat(), received_at=value.received_at.isoformat(),
                    receipt_minus_source_ms=(value.received_at - value.traded_at) / timedelta(milliseconds=1),
                    age_seconds=age, source_session=session_label(value.traded_at),
                    timestamp_semantics=PRECISION["Trade"],
                    lag_status="WITHIN_QUOTE_POLICY" if age <= max_age.total_seconds() else "EXCEEDS_QUOTE_POLICY")
        if view["status"] != "AVAILABLE":
            view["eligible"] = False  # historical diagnostic observation, never evidence
        return view

    def _quote_view(self, symbol, now, max_age):
        try:
            value = self.quote(symbol, now, max_age)
            view = dict(status="AVAILABLE", provenance=value.provenance.model_dump())
        except QuoteUnavailable as exc:
            view = dict(status="UNAVAILABLE", reason=str(exc))
            value = self._quotes.get(symbol)
            value = value if value is not None and value.generation == self.generation else None
        if value is None:
            return view
        text = lambda v: None if v is None else str(v)
        ages = {side: None if at is None else (now - at).total_seconds()
                for side, at in (("bid", value.bid_at), ("ask", value.ask_at))}
        view.update(bid=text(value.bid), ask=text(value.ask), bid_size=text(value.bid_size),
                    ask_size=text(value.ask_size), bid_changed_at=text(value.bid_at and value.bid_at.isoformat()),
                    ask_changed_at=text(value.ask_at and value.ask_at.isoformat()),
                    bid_change_age_seconds=ages["bid"], ask_change_age_seconds=ages["ask"],
                    received_at=value.received_at.isoformat(), timestamp_semantics=PRECISION["Quote"],
                    age_status=("SIDE_CHANGE_AGE_WITHIN_QUOTE_POLICY"
                                if all(a is not None and a <= max_age.total_seconds() for a in ages.values())
                                else "SIDE_CHANGE_AGE_EXCEEDS_QUOTE_POLICY_NOT_DELAY_EVIDENCE"))
        if view["status"] != "AVAILABLE":
            view["eligible"] = False
        return view


class FeedDecoder:
    """Use the server's accepted per-type maps, never a guessed field ordering.

    Maps merge per event type (assumption E1). A type with no accepted map is never
    decoded; its data is counted as undecodable. A changed map withholds that type's
    earlier values (E2). An invalid map for one type withholds that type only; the
    session stops when no Quote/Trade map remains usable or the format is unsupported.
    """
    def __init__(self, service: QuoteService, kinds=CORE, *, observer=None, channel=None):
        self.service, self.kinds, self.fields = service, tuple(kinds), {}
        # Optional diagnostic observer (desk.quote_measure, live-run package 3). It is
        # told about maps and rows after each decision and can never change one.
        self.observer, self.channel = observer, channel

    def _observe(self, method: str, *args, **kwargs):
        if self.observer is None:
            return
        try:
            getattr(self.observer, method)(self.channel, *args, **kwargs)
        except Exception as exc:  # a recorder fault ends recording, never the quote path
            observer, self.observer = self.observer, None
            try:
                observer.failed(type(exc).__name__)
            except Exception:
                pass

    def configure(self, message: dict):
        fmt = message.get("dataFormat")
        if fmt is not None and fmt != "COMPACT":
            for kind in self.kinds:
                self.fields.pop(kind, None)
                self.service.withhold(kind, "FEED_SCHEMA_UNSUPPORTED")
                self._observe("schema", kind, None, "WITHDRAWN_UNSUPPORTED_FORMAT")
            if any(k in CORE for k in self.kinds):
                raise QuoteUnavailable("FEED_SCHEMA_UNSUPPORTED")
            return
        fields = message.get("eventFields")
        if fields is None:
            return  # lazy configuration: the map may follow, before the first data
        if not isinstance(fields, dict):
            for kind in self.kinds:
                self.fields.pop(kind, None)
                self.service.withhold(kind, "FEED_SCHEMA_UNSUPPORTED")
                self._observe("schema", kind, None, "WITHDRAWN_INVALID_MAP")
            if any(k in CORE for k in self.kinds):
                raise QuoteUnavailable("FEED_SCHEMA_UNSUPPORTED")
            return
        rejected = False
        for kind in self.kinds:
            if kind not in fields:
                continue  # absent type keeps its previous map (E1)
            names = fields[kind]
            if (not isinstance(names, list) or not all(isinstance(n, str) for n in names)
                    or len(set(names)) != len(names) or not set(REQUIRED[kind]) <= set(names)):
                # Withhold this type only; its old values are not decoded under a known-bad map.
                self.fields.pop(kind, None)
                self.service.withhold(kind, "FEED_SCHEMA_UNSUPPORTED")
                self._observe("schema", kind, None, "WITHDRAWN_INVALID_MAP", offered=names)
                rejected = True
                continue
            names = tuple(names)
            changed = kind in self.fields and self.fields[kind] != names
            if changed:
                self.service.withhold(kind, "FEED_SCHEMA_CHANGED")  # earlier values need new events
            self.fields[kind] = names
            self.service.schema_accepted(kind)
            self._observe("schema", kind, names, "CHANGED" if changed else "ACCEPTED")
        if rejected and any(k in CORE for k in self.kinds) and not self.ready():
            raise QuoteUnavailable("FEED_SCHEMA_UNSUPPORTED")  # no Quote/Trade map left to use

    def ready(self) -> bool:
        return any(kind in self.fields for kind in self.kinds)

    def data(self, message: dict, received: datetime):
        data = message.get("data")
        if not isinstance(data, list) or len(data) % 2:
            raise QuoteUnavailable("FEED_DATA_INVALID")
        for i in range(0, len(data), 2):
            kind, values = data[i:i+2]
            if not isinstance(kind, str) or not isinstance(values, list):
                raise QuoteUnavailable("FEED_DATA_INVALID")
            names = self.fields.get(kind) if kind in self.kinds else None
            if names is None:
                # No accepted map (or not our type): positional data cannot be interpreted.
                self.service.note_undecodable(kind if kind in FIELDS else "OTHER")
                continue
            if len(values) % len(names):
                self.fields.pop(kind, None)
                self.service.withhold(kind, "FEED_DATA_INVALID")
                self._observe("schema", kind, None, "WITHDRAWN_DATA_INVALID")
                raise QuoteUnavailable("FEED_DATA_INVALID")
            for j in range(0, len(values), len(names)):
                row = dict(zip(names, values[j:j+len(names)], strict=True))
                accepted = self.service.feed(kind, row, received)
                self._observe("row", kind, row, received, accepted)
