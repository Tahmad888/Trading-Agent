"""Bounded read-only REST + DXLink transport; no account or order endpoints."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import logging
import ssl
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from desk.tastytrade_quotes import (CORE, FIELDS, OPTIONAL, UTC, FeedDecoder, QuoteService, QuoteUnavailable,
                                   aware, instrument)

HOSTS = {"production": "https://api.tastyworks.com", "sandbox": "https://api.cert.tastyworks.com"}
FEED_CHANNEL, PROFILE_CHANNEL = 3, 5
MEASURE_CHANNEL = 7     # opt-in diagnostic measurements (desk.quote_measure); never eligibility
HANDSHAKE_SECONDS = 10   # SETUP → channel open (engineering bound)
SCHEMA_SECONDS = 10      # subscription → first accepted Quote/Trade map (engineering bound)
# dxLink ErrorMessage types; anything else is reported as UNKNOWN, never echoed.
ERROR_TYPES = {"UNSUPPORTED_PROTOCOL", "TIMEOUT", "UNAUTHORIZED", "INVALID_MESSAGE", "BAD_ACTION", "UNKNOWN"}
# Transient faults that a bounded capture may recover from with a fresh token and
# generation. Denial, schema, protocol, rate-limit and token-expiry faults stop.
RECOVERABLE = {"DXLINK_HEARTBEAT_TIMEOUT", "DXLINK_TRANSPORT_FAILURE"}


def utcnow():
    return datetime.now(UTC)


def json_read(raw):
    def invalid(_):
        raise QuoteUnavailable("INVALID_JSON_NUMBER")
    try:
        result = json.loads(raw, parse_float=Decimal, parse_constant=invalid)
    except (ValueError, TypeError):
        raise QuoteUnavailable("INVALID_JSON") from None
    if not isinstance(result, dict):
        raise QuoteUnavailable("INVALID_JSON")
    return result


def tls_context():
    # Default trust honors SSL_CERT_FILE, including the user's certifi bundle.
    context = ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise QuoteUnavailable("TLS_VERIFICATION_REQUIRED")
    return context


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward an OAuth secret/Bearer header to a redirect.


def request_json(method, url, headers, payload, *, timeout=15, opener=None):
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        opener = opener or build_opener(_NoRedirect(), HTTPSHandler(context=tls_context()))
        with opener.open(Request(url, body, headers, method=method), timeout=timeout) as reply:
            raw = reply.read(4_000_001)
            if len(raw) > 4_000_000:
                raise QuoteUnavailable("REST_REPLY_TOO_LARGE")
            result = json_read(raw)
            if "error" in result:
                raise QuoteUnavailable("REST_ERROR")
            return result
    except HTTPError as exc:
        # Neither the error body nor its URL is safe to echo.
        # HTTPError also owns a response; dispose of it without reading the body.
        exc.close()
        raise QuoteUnavailable(f"REST_HTTP_{exc.code}") from None
    except QuoteUnavailable:
        raise
    except Exception:
        raise QuoteUnavailable("REST_TRANSPORT_FAILURE") from None


def stream_endpoint_allowed(url) -> bool:
    """The quote token is only ever sent to wss://<sub>.dxfeed.com[:443]/path."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    return (parts.scheme == "wss" and host.endswith(".dxfeed.com") and host != ".dxfeed.com"
            and "@" not in parts.netloc and not parts.username and not parts.password
            and not parts.query and not parts.fragment and port in {None, 443})


@dataclass(frozen=True)
class Credentials:
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)


@dataclass(frozen=True)
class StreamToken:
    url: str = field(repr=False)
    token: str = field(repr=False)
    expires_at: datetime
    entitlement: str | None


