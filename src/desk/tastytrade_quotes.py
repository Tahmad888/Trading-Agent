"""In-memory, independently identified DXLink quotes. No candles or volume API.

See docs/TASTYTRADE_QUOTES.md for primary sources, limits and source boundaries.
All provider errors are fixed codes: provider bodies/URLs may contain credentials.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from threading import RLock
from uuid import uuid4

from desk.risk_terms import QuoteProvenance

UTC = timezone.utc
SOURCE = "tastytrade-dxlink"
FIELDS = {
    "Quote": ("eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize", "bidTime", "askTime"),
    "Trade": ("eventType", "eventSymbol", "price", "size", "time"),
}


def session_label(at: datetime) -> str:
    from desk.calendar import clock, session, trading_day
    stamp = clock(at)
    if not trading_day(stamp.date()):
        return "CLOSED"
    opened, closed = session(stamp.date())
    return "RTH" if opened <= stamp < closed else "OFF_HOURS"


class QuoteUnavailable(ValueError):
    """Only internally generated, credential-free codes are exposed."""


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
        raise QuoteUnavailable("FUTURE_SOURCE_TIME")
    return result


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
    underlying = expiry = right = strike = None
    if kind == "Equity":
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
                   expiry=str(expiry), right=right, strike=str(strike))
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return Instrument(symbol, row["symbol"], streamer, ident, kind, digest, aware(received),
                      underlying, expiry, right, strike)


@dataclass(frozen=True)
class Quote:
    identity: Instrument
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    bid_at: datetime
    ask_at: datetime
    received_at: datetime
    generation: str
    provenance: QuoteProvenance


@dataclass(frozen=True)
class Trade:
    identity: Instrument
    price: Decimal
    size: Decimal
    traded_at: datetime
    received_at: datetime
    generation: str
    provenance: QuoteProvenance


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
        self._failures: dict[tuple[str, str], str] = {}
        self.generation = ""
        self.connected = False
        self.expires_at: datetime | None = None
        self.reason = "NOT_CONNECTED"
        self.events = {"Quote": 0, "Trade": 0}
        self.rejected = 0

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

    def begin(self, expires_at: datetime):
        with self._lock:
            self.disconnect("CONNECTING")
            self.generation = uuid4().hex
            self.expires_at = aware(expires_at)

    def ready(self, received: datetime):
        with self._lock:
            if self.expires_at is None or aware(received) >= self.expires_at:
                raise QuoteUnavailable("QUOTE_TOKEN_EXPIRED")
            self.connected, self.reason = True, "CONNECTED"

    def disconnect(self, reason="DISCONNECTED"):
        with self._lock:
            self.connected, self.reason = False, reason
            self._quotes.clear()
            self._trades.clear()

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

    def feed(self, kind: str, row: dict, received: datetime):
        """Rejected/out-of-order events never freshen an earlier observation."""
        with self._lock:
            wire = row.get("eventSymbol")
            symbol = self._streamers.get(wire) if isinstance(wire, str) else None
            if symbol is None or kind not in FIELDS:
                self.rejected += 1
                return False
            try:
                identity = self._health(symbol, received)
                if row.get("eventType") != kind:
                    raise QuoteUnavailable("EVENT_TYPE_MISMATCH")
                if kind == "Quote":
                    value = Quote(identity, number(row.get("bidPrice"), positive=True),
                                  number(row.get("askPrice"), positive=True), number(row.get("bidSize")),
                                  number(row.get("askSize")), event_time(row.get("bidTime"), received),
                                  event_time(row.get("askTime"), received), aware(received), self.generation,
                                  self.provenance(identity))
                    prior = self._quotes.get(symbol)
                    older = prior and (value.bid_at < prior.bid_at or value.ask_at < prior.ask_at)
                    store = self._quotes
                else:
                    value = Trade(identity, number(row.get("price"), positive=True), number(row.get("size")),
                                  event_time(row.get("time"), received), aware(received), self.generation,
                                  self.provenance(identity))
                    prior = self._trades.get(symbol)
                    older = prior and value.traded_at < prior.traded_at
                    store = self._trades
                if older:
                    self.rejected += 1
                    return False
                store[symbol] = value
                self._failures.pop((symbol, kind), None)
                self.events[kind] += 1
                return True
            except QuoteUnavailable as exc:
                self._failures[symbol, kind] = str(exc)
                self.rejected += 1
                return False

    def provenance(self, identity: Instrument) -> QuoteProvenance:
        return QuoteProvenance(source=SOURCE, environment=self.environment, symbol=identity.symbol,
                               instrument_id=identity.instrument_id, streamer_symbol=identity.streamer_symbol,
                               identity_digest=identity.identity_digest, generation=self.generation)

    def trade(self, symbol: str, now: datetime, max_age: timedelta) -> tuple[Trade, QuoteProvenance]:
        with self._lock:
            identity = self._health(symbol, now)
            if (symbol, "Trade") in self._failures:
                raise QuoteUnavailable(self._failures[symbol, "Trade"])
            value = self._trades.get(symbol)
            if value is None or value.generation != self.generation:
                raise QuoteUnavailable("TRADE_UNAVAILABLE")
            if not timedelta(0) <= aware(now) - value.traded_at <= max_age:
                raise QuoteUnavailable("TRADE_STALE")
            return value, self.provenance(identity)

    def quote(self, symbol: str, now: datetime, max_age: timedelta) -> Quote:
        with self._lock:
            self._health(symbol, now)
            if (symbol, "Quote") in self._failures:
                raise QuoteUnavailable(self._failures[symbol, "Quote"])
            value = self._quotes.get(symbol)
            if value is None or value.generation != self.generation:
                raise QuoteUnavailable("QUOTE_UNAVAILABLE")
            if any(not timedelta(0) <= aware(now) - at <= max_age for at in (value.bid_at, value.ask_at)):
                raise QuoteUnavailable("QUOTE_STALE")
            if value.bid >= value.ask:
                raise QuoteUnavailable("QUOTE_LOCKED" if value.bid == value.ask else "QUOTE_CROSSED")
            if value.bid_size == 0 or value.ask_size == 0:
                raise QuoteUnavailable("QUOTE_ZERO_SIZE")
            return value

    def trade_source(self, clock, max_age):
        """Trusted `(symbol, price, quote_at)` callable for EventRiskSource.

        Use TastytradeRiskSource for quote-backed tickets: it additionally binds
        provenance and supplies the final transaction health fence.
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
        if max_age is None:
            from desk.risk import RiskLimits
            max_age = RiskLimits().max_quote_age
        with self._lock:
            checks = []
            for symbol, identity in self.identities.items():
                item = dict(symbol=symbol, provider_symbol=identity.provider_symbol,
                            streamer_symbol=identity.streamer_symbol, instrument_id=identity.instrument_id,
                            kind=identity.kind, generation=self.generation,
                            identity_received_at=identity.received_at.isoformat(),
                            identity_age_seconds=(aware(now)-identity.received_at).total_seconds(),
                            receipt_session=session_label(now))
                for label, read in (("trade", self.trade), ("quote", self.quote)):
                    try:
                        value = read(symbol, now, max_age)
                        if label == "trade":
                            value, proof = value
                            item[label] = dict(status="AVAILABLE", price=str(value.price),
                                               source_at=value.traded_at.isoformat(),
                                               age_seconds=(aware(now) - value.traded_at).total_seconds(),
                                               source_session=session_label(value.traded_at),
                                               received_at=value.received_at.isoformat(),
                                               provenance=proof.model_dump())
                        else:
                            item[label] = dict(status="AVAILABLE", bid=str(value.bid), ask=str(value.ask),
                                               bid_size=str(value.bid_size), ask_size=str(value.ask_size),
                                               bid_at=value.bid_at.isoformat(), ask_at=value.ask_at.isoformat(),
                                               bid_age_seconds=(aware(now)-value.bid_at).total_seconds(),
                                               ask_age_seconds=(aware(now)-value.ask_at).total_seconds(),
                                               bid_session=session_label(value.bid_at),
                                               ask_session=session_label(value.ask_at),
                                               provenance=value.provenance.model_dump(),
                                               received_at=value.received_at.isoformat())
                    except QuoteUnavailable as exc:
                        item[label] = dict(status="UNAVAILABLE", reason=str(exc))
                        old = (self._trades if label == "trade" else self._quotes).get(symbol)
                        if old is not None:
                            # Historical diagnostic observation, never eligible evidence.
                            item[label]["last_observation_not_eligible"] = (
                                dict(price=str(old.price), source_at=old.traded_at.isoformat(),
                                     age_seconds=(aware(now)-old.traded_at).total_seconds()) if label == "trade"
                                else dict(bid=str(old.bid), ask=str(old.ask), bid_at=old.bid_at.isoformat(),
                                          ask_at=old.ask_at.isoformat(),
                                          bid_age_seconds=(aware(now)-old.bid_at).total_seconds(),
                                          ask_age_seconds=(aware(now)-old.ask_at).total_seconds()))
                checks.append(item)
            return dict(source=SOURCE, environment=self.environment, connected=self.connected,
                        health_reason=self.reason, events=dict(self.events), rejected=self.rejected, checks=checks)


