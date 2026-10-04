"""Opt-in, read-only Alpaca historical SIP volume evidence (G5 checkpoint 1).

Producer and cache. Checkpoint 2 consumers reach it only through
``desk.alpaca_source`` (opt-in configuration); identities come from
``desk.alpaca_assets``.
Request contract: https://docs.alpaca.markets/us/reference/stockbars (Sourced):
explicit ``feed=sip``; ``split`` adjusts price and volume for splits, ``raw`` applies
none; pagination until ``next_page_token`` is null; ``limit`` counts across symbols.
https://docs.alpaca.markets/us/docs/market-data-faq (Sourced): unsubscribed
historical SIP needs an ``end`` at least 15 minutes old.

Only timestamps and volumes are retained, exactly as returned (no multipliers).
Prices stay Webull's; nothing here is a price, VWAP or Webull instrument record.
Historical access proves nothing about real-time entitlement or current bars.

Plan B: any authentication, entitlement, rate-limit, pagination or validation
failure makes the affected ticker's volume UNAVAILABLE. There is no fallback to
IEX, Webull or a ranking-list volume field.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import ssl
from typing import Annotated, Literal
from urllib import error, parse, request

import pandas as pd
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from desk.bars import BarDataError
from desk.calendar import ET, clock, previous_trading_day, session, trading_day
from desk.playbook.cards import CARDS

HOST = "data.alpaca.markets"
ROUTE = "/v2/stocks/bars"
REFERENCE = "https://docs.alpaca.markets/us/reference/stockbars"
FEED = "sip"
PAGE_LIMIT = 10000
KEY_ENV, SECRET_ENV = "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"
# Response headers kept as evidence; none carries a credential.
SAFE_HEADERS = ("date", "content-type", "x-request-id", "x-ratelimit-limit",
                "x-ratelimit-remaining", "x-ratelimit-reset")
# The run stops on these; no retry, key rotation or alternative route.
STOP_CODES = frozenset({"AUTH_OR_ENTITLEMENT_FAILURE", "RATE_LIMITED"})
HISTORICAL_DELAY = timedelta(minutes=15)  # Sourced: market-data FAQ, unsubscribed SIP
MAPPING = ("alpaca-symbol-as-requested; asof not sent (provider default symbol mapping); "
           "no independent provider identity resolved")  # probe/legacy namespace only
CHANNELS = {"1Day": "native-daily", "15Min": "rth-m15"}
STEP = {"1Day": timedelta(days=1), "15Min": timedelta(minutes=15)}
SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,9}")

Text = Annotated[str, Field(min_length=1)]


def definition_id(adjustment: str, channel: str) -> str:
    """Session/aggregation definition: same vendor does not mean same coverage."""
    return f"alpaca:{FEED}:{adjustment}:{channel}:provider-reported"


def share_basis_id(adjustment: str) -> str:
    return f"alpaca:{FEED}:adjustment={adjustment}"


class AlpacaVolumeError(BarDataError):
    """Fixed diagnostic codes only; never provider bodies, URLs or headers."""

    def __init__(self, code: str):
        self.code = code
        super().__init__("Alpaca volume: " + code)


def _utc(stamp: datetime) -> str:
    return pd.Timestamp(stamp).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- request ----

class BarRequest(BaseModel):
    """One batch query. Feed, limit and sort are fixed; adjustment is explicit."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbols: tuple[Text, ...]
    timeframe: Literal["1Day", "15Min"]
    start: AwareDatetime
    end: AwareDatetime
    adjustment: Literal["split", "raw"]
    feed: Literal["sip"] = FEED
    limit: Literal[10000] = PAGE_LIMIT
    sort: Literal["asc"] = "asc"

    @model_validator(mode="after")
    def bounded(self):
        if not self.symbols or len(set(self.symbols)) != len(self.symbols) or not all(
                SYMBOL.fullmatch(s) for s in self.symbols):
            raise ValueError("Symbols must be unique upper-case tickers")
        if self.start > self.end:
            raise ValueError("Request start is after its end")
        return self

    @property
    def channel(self) -> str:
        return CHANNELS[self.timeframe]

    def params(self, page_token: str | None = None) -> list[tuple[str, str]]:
        args = [("symbols", ",".join(self.symbols)), ("timeframe", self.timeframe),
                ("start", _utc(self.start)), ("end", _utc(self.end)), ("limit", str(self.limit)),
                ("adjustment", self.adjustment), ("feed", self.feed), ("sort", self.sort)]
        return args + ([("page_token", page_token)] if page_token else [])

    def url(self, page_token: str | None = None) -> str:
        return parse.urlunsplit(("https", HOST, ROUTE, parse.urlencode(self.params(page_token)), ""))


# --------------------------------------------------------------- evidence ----