class ReadClient:
    def __init__(self, credentials: Credentials, *, environment="production", request=request_json,
                 clock=utcnow, max_requests=20):
        if environment not in HOSTS or type(max_requests) is not int or not 1 <= max_requests <= 100:
            raise QuoteUnavailable("INVALID_REST_CONFIG")
        if not credentials.client_secret or not credentials.refresh_token:
            raise QuoteUnavailable("CREDENTIALS_MISSING")
        self.credentials, self.environment, self.request, self.clock = credentials, environment, request, clock
        self.max_requests, self.requests = max_requests, 0
        self._access, self._expires = None, None

    def _call(self, method, path, payload=None, authenticated=True, *, query=None):
        # Private allowlist also protects future callers from accidental account use.
        allowed = (path in {"/api-quote-tokens", "/market-data/by-type"} or path.startswith("/instruments/equities/")
                   or path.startswith("/instruments/equity-options/")
                   or (path.startswith("/option-chains/") and path.endswith("/nested")))
        if not ((method == "POST" and path == "/oauth/token" and not authenticated)
                or (method == "GET" and allowed and authenticated)):
            raise QuoteUnavailable("REST_ROUTE_REFUSED")
        if query is not None and (path != "/market-data/by-type" or method != "GET"):
            raise QuoteUnavailable("REST_QUERY_REFUSED")
        if authenticated and (self._expires is None or self.clock() >= self._expires - timedelta(seconds=10)):
            self.authenticate()
        if self.requests >= self.max_requests:
            raise QuoteUnavailable("REST_REQUEST_BUDGET")
        self.requests += 1
        headers = {"User-Agent": "TradingDesk/0.1.0", "Accept": "application/json", "Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self._access
        try:
            suffix = "?" + urlencode(query) if query else ""
            result = self.request(method, HOSTS[self.environment] + path + suffix, headers, payload)
            if not isinstance(result, dict) or "error" in result:
                raise QuoteUnavailable("REST_ERROR")
            return result
        except QuoteUnavailable:
            raise
        except Exception:
            raise QuoteUnavailable("REST_TRANSPORT_FAILURE") from None

    def market_quotes(self, identities):
        """One batched read-only snapshot; resolved instruments, never guessed options.

        Dasherized REST fields are normalized separately in desk.snapshot_quotes.
        No retry/cache and no QuoteService or trading-state mutation occurs here.
        """
        from desk.tastytrade_quotes import Instrument, canonical
        if not isinstance(identities, (list, tuple)) or not 1 <= len(identities) <= 100:
            raise QuoteUnavailable("SNAPSHOT_REQUEST_INVALID")
        groups, seen = {}, set()
        for identity in identities:
            if not isinstance(identity, Instrument) or identity.kind not in {"Equity", "Equity Option"}:
                raise QuoteUnavailable("SNAPSHOT_REQUEST_INVALID")
            if (canonical(identity.provider_symbol, identity.kind) != identity.symbol
                    or not identity.identity_digest or identity.symbol in seen):
                raise QuoteUnavailable("SNAPSHOT_REQUEST_INVALID")
            seen.add(identity.symbol)
            key = "equity" if identity.kind == "Equity" else "equity-option"
            groups.setdefault(key, []).append(identity.provider_symbol)
        result = self._call("GET", "/market-data/by-type",
                            query={key: ",".join(symbols) for key, symbols in groups.items()})
        data = result.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise QuoteUnavailable("SNAPSHOT_REPLY_INVALID")
        if result.get("pagination") not in (None, {}):
            raise QuoteUnavailable("SNAPSHOT_PAGINATION_UNEXPECTED")
        return data["items"]

    def authenticate(self):
        result = self._call("POST", "/oauth/token", {
            "grant_type": "refresh_token", "client_secret": self.credentials.client_secret,
            "refresh_token": self.credentials.refresh_token, "scope": "read"}, authenticated=False)
        lifetime, token = result.get("expires_in"), result.get("access_token")
        if (type(lifetime) is not int or not 0 < lifetime <= 900 or not isinstance(token, str)
                or not token or result.get("token_type", "").lower() != "bearer"):
            raise QuoteUnavailable("OAUTH_REPLY_INVALID")
        self._access, self._expires = token, self.clock() + timedelta(seconds=lifetime)
        # A missing scope field is NOT proof of the granted scope.

    def stream_token(self) -> StreamToken:
        data = self._call("GET", "/api-quote-tokens").get("data")
        if not isinstance(data, dict):
            raise QuoteUnavailable("QUOTE_TOKEN_REPLY_INVALID")
        url, token = data.get("dxlink-url"), data.get("token")
        if not isinstance(url, str) or not isinstance(token, str) or not token:
            raise QuoteUnavailable("QUOTE_TOKEN_REPLY_INVALID")
        if not stream_endpoint_allowed(url):
            raise QuoteUnavailable("QUOTE_ENDPOINT_INVALID")
        try:
            expires = aware(datetime.fromisoformat(data["expires-at"].replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise QuoteUnavailable("QUOTE_TOKEN_EXPIRY_MISSING") from None
        if not self.clock() < expires <= self.clock() + timedelta(hours=24, seconds=30):
            raise QuoteUnavailable("QUOTE_TOKEN_EXPIRY_INVALID")
        level = data.get("level")
        return StreamToken(url, token, expires, level if isinstance(level, str) else None)

    def resolve(self, requested: str, kind: str, service: QuoteService):
        from desk.tastytrade_quotes import canonical
        if service.environment != self.environment:
            raise QuoteUnavailable("QUOTE_ENVIRONMENT_MISMATCH")
        from desk.tastytrade_quotes import equity_provider_symbol
        symbol = canonical(requested, kind)
        provider_symbol = equity_provider_symbol(symbol) if kind == "Equity" else requested
        route = "equities" if kind == "Equity" else "equity-options"
        try:
            row = self._call("GET", f"/instruments/{route}/" + quote(provider_symbol, safe="")).get("data")
            identity = instrument(requested, row, self.clock())
            service.register(identity)
            return identity
        except QuoteUnavailable as exc:
            service.invalidate(symbol)
            if str(exc) in {"REST_HTTP_401", "REST_HTTP_403"}:
                service.disconnect(str(exc))
            raise

    def chain_options(self, underlying: str) -> list[tuple[date, Decimal, str, str]]:
        """Actual listed Standard (expiry, strike, call, put) rows; no selection here.

        The request uses tastytrade's Equity symbol (``BRK/B``, one encoded path
        component); returned underlyings are compared in canonical Equity form, so a
        share class never matches another class or underlying. Option symbols are
        returned exactly as listed.
        """
        from desk.tastytrade_quotes import canonical, equity_provider_symbol, number
        symbol = canonical(underlying, "Equity")
        data = self._call("GET", "/option-chains/" + quote(equity_provider_symbol(symbol), safe="")
                          + "/nested").get("data")

        def same_underlying(value):
            try:
                return canonical(value, "Equity") == symbol
            except QuoteUnavailable:
                return False  # malformed or non-Equity underlying never matches
        try:
            rows = {}
            for chain in data["items"]:
                if not same_underlying(chain.get("underlying-symbol")) or chain.get("option-chain-type") != "Standard":
                    continue
                for expiry in chain["expirations"]:
                    day = date.fromisoformat(expiry["expiration-date"])
                    for row in expiry["strikes"]:
                        if isinstance(row.get("call"), str) and isinstance(row.get("put"), str):
                            key = (day, number(row["strike-price"], positive=True))
                            if rows.setdefault(key, (row["call"], row["put"])) != (row["call"], row["put"]):
                                raise QuoteUnavailable("OPTION_CHAIN_AMBIGUOUS")
        except QuoteUnavailable as exc:
            if str(exc) == "OPTION_CHAIN_AMBIGUOUS":
                raise
            raise QuoteUnavailable("OPTION_CHAIN_UNAVAILABLE") from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise QuoteUnavailable("OPTION_CHAIN_UNAVAILABLE") from None
        if not rows:
            raise QuoteUnavailable("OPTION_CHAIN_UNAVAILABLE")
        return [(day, strike, call, put) for (day, strike), (call, put) in rows.items()]

    def chain_pair(self, underlying: str, *, strike: Decimal | None = None, reference: Decimal | None = None,
                   median: bool = False, now: datetime | None = None) -> tuple[str, str]:
        selection = select_pair(self.chain_options(underlying), now=now or self.clock(), strike=strike,
                                reference=reference, median=median)
        return selection["call"], selection["put"]


def select_pair(options, *, now, strike=None, reference=None, median=False) -> dict:
    """A diagnostic call/put pair, never a trading plan.

    Expiry: the nearest listed Standard expiry not yet expired on the ET market date
    (today's expiry only before today's close). Strike: nearest listed to an explicit
    strike, else to an observed underlying price; the median listed strike only when
    explicitly allowed, labelled as not representative. Nothing is invented.
    """
    from desk.calendar import clock, session, trading_day
    stamp = clock(now)
    today = stamp.date()
    open_today = trading_day(today) and stamp < session(today)[1]
    live = [x for x in options if x[0] > today or (x[0] == today and open_today)]
    if not live:
        raise QuoteUnavailable("OPTION_CHAIN_UNAVAILABLE")
    first = min(x[0] for x in live)
    rows = sorted(x for x in live if x[0] == first)
    if strike is not None:
        method, target = "EXPLICIT_STRIKE", Decimal(str(strike))
    elif reference is not None:
        method, target = "NEAR_OBSERVED_UNDERLYING", Decimal(str(reference))
    elif median:
        method, target = "MEDIAN_LISTED_NOT_REPRESENTATIVE", None
    else:
        raise QuoteUnavailable("OPTION_STRIKE_UNAVAILABLE")
    if target is not None and (not target.is_finite() or target <= 0):
        raise QuoteUnavailable("OPTION_STRIKE_UNAVAILABLE")
    chosen = (min(rows, key=lambda x: (abs(x[1] - target), x[1])) if target is not None
              else rows[len(rows) // 2])
    return dict(call=chosen[2], put=chosen[3], method=method, strike=str(chosen[1]), expiry=chosen[0].isoformat(),
                target=None if target is None else str(target))


class Session:
    """DXLink state machine, independently testable without a socket.

    Transport readiness (channel open, subscription sent) is separate from schema
    availability (an accepted per-type map). Subscribing never waits for a map: the
    dxLink specification lets the server send FEED_CONFIG lazily, before first data.
    """
    def __init__(self, service: QuoteService, token: StreamToken, now: datetime, *, profile: bool = False,
                 measure=None):
        self.service, self.token = service, token
        # ``measure`` (desk.quote_measure.Recorder) observes the core decoder and owns a
        # separate measurement channel; its rows never reach the service (package 3).
        self.measure = measure
        self.decoder = FeedDecoder(service, CORE, observer=measure, channel=FEED_CHANNEL)
        self.profile = FeedDecoder(service, OPTIONAL) if profile else None
        self.profile_state = "REQUESTED" if profile else "NOT_REQUESTED"
        self.phase, self.keepalive, self.live = "SETUP", 30.0, False
        self.subscribed: set[tuple[int, str, str]] = set()
        service.begin(token.expires_at)
        self.generation = service.generation
        if measure is not None:
            measure.begin(self.generation, aware(now))

    @staticmethod
    def setup():
        return {"type": "SETUP", "channel": 0, "version": "0.1-DXF-JS/0.3.0",
                "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60}

    def schema_ready(self) -> bool:
        return self.decoder.ready()

    def receive(self, message: dict, received: datetime) -> list[dict]:
        try:
            return self._receive(message, received)
        except QuoteUnavailable as exc:
            # Invalidate before WebSocket teardown can wait for close, but never touch a
            # newer session's state.
            if self.service.generation == self.generation and str(exc) != "SESSION_NOT_CONNECTED":
                if self.measure is not None:
                    self.measure.down(str(exc), aware(received))
                self.service.disconnect(str(exc))  # keep the original reason when already down
            raise

    def _subscription(self, channel, kinds, identities, *, reset):
        add = []
        for identity in identities:
            if channel == PROFILE_CHANNEL and identity.kind != "Equity":
                continue
            for kind in kinds:
                if channel == MEASURE_CHANNEL and not self.measure.wants(kind, identity.kind):
                    continue
                key = (channel, kind, identity.streamer_symbol)
                if key not in self.subscribed:
                    self.subscribed.add(key)
                    add.append({"type": kind, "symbol": identity.streamer_symbol})
        if not add and not reset:
            return []
        message = {"type": "FEED_SUBSCRIPTION", "channel": channel, "add": add}
        if reset:
            message["reset"] = True
        return [message]

    def add(self, identities) -> list[dict]:
        """Subscribe later identities (each once) without resetting the initial set."""
        out = self._subscription(FEED_CHANNEL, CORE, identities, reset=False) if self.phase == "STREAMING" else []
        if self.profile_state == "OPEN":
            out += self._subscription(PROFILE_CHANNEL, OPTIONAL, identities, reset=False)
        if self.measure is not None and self.measure.state == "OPEN":
            out += self._subscription(MEASURE_CHANNEL, self.measure.kinds, identities, reset=False)
        return out

    def _profile_down(self, code):
        self.profile_state = "UNAVAILABLE"
        self.service.withhold("Profile", code)
        return []

    def _measure_down(self, code, received):
        self.measure.down(code, aware(received))
        return []

    @staticmethod
    def _setup_message(channel, kinds):
        return {"type": "FEED_SETUP", "channel": channel, "acceptAggregationPeriod": 0.1,
                "acceptDataFormat": "COMPACT", "acceptEventFields": {k: list(FIELDS[k]) for k in kinds}}

    def _receive(self, message: dict, received: datetime) -> list[dict]:
        kind, channel = message.get("type"), message.get("channel")
        if self.service.generation != self.generation:
            raise QuoteUnavailable("SESSION_SUPERSEDED")
        if self.live and not self.service.connected:
            # Nothing (KEEPALIVE included) revives a session whose evidence was withdrawn.
            raise QuoteUnavailable("SESSION_NOT_CONNECTED")
        profile_channel = self.profile is not None and channel == PROFILE_CHANNEL
        measure_channel = self.measure is not None and channel == MEASURE_CHANNEL
        if kind == "ERROR":
            code = message.get("error") if message.get("error") in ERROR_TYPES else "UNKNOWN"
            if profile_channel:
                return self._profile_down("PROFILE_CHANNEL_ERROR_" + code)
            if measure_channel:
                return self._measure_down("MEASURE_CHANNEL_ERROR_" + code, received)
            # Informational in the protocol, but this desk withholds evidence on it.
            raise QuoteUnavailable("DXLINK_ERROR_" + code)
        if kind == "CHANNEL_CLOSED":
            if profile_channel:
                return self._profile_down("PROFILE_CHANNEL_CLOSED")
            if measure_channel:
                return self._measure_down("MEASURE_CHANNEL_CLOSED", received)
            raise QuoteUnavailable("DXLINK_DENIED_OR_CLOSED")
        if aware(received) >= self.token.expires_at:
            raise QuoteUnavailable("QUOTE_TOKEN_EXPIRED")
        if kind == "SETUP" and channel == 0 and self.phase == "SETUP":
            timeout = message.get("keepaliveTimeout")
            if isinstance(timeout, bool) or not isinstance(timeout, (int, Decimal, float)) or not 2 <= timeout <= 60:
                raise QuoteUnavailable("KEEPALIVE_CONFIG_INVALID")
            self.keepalive, self.phase = float(timeout)/2, "AUTH"
            return []
        if kind == "AUTH_STATE" and channel == 0:
            state = message.get("state")
            if state == "UNAUTHORIZED" and self.phase == "AUTH":
                self.phase = "AUTH_SENT"
                return [{"type": "AUTH", "channel": 0, "token": self.token.token}]
            if state == "AUTHORIZED" and self.phase == "AUTH_SENT":
                self.phase = "CHANNEL"
                return [{"type": "CHANNEL_REQUEST", "channel": FEED_CHANNEL, "service": "FEED",
                         "parameters": {"contract": "AUTO"}}]
            raise QuoteUnavailable("DXLINK_AUTH_STATE_INVALID")
        if kind == "CHANNEL_OPENED" and channel == FEED_CHANNEL and self.phase == "CHANNEL":
            self.service.ready(received)
            self.live, self.phase = True, "STREAMING"
            out = [self._setup_message(FEED_CHANNEL, CORE)]
            out += self._subscription(FEED_CHANNEL, CORE, list(self.service.identities.values()), reset=True)
            if self.profile is not None:
                # Requested only now, so its snapshot cannot arrive before the service is ready.
                out.append({"type": "CHANNEL_REQUEST", "channel": PROFILE_CHANNEL, "service": "FEED",
                            "parameters": {"contract": "AUTO"}})
            if self.measure is not None:
                self.measure.requested()
                out.append({"type": "CHANNEL_REQUEST", "channel": MEASURE_CHANNEL, "service": "FEED",
                            "parameters": {"contract": "AUTO"}})
            return out
        if kind == "CHANNEL_OPENED" and profile_channel and self.profile_state == "REQUESTED":
            self.profile_state = "OPEN"
            return [self._setup_message(PROFILE_CHANNEL, OPTIONAL)] + self._subscription(
                PROFILE_CHANNEL, OPTIONAL, list(self.service.identities.values()), reset=True)
        if kind == "FEED_CONFIG" and channel == FEED_CHANNEL and self.phase == "STREAMING":
            self.decoder.configure(message)
            return []
        if kind in {"FEED_CONFIG", "FEED_DATA", "CHANNEL_OPENED"} and profile_channel:
            if self.profile_state != "OPEN":
                # Profile withheld (or a duplicate open): never disturbs Quote/Trade.
                if kind == "FEED_DATA":
                    self.service.note_undecodable("Profile")
                return []
            if kind == "CHANNEL_OPENED":
                return []
            try:
                if kind == "FEED_CONFIG":
                    self.profile.configure(message)
                else:
                    self.profile.data(message, received)
            except QuoteUnavailable as exc:
                return self._profile_down(str(exc))
            return []
        if kind in {"FEED_CONFIG", "FEED_DATA", "CHANNEL_OPENED"} and measure_channel:
            if kind == "CHANNEL_OPENED":
                if self.measure.state != "REQUESTED":
                    return []
                self.measure.opened()
                return [self.measure.setup_message()] + self._subscription(
                    MEASURE_CHANNEL, self.measure.kinds, list(self.service.identities.values()), reset=True)
            if self.measure.state != "OPEN":
                return []  # withheld measurement channel: never disturbs Quote/Trade
            try:
                if kind == "FEED_CONFIG":
                    self.measure.configure(message, aware(received))
                else:
                    self.measure.data(message, aware(received))
            except QuoteUnavailable as exc:
                return self._measure_down(str(exc), received)
            except Exception as exc:  # a recorder fault ends the measurement only
                self.measure.failed(type(exc).__name__)
                return self._measure_down("RECORDER_FAULT", received)
            return []
        if kind == "FEED_DATA" and channel == FEED_CHANNEL and self.phase == "STREAMING":
            self.decoder.data(message, received)
            return []
        if kind == "KEEPALIVE" and channel == 0:
            return []  # No quote timestamp or health revival here.
        raise QuoteUnavailable("DXLINK_PROTOCOL_UNEXPECTED")


def connect_ws(url, **kwargs):
    if not stream_endpoint_allowed(url):
        raise QuoteUnavailable("QUOTE_ENDPOINT_INVALID")
    try:
        from websockets.sync.client import connect
    except ImportError:
        raise QuoteUnavailable("QUOTES_DEPENDENCY_MISSING") from None
    logger = logging.getLogger("desk.dxlink.quiet")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    return connect(url, ssl=tls_context(), open_timeout=10, close_timeout=2,
                   max_size=2_000_000, max_queue=16, logger=logger, **kwargs)


def usable_at_end(view, components) -> dict:
    """Per symbol: whether each component was usable in the terminal view (D1).

    Quote/Trade: the existing getters accepted the value (age, BBO, schema, halt and
    identity rules unchanged). Profile: a live Profile in this session supplied the
    status. Receipt counters (``components``) never decide this.
    """
    out = {}
    for row in (view or {}).get("checks", []):
        status = row.get("trading_status", {}).get("reason")
        out[row["symbol"]] = {"Quote": row.get("quote", {}).get("status") == "AVAILABLE",
                              "Trade": row.get("trade", {}).get("status") == "AVAILABLE",
                              "Profile": status in {"PROFILE_ACTIVE", "PROFILE_HALTED"}}
    for symbol in components:
        out.setdefault(symbol, {"Quote": False, "Trade": False, "Profile": False})
    return out


def end_health(usable) -> str:
    """Aggregate wording for one attempt's terminal view; never a live PASS."""
    pairs = [u["Quote"] and u["Trade"] for u in usable.values()]
    if pairs and all(pairs):
        return "ALL_REQUESTED_QUOTE_AND_TRADE_USABLE_AT_END"
    if any(u["Quote"] or u["Trade"] for u in usable.values()):
        return "SOME_COMPONENTS_UNAVAILABLE_AT_END"
    return "NO_QUOTE_OR_TRADE_USABLE_AT_END"


def capture(client: ReadClient, service: QuoteService, *, seconds=30, reconnects=1,
            connect=connect_ws, clock=utcnow, monotonic=time.monotonic, sleep=time.sleep,
            on_observation=None, extend=None, profile=False, measure=None) -> dict:
    """Finite synchronous capture; can run in a caller-owned thread.

    A heartbeat timeout or transport failure may be retried within the same bounded
    attempt, duration and REST-request budget: old observations are cleared at once,
    and the retry needs a fresh token, a new generation and new events. Denial,
    protocol/schema, rate-limit and token-expiry faults stop. This function does not
    activate a desk runner; an all-day service and token renewal are future work.
    ``extend(session, service, received)`` may return extra subscriptions (each once).
    """
    if type(seconds) is not int or not 1 <= seconds <= 600 or type(reconnects) is not int or not 0 <= reconnects <= 2:
        raise QuoteUnavailable("INVALID_CAPTURE_BOUNDS")
    if service.environment != client.environment:
        service.disconnect("QUOTE_ENVIRONMENT_MISMATCH")
        raise QuoteUnavailable("QUOTE_ENVIRONMENT_MISMATCH")
    end, attempts, reason, observations = monotonic()+seconds, 0, None, []
    authenticated, configured, levels, faults, recoveries = False, False, [], [], []
    pending, attempt_log = None, []

    def coverage():
        """Per symbol: which components were received in the current generation (R4)."""
        seen = service.generation_events()
        return {symbol: {kind: seen.get(symbol, {}).get(kind, 0) > 0 for kind in (*CORE, *OPTIONAL)}
                for symbol in service.identities}

    def terminal():
        """The attempt's terminal view (child 3, D1): the existing getters at its end.

        Taken before a deliberate finite end closes anything, so values valid at that
        moment stay visible; after a fault it runs once the service has withdrawn its
        caches, so it reports the refusal and never revives a cleared value.
        """
        try:
            return service.inspect(clock())
        except Exception:  # a report must not hide the attempt's own outcome
            return {"checks": [], "error": "TERMINAL_VIEW_UNAVAILABLE"}

    def fail(code):
        service.disconnect(code)  # before any socket teardown wait
        raise QuoteUnavailable(code)

    while monotonic() < end and attempts <= reconnects:
        attempts += 1
        fault, before, generation, view, greek_terminal = None, sum(service.events.values()), None, None, None
        try:
            token = client.stream_token()
            levels.append(token.entitlement)
            session = Session(service, token, clock(), profile=profile, measure=measure)
            generation = session.generation
            with connect(token.url) as ws:
                ws.send(json.dumps(session.setup()))
                started = last_message = last_send = monotonic()
                streaming_since = None
                while monotonic() < end:
                    now = monotonic()
                    if clock() >= token.expires_at:
                        fail("QUOTE_TOKEN_EXPIRED")
                    if session.phase != "STREAMING" and now-started >= HANDSHAKE_SECONDS:
                        fail("DXLINK_HANDSHAKE_TIMEOUT")
                    if (streaming_since is not None and not session.schema_ready()
                            and now-streaming_since >= SCHEMA_SECONDS):
                        fail("DXLINK_SCHEMA_UNAVAILABLE")
                    if now-last_message >= session.keepalive*2:
                        fail("DXLINK_HEARTBEAT_TIMEOUT")
                    if now-last_send >= session.keepalive:
                        ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
                        last_send = now
                    try:
                        raw = ws.recv(timeout=min(0.5, max(0.001, end-now)))
                    except TimeoutError:
                        continue
                    except Exception:
                        fail("DXLINK_TRANSPORT_FAILURE")
                    try:
                        message = json_read(raw)
                    except QuoteUnavailable as exc:
                        fail(str(exc))
                    received = clock()
                    last_message = monotonic()
                    for response in session.receive(message, received):
                        ws.send(json.dumps(response))
                        last_send = monotonic()
                    if session.phase == "STREAMING" and streaming_since is None:
                        streaming_since = monotonic()
                    authenticated |= session.phase in {"CHANNEL", "STREAMING"}
                    configured |= session.schema_ready()
                    if session.phase == "STREAMING" and extend is not None:
                        for response in extend(session, service, received):
                            ws.send(json.dumps(response))
                            last_send = monotonic()
                    if message.get("type") == "FEED_DATA":
                        view = service.inspect(received)
                        observations.append(view)
                        if on_observation is not None:
                            on_observation(view)
                        # Bounded diagnostic artifacts, not raw frames or historical storage.
                        if len(observations) > 500:
                            observations.pop(0)
                view = terminal()  # deliberate end: before anything is closed
                reason = "CAPTURE_COMPLETE"
        except QuoteUnavailable as exc:
            fault = str(exc)
        except Exception:
            fault = "DXLINK_TRANSPORT_FAILURE"
        finally:
            try:
                if measure is not None:
                    try:
                        if fault is not None:
                            measure.down(fault, clock())  # a close fault cannot certify a previous value
                        if hasattr(measure, "greek_report"):
                            greek_terminal = measure.greek_report(clock())
                    except Exception:
                        greek_terminal = {"error": "GREEK_TERMINAL_VIEW_UNAVAILABLE", "checks": {}}
                    try:
                        measure.down(fault or reason or "DISCONNECTED", clock())
                    except Exception:
                        # A measurement report never prevents core quote disposal.
                        greek_terminal = {"error": "GREEK_TERMINAL_VIEW_UNAVAILABLE", "checks": {}}
            finally:
                service.disconnect(fault or reason or "DISCONNECTED")
        current = generation is not None and generation == service.generation
        if fault is not None:
            view = None  # e.g. the socket failed to close after the deadline: report the fault state
        if view is None and current:
            view = terminal()  # after the fault withdrew everything: the refusal state
        fresh = sum(service.events.values()) - before
        components = coverage() if current else {}
        usable = usable_at_end(view, components)
        attempt_log.append(dict(attempt=attempts, generation=generation, outcome=fault or reason,
                                deliberate_end=fault is None, fresh_events=fresh, components=components,
                                usable_at_end=usable, health=end_health(usable), terminal_view=view))
        if greek_terminal is not None:
            attempt_log[-1]["greek_terminal_state"] = dict(
                eligible=False, historical=True, note="As of attempt end; disconnected captures are not live state",
                view=greek_terminal)
        if pending is not None:
            # Receipt and current usability are separate facts. Recovered only if every
            # subscribed symbol's Quote and Trade were usable at the attempt's end under
            # the existing rules; Profile alone or one healthy peer is not recovery.
            received = bool(components) and all(c["Quote"] and c["Trade"] for c in components.values())
            ok = bool(usable) and all(u["Quote"] and u["Trade"] for u in usable.values())
            outcome = ("USABLE_AT_ATTEMPT_END" if ok else "RECEIVED_BUT_NOT_USABLE_AT_ATTEMPT_END" if received
                       else "PARTIALLY_RECEIVED" if any(c["Quote"] or c["Trade"] for c in components.values())
                       else "NOT_RECEIVED")
            recoveries.append(dict(after=pending, attempt=attempts, generation=generation, fresh_events=fresh,
                                   by_symbol=components, received_by_symbol=components, usable_by_symbol=usable,
                                   outcome=outcome, recovered=ok))
            pending = None
        if fault is None:
            break
        faults.append(dict(attempt=attempts, code=fault, recoverable=fault in RECOVERABLE))
        if fault not in RECOVERABLE or attempts > reconnects or monotonic() >= end:
            reason = fault
            break
        pending = fault
        sleep(min(2**(attempts-1), max(0, end-monotonic())))
    if reason is None:
        reason = faults[-1]["code"] if faults else "CAPTURE_DEADLINE"
    return dict(attempts=attempts, stop_reason=reason, deadline_reached=monotonic() >= end,
                faults=faults, recoveries=recoveries, attempt_log=attempt_log,
                final_attempt=attempt_log[-1] if attempt_log else None, observations=observations,
                requests=client.requests, connected_after_capture=service.connected,
                rest_authentication_seen=client._access is not None,
                streaming_authentication_seen=authenticated, field_schema_accepted=configured,
                schema={k: service.kind_state(k) for k in FIELDS}, profile_requested=profile,
                token_renewal="NOT_IMPLEMENTED_FOR_LONG_RUNNING_SERVICE", entitlement_levels=levels)