class FeedDecoder:
    """Use negotiated names and strides, never a guessed field ordering."""
    def __init__(self, service: QuoteService):
        self.service, self.fields = service, {}

    def configure(self, message: dict):
        fields = message.get("eventFields")
        if message.get("dataFormat") != "COMPACT" or not isinstance(fields, dict):
            raise QuoteUnavailable("FEED_SCHEMA_UNAVAILABLE")
        parsed = {}
        for kind, required in FIELDS.items():
            names = fields.get(kind)
            if (not isinstance(names, list) or not all(isinstance(n, str) for n in names)
                    or len(set(names)) != len(names) or not set(required) <= set(names)):
                raise QuoteUnavailable("FEED_SCHEMA_UNSUPPORTED")
            parsed[kind] = tuple(names)
        self.fields = parsed

    def data(self, message: dict, received: datetime):
        data = message.get("data")
        if not self.fields or not isinstance(data, list) or len(data) % 2:
            raise QuoteUnavailable("FEED_DATA_INVALID")
        for i in range(0, len(data), 2):
            kind, values = data[i:i+2]
            if not isinstance(kind, str) or kind not in self.fields or not isinstance(values, list):
                raise QuoteUnavailable("FEED_DATA_UNSUPPORTED")
            names = self.fields[kind]
            if len(values) % len(names):
                raise QuoteUnavailable("FEED_DATA_INVALID")
            for j in range(0, len(values), len(names)):
                self.service.feed(kind, dict(zip(names, values[j:j+len(names)], strict=True)), received)