class VolumeObservation(BaseModel):
    """One ticker's volumes from one complete (fully paginated) batch request."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: Literal["https://docs.alpaca.markets/us/reference/stockbars"] = REFERENCE
    requested_symbol: Text
    returned_symbol: Text
    provider_identity: Text | None  # None: not independently resolved (see MAPPING)
    mapping_provenance: Text
    feed: Literal["sip"]
    adjustment: Literal["split", "raw"]
    timeframe: Literal["1Day", "15Min"]
    channel: Literal["native-daily", "rth-m15"]
    definition_id: Text
    share_basis_id: Text
    requested_start: AwareDatetime
    requested_end: AwareDatetime
    first_sent_at: AwareDatetime
    received_at: AwareDatetime
    # (bar start in UTC, volume exactly as returned)
    bars: tuple[tuple[str, Decimal], ...]
    page_digests: tuple[str, ...]
    content_digest: str
    complete: Literal[True] = True

    @staticmethod
    def content(fields: dict) -> str:
        keys = ("requested_symbol", "returned_symbol", "provider_identity", "mapping_provenance", "feed",
                "adjustment", "timeframe", "channel", "definition_id", "share_basis_id")
        payload = {k: fields[k] for k in keys}
        payload.update(requested_start=_utc(fields["requested_start"]), requested_end=_utc(fields["requested_end"]),
                       bars=[[t, str(v)] for t, v in fields["bars"]])
        return _digest(payload)

    @model_validator(mode="after")
    def consistent(self):
        if self.channel != CHANNELS[self.timeframe] or self.definition_id != definition_id(self.adjustment, self.channel):
            raise ValueError("Volume channel/definition disagrees with the request")
        if self.share_basis_id != share_basis_id(self.adjustment):
            raise ValueError("Share basis disagrees with the requested adjustment")
        stamps = [t for t, _ in self.bars]
        if stamps != sorted(set(stamps)) or any(not v.is_finite() or v < 0 for _, v in self.bars):
            raise ValueError("Bars must be unique, ordered and finite nonnegative")
        if not self.first_sent_at <= self.received_at:
            raise ValueError("Receipt precedes request")
        if self.content_digest != self.content(self.__dict__):
            raise ValueError("Content digest does not match the observation")
        return self

    @property
    def identity(self) -> tuple:
        return (self.requested_symbol, self.returned_symbol, self.provider_identity, self.mapping_provenance)

    def observed_through(self) -> datetime | None:
        """End of the latest bar: a lower bound on when it could exist, not a receipt time."""
        if not self.bars:
            return None
        return pd.Timestamp(self.bars[-1][0]).to_pydatetime() + STEP[self.timeframe]


@dataclass(frozen=True)
class HttpReply:
    status: int
    headers: dict
    body: bytes


@dataclass(frozen=True)
class BatchResult:
    request: BarRequest
    status: Literal["COMPLETE", "PARTIAL", "UNAVAILABLE"]
    observations: dict[str, VolumeObservation]
    failures: dict[str, str]          # ticker -> code; one bad ticker never erases another
    error: str | None = None          # request-level code (auth, pagination, envelope...)
    issues: tuple[str, ...] = ()      # request-level notes (an unrequested symbol...)
    pages: tuple[dict, ...] = ()      # sanitized page receipts, no headers with secrets
    raw_pages: tuple[bytes, ...] = field(default=(), repr=False)
    from_cache: bool = False
    cache_status: dict[str, dict] = field(default_factory=dict)

    def report(self) -> dict:
        return {"request": {"route": "https://" + HOST + ROUTE, "params": dict(self.request.params())},
                "status": self.status, "error": self.error, "issues": list(self.issues),
                "http_requests": len(self.pages), "pages": list(self.pages), "from_cache": self.from_cache,
                "failures": dict(sorted(self.failures.items())),
                "cache_status": self.cache_status,
                "observations": {s: _obs_summary(o) for s, o in sorted(self.observations.items())}}


def _obs_summary(obs: VolumeObservation) -> dict:
    through = obs.observed_through()
    return {"returned_symbol": obs.returned_symbol, "provider_identity": obs.provider_identity,
            "mapping_provenance": obs.mapping_provenance, "feed": obs.feed, "adjustment": obs.adjustment,
            "channel": obs.channel, "definition_id": obs.definition_id, "share_basis_id": obs.share_basis_id,
            "requested_start": _utc(obs.requested_start), "requested_end": _utc(obs.requested_end),
            "first_sent_at": obs.first_sent_at.isoformat(), "received_at": obs.received_at.isoformat(),
            "bar_count": len(obs.bars), "first_bar": obs.bars[0][0] if obs.bars else None,
            "last_bar": obs.bars[-1][0] if obs.bars else None,
            "observed_through": through.isoformat() if through else None,
            "page_digests": list(obs.page_digests), "content_digest": obs.content_digest, "complete": obs.complete}


# ------------------------------------------------------------- transport ----

class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # credentials are headers; never forward them to another URL


def _safe_headers(headers) -> dict:
    return {k.lower(): v for k, v in headers.items() if k.lower() in SAFE_HEADERS}


def _fetch(req: request.Request, timeout: float) -> HttpReply:
    # Default context: certificate and hostname verification stay on.
    opener = request.build_opener(_NoRedirect(), request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(req, timeout=timeout) as response:
            return HttpReply(response.status, _safe_headers(response.headers), response.read())
    except error.HTTPError as exc:
        reply = HttpReply(exc.code, _safe_headers(exc.headers or {}), b"")  # error body never read
        exc.close()
        return reply


class RequestBudget:
    """Hard cap on HTTP requests, shared by every call of one run."""

    def __init__(self, limit: int):
        self.limit, self.used = limit, 0

    def take(self):
        if self.used >= self.limit:
            raise AlpacaVolumeError("REQUEST_BUDGET_EXHAUSTED")
        self.used += 1


NETWORK_FORBIDDEN = "NETWORK_FORBIDDEN_IN_FINAL_GUARD"


def _http_code(status: int) -> str:
    if status in (401, 403):
        return "AUTH_OR_ENTITLEMENT_FAILURE"
    return "RATE_LIMITED" if status == 429 else "HTTP_FAILURE"


def _volume(value) -> Decimal:
    if isinstance(value, float):  # JSON NaN/Infinity arrive as floats (decimals parse as Decimal)
        raise AlpacaVolumeError("NONFINITE_OR_NEGATIVE_VOLUME" if not math.isfinite(value) else "MALFORMED_VOLUME")
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise AlpacaVolumeError("MALFORMED_VOLUME")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise AlpacaVolumeError("MALFORMED_VOLUME") from None
    if not number.is_finite() or number < 0:
        raise AlpacaVolumeError("NONFINITE_OR_NEGATIVE_VOLUME")
    return number


def _stamp(value) -> pd.Timestamp:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise AlpacaVolumeError("MALFORMED_TIMESTAMP")
    try:
        return pd.Timestamp(value)
    except ValueError:
        raise AlpacaVolumeError("MALFORMED_TIMESTAMP") from None


def _channel_member(stamp: pd.Timestamp, timeframe: str) -> bool:
    local = stamp.tz_convert(ET)
    day = local.date()
    if not trading_day(day):
        return False  # weekend/holiday rows are never sessions
    if timeframe == "1Day":
        return local == pd.Timestamp(day).tz_localize(ET)  # Alpaca daily stamp: ET midnight
    opened, closed = session(day)
    return opened <= local < closed and (local - opened) % STEP["15Min"] == timedelta(0)


def _symbol_bars(rows, req: BarRequest) -> tuple[tuple[str, Decimal], ...]:
    if not isinstance(rows, list) or not rows:
        raise AlpacaVolumeError("SYMBOL_MISSING")
    out, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or "t" not in row or "v" not in row:
            raise AlpacaVolumeError("MALFORMED_BAR")
        stamp, volume = _stamp(row["t"]), _volume(row["v"])
        if stamp in seen:
            raise AlpacaVolumeError("DUPLICATE_TIMESTAMP")
        seen.add(stamp)
        if out and stamp < pd.Timestamp(out[-1][0]):
            raise AlpacaVolumeError("UNSORTED_BARS")
        if not pd.Timestamp(req.start) <= stamp <= pd.Timestamp(req.end):
            raise AlpacaVolumeError("OUTSIDE_REQUEST_BOUNDS")
        if not _channel_member(stamp, req.timeframe):
            raise AlpacaVolumeError("WRONG_CHANNEL_ROW")
        out.append((_utc(stamp), volume))
    return tuple(out)


# ------------------------------------------------------------------ cache ----

class VolumeCache:
    """Cache by feed, adjustment, identity, timeframe and bounds; keeps revisions.

    Eligibility is decided by an append-only event log ordered by sequence, not by
    receipt time: a ticker's cached snapshot is reusable only when its latest event
    for that request key is a successful refresh and no provider stop (auth,
    entitlement, rate limit) was recorded after it. Failures never delete earlier
    snapshots; those stay as audit history.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS identities(symbol TEXT PRIMARY KEY, identity TEXT NOT NULL,
                    first_seen TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots(digest TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS current(key TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    first_received TEXT NOT NULL, last_received TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(sequence INTEGER PRIMARY KEY, key TEXT, at TEXT,
                    old_digest TEXT, new_digest TEXT, changed TEXT);
                CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT,
                    symbol TEXT, at TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('OK','FAILED','STOP')),
                    code TEXT, digest TEXT);
            ''')

    @staticmethod
    def key(req: BarRequest, symbol: str, namespace: str | None = None) -> str:
        # A resolved identity mapping is its own namespace; a changed mapping never
        # rewrites an earlier namespace's pin or history. No namespace: legacy/probe key.
        parts = [req.feed, req.adjustment, symbol, req.timeframe, _utc(req.start), _utc(req.end)]
        return json.dumps(parts + ([namespace] if namespace else []))

    @staticmethod
    def namespace(obs: VolumeObservation) -> str | None:
        return None if obs.mapping_provenance == MAPPING else obs.mapping_provenance

    def pin(self, obs: VolumeObservation, at: datetime):
        identity = json.dumps(list(obs.identity))
        space = self.namespace(obs)
        symbol = obs.requested_symbol if space is None else obs.requested_symbol + "|" + space
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT identity FROM identities WHERE symbol=?", (symbol,)).fetchone()
            if old and old[0] != identity:
                raise AlpacaVolumeError("IDENTITY_CHANGED")
            db.execute("INSERT OR IGNORE INTO identities VALUES (?,?,?)", (symbol, identity, clock(at).isoformat()))

    def record(self, req: BarRequest, obs: VolumeObservation) -> dict:
        self.pin(obs, obs.received_at)
        key = self.key(req, obs.requested_symbol, self.namespace(obs))
        at = clock(obs.received_at).isoformat()
        payload = obs.model_dump_json()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT c.digest,c.last_received,c.revision,s.payload FROM current c "
                             "JOIN snapshots s ON s.digest=c.digest WHERE c.key=?", (key,)).fetchone()
            db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?)", (obs.content_digest, payload))
            if old is not None and clock(old[1]) > clock(obs.received_at):
                raise AlpacaVolumeError("CLOCK_MOVED_BACKWARDS")
            # The success event is what makes this key eligible again, even when the
            # content is unchanged; its sequence orders it after any earlier failure.
            db.execute("INSERT INTO events(key,symbol,at,state,code,digest) VALUES (?,?,?,'OK',NULL,?)",
                       (key, obs.requested_symbol, at, obs.content_digest))
            if old is None:
                db.execute("INSERT INTO current VALUES (?,?,?,?,0)", (key, obs.content_digest, at, at))
                return {"status": "NEW", "revision": 0, "changed_bars": []}
            if old[0] == obs.content_digest:
                db.execute("UPDATE current SET last_received=? WHERE key=?", (at, key))
                return {"status": "UNCHANGED", "revision": old[2], "changed_bars": []}
            before = dict(json.loads(old[3])["bars"])
            after = {t: str(v) for t, v in obs.bars}
            changed = sorted(t for t in before.keys() | after.keys() if before.get(t) != after.get(t))
            db.execute("INSERT INTO revisions(key,at,old_digest,new_digest,changed) VALUES (?,?,?,?,?)",
                       (key, at, old[0], obs.content_digest, json.dumps(changed)))
            db.execute("UPDATE current SET digest=?, last_received=?, revision=? WHERE key=?",
                       (obs.content_digest, at, old[2] + 1, key))
            return {"status": "REVISED", "revision": old[2] + 1, "changed_bars": changed}

    def record_failure(self, req: BarRequest, symbol: str, code: str, at: datetime, namespace: str | None = None):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO events(key,symbol,at,state,code,digest) VALUES (?,?,?,'FAILED',?,NULL)",
                       (self.key(req, symbol, namespace), symbol, clock(at).isoformat(), code))

    def record_stop(self, code: str, at: datetime):
        """A provider stop makes every earlier success ineligible until refreshed."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO events(key,symbol,at,state,code,digest) VALUES (NULL,NULL,?,'STOP',?,NULL)",
                       (clock(at).isoformat(), code))

    def eligible(self, req: BarRequest, symbol: str,
                 namespace: str | None = None) -> tuple[VolumeObservation | None, str | None]:
        """(snapshot, None) when reusable now; otherwise (None, the reason)."""
        key = self.key(req, symbol, namespace)
        with closing(sqlite3.connect(self.path)) as db:
            latest = db.execute("SELECT sequence,state,code,digest FROM events WHERE key=? "
                                "ORDER BY sequence DESC LIMIT 1", (key,)).fetchone()
            stop = db.execute("SELECT sequence,code FROM events WHERE state='STOP' "
                              "ORDER BY sequence DESC LIMIT 1").fetchone()
            row = db.execute("SELECT s.payload,c.digest FROM current c JOIN snapshots s ON s.digest=c.digest "
                             "WHERE c.key=?", (key,)).fetchone()
        if latest is None:
            return None, "NOT_CACHED"
        if latest[1] != "OK":
            return None, latest[2]
        if stop is not None and stop[0] > latest[0]:
            return None, "PROVIDER_STOP_AFTER_LAST_SUCCESS:" + stop[1]
        if row is None or row[1] != latest[3]:
            return None, "CACHE_STATE_INCONSISTENT"
        return VolumeObservation.model_validate_json(row[0]), None

    def current(self, req: BarRequest, symbol: str, namespace: str | None = None) -> VolumeObservation | None:
        return self.eligible(req, symbol, namespace)[0]

    def revisions(self, req: BarRequest, symbol: str, namespace: str | None = None) -> list[dict]:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT at,old_digest,new_digest,changed FROM revisions WHERE key=? ORDER BY sequence",
                              (self.key(req, symbol, namespace),)).fetchall()
        return [{"at": a, "old": o, "new": n, "changed_bars": json.loads(c)} for a, o, n, c in rows]


# ----------------------------------------------------------------- client ----

class AlpacaVolumeClient:
    def __init__(self, key_id: str, secret: str, *, budget: RequestBudget, transport=_fetch,
                 clock_fn=lambda: datetime.now(timezone.utc), timeout: float = 30.0,
                 cache: VolumeCache | None = None):
        if not all(isinstance(v, str) and v.strip() for v in (key_id, secret)):
            raise AlpacaVolumeError("NOT_CONFIGURED")
        self._key_id, self._secret = key_id.strip(), secret.strip()
        self._budget, self._transport, self._clock, self._timeout = budget, transport, clock_fn, timeout
        self.cache = cache
        self.stopped: str | None = None
        # Set while a ticket's final guard holds the cache file (audit F2): no request
        # may be sent then, so no provider wait can happen inside the final locks.
        self.network_blocked = False

    @classmethod
    def from_env(cls, env=None, **kwargs):
        env = os.environ if env is None else env
        return cls(env.get(KEY_ENV, ""), env.get(SECRET_ENV, ""), **kwargs)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise AlpacaVolumeError("INVALID_RECEIPT_CLOCK")
        return now

    def _unavailable(self, req, code, pages=(), raw=(), issues=(), spaces=None):
        failures = {s: code for s in req.symbols}
        if self.cache is not None:
            at = self._now()
            if code in STOP_CODES:
                self.cache.record_stop(code, at)
            for s in req.symbols:
                self.cache.record_failure(req, s, code, at, (spaces or {}).get(s))
        return BatchResult(req, "UNAVAILABLE", {}, failures, code, tuple(issues), tuple(pages), tuple(raw))

    @staticmethod
    def _spaces(req: BarRequest, identities) -> dict | None:
        if identities is None:
            return None
        if set(identities) != set(req.symbols) or any(r.alpaca_symbol != s for s, r in identities.items()):
            raise AlpacaVolumeError("IDENTITY_REQUEST_MISMATCH")
        return {s: r.namespace for s, r in identities.items()}

    def _reuse(self, req: BarRequest, spaces=None) -> BatchResult:
        """Cache only: never sends a request. Each ticker is eligible or carries its reason."""
        observations, failures = {}, {}
        for symbol in req.symbols:
            obs, reason = self.cache.eligible(req, symbol, (spaces or {}).get(symbol))
            if obs is None:
                failures[symbol] = reason
            else:
                observations[symbol] = obs
        status = "COMPLETE" if not failures else "PARTIAL" if observations else "UNAVAILABLE"
        return BatchResult(req, status, observations, failures, None if observations else "NO_ELIGIBLE_CACHE",
                           from_cache=True, cache_status={s: {"status": "REUSED"} for s in observations})

    def fetch_assets(self):
        """One read-only GET of the paper host's active US-equity asset list.

        Same budget, stop, TLS, redirect and redaction rules as the bar route. Returns
        an ``AssetList``; raises ``AssetError`` with a fixed code otherwise.
        """
        from desk.alpaca_assets import ASSET_HOST, ASSET_PARAMS, ASSET_ROUTE, AssetError, parse_assets
        if self.network_blocked:
            raise AssetError(NETWORK_FORBIDDEN)
        if self.stopped:
            raise AssetError("RUN_STOPPED_AFTER_" + self.stopped)
        try:
            self._budget.take()
        except AlpacaVolumeError:
            raise AssetError("REQUEST_BUDGET_EXHAUSTED") from None
        url = parse.urlunsplit(("https", ASSET_HOST, ASSET_ROUTE, parse.urlencode(ASSET_PARAMS), ""))
        sent = self._now()
        outgoing = request.Request(url, headers={
            "APCA-API-KEY-ID": self._key_id, "APCA-API-SECRET-KEY": self._secret,
            "Accept": "application/json", "User-Agent": "trading-desk/0.1"})
        try:
            reply = self._transport(outgoing, self._timeout)
        except (error.URLError, TimeoutError, OSError):
            self.asset_receipt = {"sent_at": sent.isoformat(), "http_status": None}
            raise AssetError("TRANSPORT_FAILURE") from None
        received = self._now()
        body = reply.body if reply.status == 200 else b""
        unsafe = self._key_id.encode() in body or self._secret.encode() in body
        self.asset_receipt = {"route": "https://" + ASSET_HOST + ASSET_ROUTE, "params": dict(ASSET_PARAMS),
                              "sent_at": sent.isoformat(), "received_at": received.isoformat(),
                              "http_status": reply.status, "headers": dict(reply.headers), "bytes": len(body),
                              "sha256": hashlib.sha256(body).hexdigest() if body and not unsafe else None}
        if reply.status != 200:
            code = _http_code(reply.status)
            if code in STOP_CODES:
                self.stopped = code
                if self.cache is not None:
                    self.cache.record_stop(code, received)
            raise AssetError(code)
        if unsafe:
            raise AssetError("UNSAFE_RESPONSE")
        return parse_assets(body, received)

    def fetch(self, req: BarRequest, *, reuse: bool = False, identities=None) -> BatchResult:
        """``identities`` (Alpaca symbol -> pinned ``IdentityRecord``) puts each ticker in
        its mapping's cache namespace and records the Alpaca asset ID as its identity."""
        spaces = self._spaces(req, identities)
        # An active run stop outranks every path, the cached one included. The cached
        # path stays read-only (the STOP is already persisted), so it never needs the
        # writer slot a ticket's final guard may hold.
        if self.stopped:
            code = "RUN_STOPPED_AFTER_" + self.stopped
            if reuse:
                return BatchResult(req, "UNAVAILABLE", {}, {s: code for s in req.symbols}, code, from_cache=True)
            return self._unavailable(req, code, spaces=spaces)
        if reuse:
            if self.cache is None:
                raise AlpacaVolumeError("NO_CACHE_CONFIGURED")
            return self._reuse(req, spaces)
        if self.network_blocked:
            return BatchResult(req, "UNAVAILABLE", {}, {s: NETWORK_FORBIDDEN for s in req.symbols},
                               NETWORK_FORBIDDEN)
        sent = self._now()
        if clock(req.end) > clock(sent) - HISTORICAL_DELAY:
            return self._unavailable(req, "END_NOT_15_MINUTES_OLD", spaces=spaces)
        rows: dict[str, list] = {}
        pages, raw, digests, issues, tokens, token = [], [], [], [], set(), None
        while True:
            try:
                self._budget.take()
            except AlpacaVolumeError:
                # A remaining page is incomplete evidence, never a truncated success.
                return self._unavailable(req, "INCOMPLETE_PAGINATION" if pages else "REQUEST_BUDGET_EXHAUSTED",
                                         pages, raw, issues, spaces=spaces)
            page_sent = self._now()
            outgoing = request.Request(req.url(token), headers={
                "APCA-API-KEY-ID": self._key_id, "APCA-API-SECRET-KEY": self._secret,
                "Accept": "application/json", "User-Agent": "trading-desk/0.1"})
            try:
                reply = self._transport(outgoing, self._timeout)
            except (error.URLError, TimeoutError, OSError):
                pages.append({"page": len(pages) + 1, "sent_at": page_sent.isoformat(), "http_status": None})
                return self._unavailable(req, "TRANSPORT_FAILURE", pages, raw, issues, spaces)
            received = self._now()
            body = reply.body if reply.status == 200 else b""
            unsafe = self._key_id.encode() in body or self._secret.encode() in body
            receipt = {"page": len(pages) + 1, "page_token_sent": token is not None,
                       "sent_at": page_sent.isoformat(), "received_at": received.isoformat(),
                       "http_status": reply.status, "headers": dict(reply.headers),
                       "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest() if body and not unsafe else None}
            pages.append(receipt)
            if reply.status != 200:
                code = _http_code(reply.status)
                if code in STOP_CODES:
                    self.stopped = code
                return self._unavailable(req, code, pages, raw, issues, spaces)
            if unsafe:
                return self._unavailable(req, "UNSAFE_RESPONSE", pages, raw, issues, spaces)
            raw.append(body)
            digests.append(receipt["sha256"])
            try:
                envelope = json.loads(body, parse_float=Decimal)
            except (ValueError, TypeError):
                return self._unavailable(req, "INVALID_JSON", pages, raw, issues, spaces)
            bars = envelope.get("bars") if isinstance(envelope, dict) else None
            following = envelope.get("next_page_token") if isinstance(envelope, dict) else None
            if not isinstance(envelope, dict) or not isinstance(bars, (dict, type(None))) or (
                    following is not None and (not isinstance(following, str) or not following)):
                return self._unavailable(req, "INVALID_ENVELOPE", pages, raw, issues, spaces)
            if envelope.get("currency", "USD") != "USD":
                return self._unavailable(req, "CURRENCY_MISMATCH", pages, raw, issues, spaces)
            receipt["next_page_token_present"] = following is not None
            for symbol, symbol_rows in (bars or {}).items():
                if symbol not in req.symbols:
                    issues.append("UNREQUESTED_SYMBOL")  # never attributed to a requested ticker
                    continue
                rows.setdefault(symbol, []).append(symbol_rows)
            if following is None:
                break
            if following in tokens:
                return self._unavailable(req, "PAGINATION_LOOP", pages, raw, issues, spaces)
            tokens.add(following)
            token = following
        observations, failures, cache_status = {}, {}, {}
        for symbol in req.symbols:
            try:
                parts = rows.get(symbol, [])
                if any(not isinstance(p, list) for p in parts):
                    raise AlpacaVolumeError("MALFORMED_BAR")
                bars = _symbol_bars([r for p in parts for r in p], req)
                record = (identities or {}).get(symbol)
                values = dict(requested_symbol=symbol, returned_symbol=symbol,
                              provider_identity=record.alpaca_asset_id if record else None,
                              mapping_provenance=record.namespace if record else MAPPING, feed=req.feed, adjustment=req.adjustment,
                              timeframe=req.timeframe, channel=req.channel,
                              definition_id=definition_id(req.adjustment, req.channel),
                              share_basis_id=share_basis_id(req.adjustment), requested_start=req.start,
                              requested_end=req.end, first_sent_at=sent, received_at=received, bars=bars,
                              page_digests=tuple(digests))
                obs = VolumeObservation(**values, content_digest=VolumeObservation.content(values))
                if self.cache is not None:
                    cache_status[symbol] = self.cache.record(req, obs)
                observations[symbol] = obs
            except AlpacaVolumeError as exc:
                failures[symbol] = exc.code
                if self.cache is not None:
                    self.cache.record_failure(req, symbol, exc.code, received, (spaces or {}).get(symbol))
        status = "COMPLETE" if not failures else "PARTIAL" if observations else "UNAVAILABLE"
        return BatchResult(req, status, observations, failures, None, tuple(sorted(set(issues))),
                           tuple(pages), tuple(raw), False, cache_status)


# ----------------------------------------------------------- calculation ----

@dataclass(frozen=True)
class ComparisonPolicy:
    """Directional: RTH first-30 numerator over a native-daily 50-session mean.

    Not a claim that daily and RTH bars cover the same trades: the daily bar is
    the provider's own session aggregate (observed to include extended hours).
    """
    id: str = "alpaca-sip-split:rth30/native-daily50-v1"
    adjustment: str = "split"
    numerator: str = definition_id("split", "rth-m15")
    denominator: str = definition_id("split", "native-daily")
    intervals: int = 2
    sessions: int = 50


POLICY = ComparisonPolicy()
EP_CARD = "5_qullamaggie_episodic_pivot"


def approved_rule() -> tuple[Decimal, str]:
    """The EP card's current early-volume threshold (0.5) and the card fingerprint.

    Read at every calculation and revalidation; callers cannot supply a threshold,
    and a stored result never selects its own rule. An approved card change
    (new fingerprint) requires fresh qualification.
    """
    card = CARDS[EP_CARD]
    return Decimal(str(card.p("early_volume"))), card.fingerprint()
SCOPE = "volume component only; not setup qualification, signal activation or profitability evidence"


class EPVolumeComponent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbol: Text
    entry_session: date
    policy: Literal["alpaca-sip-split:rth30/native-daily50-v1"]
    numerator_definition: Text
    denominator_definition: Text
    share_basis_id: Text
    first30_intervals: tuple[tuple[str, Decimal], ...]
    first30_volume: Decimal
    prior_sessions: tuple[date, ...]
    prior_count: int
    prior50_total: Decimal
    prior50_average: Decimal
    ratio: Decimal
    threshold: Decimal
    rule_version: Text  # EP card fingerprint the threshold came from
    threshold_met: bool
    daily_digest: Text
    rth_digest: Text
    scope: Literal["volume component only; not setup qualification, signal activation or profitability evidence"] = SCOPE

    @model_validator(mode="after")
    def approved_and_consistent(self):
        # An unsupported threshold cannot claim this policy; any experiment needs
        # its own non-eligible policy id. The pass flag must follow from the numbers.
        if self.threshold != approved_rule()[0]:
            raise ValueError("Threshold is not the approved EP rule for this policy")
        if self.prior_count != len(self.prior_sessions) or self.threshold_met != (
                self.first30_volume * self.prior_count >= self.threshold * self.prior50_total):
            raise ValueError("Volume result is inconsistent with its inputs")
        return self

    def report(self) -> dict:
        data = self.model_dump(mode="json")
        data["prior_first"], data["prior_last"] = str(self.prior_sessions[0]), str(self.prior_sessions[-1])
        data["ratio_display"] = f"{self.ratio:.6f}"
        return data


def prior_sessions(entry: date, count: int = POLICY.sessions) -> tuple[date, ...]:
    """Exchange sessions strictly before the entry session (holidays never count)."""
    days, day = [], entry
    for _ in range(count):
        day = previous_trading_day(day)
        days.append(day)
    return tuple(reversed(days))


def ep_volume_component(entry: date, daily: VolumeObservation, rth: VolumeObservation) -> EPVolumeComponent:
    threshold, rule_version = approved_rule()
    if daily.channel != "native-daily" or rth.channel != "rth-m15":
        raise AlpacaVolumeError("WRONG_CHANNEL")
    if daily.adjustment != rth.adjustment or daily.share_basis_id != rth.share_basis_id:
        raise AlpacaVolumeError("SHARE_BASIS_MISMATCH")
    if daily.adjustment != POLICY.adjustment:
        raise AlpacaVolumeError("RAW_DIAGNOSTIC_ONLY")
    if (daily.definition_id, rth.definition_id) != (POLICY.denominator, POLICY.numerator):
        raise AlpacaVolumeError("DEFINITIONS_LACK_COMPARISON_POLICY")
    if daily.identity != rth.identity or daily.requested_symbol != daily.returned_symbol:
        raise AlpacaVolumeError("IDENTITY_MISMATCH")
    if not trading_day(entry):
        raise AlpacaVolumeError("ENTRY_NOT_A_SESSION")
    prior = prior_sessions(entry)
    by_day = {pd.Timestamp(t).tz_convert(ET).date(): v for t, v in daily.bars}
    if any(d not in by_day for d in prior):
        raise AlpacaVolumeError("MISSING_DAILY_SESSIONS")
    # The daily aggregate includes extended hours (to 20:00 ET); a receipt before
    # the following ET midnight could hold a developing bar (Assumption).
    if clock(daily.received_at) < pd.Timestamp(prior[-1] + timedelta(days=1)).tz_localize(ET):
        raise AlpacaVolumeError("DAILY_NOT_COMPLETED_AT_RECEIPT")
    opened = session(entry)[0]
    needed = [(opened + STEP["15Min"] * i).tz_convert("UTC") for i in range(POLICY.intervals)]
    by_stamp = {pd.Timestamp(t).tz_convert("UTC"): v for t, v in rth.bars}
    if any(stamp not in by_stamp for stamp in needed):
        raise AlpacaVolumeError("MISSING_RTH_INTERVALS")  # never zero-filled
    if clock(rth.received_at) < needed[-1] + STEP["15Min"] + HISTORICAL_DELAY:
        raise AlpacaVolumeError("RTH_NOT_COMPLETED_AT_RECEIPT")
    with localcontext() as ctx:
        ctx.prec = 34
        total = sum((by_day[d] for d in prior), Decimal(0))
        average = total / len(prior)
        if average <= 0:
            raise AlpacaVolumeError("ZERO_BASELINE")
        first30 = sum((by_stamp[s] for s in needed), Decimal(0))
        ratio = first30 / average
        met = first30 * len(prior) >= threshold * total  # exact; no division at the boundary
    return EPVolumeComponent(
        symbol=daily.requested_symbol, entry_session=entry, policy=POLICY.id,
        numerator_definition=rth.definition_id, denominator_definition=daily.definition_id,
        share_basis_id=daily.share_basis_id, first30_intervals=tuple((_utc(s), by_stamp[s]) for s in needed),
        first30_volume=first30, prior_sessions=prior, prior_count=len(prior), prior50_total=total,
        prior50_average=average, ratio=ratio, threshold=threshold, rule_version=rule_version, threshold_met=met,
        daily_digest=daily.content_digest, rth_digest=rth.content_digest)


def revalidate(component: EPVolumeComponent, daily: VolumeObservation, rth: VolumeObservation):
    """A revised input never leaves an earlier volume result intact."""
    if (daily.content_digest, rth.content_digest) != (component.daily_digest, component.rth_digest):
        raise AlpacaVolumeError("VOLUME_EVIDENCE_REVISED")
    if (component.threshold, component.rule_version) != approved_rule():
        raise AlpacaVolumeError("VOLUME_RULE_CHANGED_REQUALIFY")
    if ep_volume_component(component.entry_session, daily, rth) != component:
        raise AlpacaVolumeError("VOLUME_RESULT_MISMATCH")


def evaluate(entry: date, daily: BatchResult, rth: BatchResult) -> dict[str, EPVolumeComponent | str]:
    """Per-ticker outcome: a component, or the code that made that ticker unavailable."""
    out: dict[str, EPVolumeComponent | str] = {}
    for symbol in dict.fromkeys(daily.request.symbols + rth.request.symbols):
        if symbol not in daily.observations or symbol not in rth.observations:
            out[symbol] = daily.failures.get(symbol) or rth.failures.get(symbol) or "SYMBOL_NOT_REQUESTED"
            continue
        try:
            out[symbol] = ep_volume_component(entry, daily.observations[symbol], rth.observations[symbol])
        except AlpacaVolumeError as exc:
            out[symbol] = exc.code
    return out
