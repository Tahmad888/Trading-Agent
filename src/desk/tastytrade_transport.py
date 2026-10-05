"""Bounded read-only REST + DXLink transport; no account or order endpoints."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
import json
import logging
import ssl
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from desk.tastytrade_quotes import (FIELDS, UTC, FeedDecoder, QuoteService, QuoteUnavailable,
                                   aware, instrument)

HOSTS = {"production": "https://api.tastyworks.com", "sandbox": "https://api.cert.tastyworks.com"}


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


def request_json(method, url, headers, payload, *, timeout=15):
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        opener = build_opener(_NoRedirect(), HTTPSHandler(context=tls_context()))
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
        raise QuoteUnavailable(f"REST_HTTP_{exc.code}") from None
    except QuoteUnavailable:
        raise
    except Exception:
        raise QuoteUnavailable("REST_TRANSPORT_FAILURE") from None


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

    def _call(self, method, path, payload=None, authenticated=True):
        # Private allowlist also protects future callers from accidental account use.
        allowed = (path == "/api-quote-tokens" or path.startswith("/instruments/equities/")
                   or path.startswith("/instruments/equity-options/")
                   or (path.startswith("/option-chains/") and path.endswith("/nested")))
        if not ((method == "POST" and path == "/oauth/token" and not authenticated)
                or (method == "GET" and allowed and authenticated)):
            raise QuoteUnavailable("REST_ROUTE_REFUSED")
        if authenticated and (self._expires is None or self.clock() >= self._expires - timedelta(seconds=10)):
            self.authenticate()
        if self.requests >= self.max_requests:
            raise QuoteUnavailable("REST_REQUEST_BUDGET")
        self.requests += 1
        headers = {"User-Agent": "TradingDesk/0.1.0", "Accept": "application/json", "Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self._access
        try:
            result = self.request(method, HOSTS[self.environment] + path, headers, payload)
            if not isinstance(result, dict) or "error" in result:
                raise QuoteUnavailable("REST_ERROR")
            return result
        except QuoteUnavailable:
            raise
        except Exception:
            raise QuoteUnavailable("REST_TRANSPORT_FAILURE") from None

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
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise QuoteUnavailable("QUOTE_ENDPOINT_INVALID") from None
        if (parts.scheme != "wss" or not parts.hostname or parts.username or parts.password
                or not parts.hostname.endswith(".dxfeed.com")
                or parts.query or parts.fragment or port not in {None, 443}):
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
        symbol = canonical(requested, kind)
        provider_symbol = symbol.replace(".", "/") if kind == "Equity" else requested
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

    def chain_pair(self, underlying: str, *, strike: Decimal | None = None) -> tuple[str, str]:
        """Choose actual listed symbols for a diagnostic, never a trading plan.

        Nearest non-expired Standard expiry; caller's nearest strike or median listed
        strike. Both individual option responses still have to validate identity.
        """
        from desk.tastytrade_quotes import canonical, number
        symbol = canonical(underlying, "Equity")
        data = self._call("GET", "/option-chains/" + quote(symbol, safe="") + "/nested").get("data")
        try:
            options = []
            for chain in data["items"]:
                if chain.get("underlying-symbol") != symbol or chain.get("option-chain-type") != "Standard":
                    continue
                for expiry in chain["expirations"]:
                    from datetime import date
                    day = date.fromisoformat(expiry["expiration-date"])
                    if day < self.clock().date():
                        continue
                    for row in expiry["strikes"]:
                        if isinstance(row.get("call"), str) and isinstance(row.get("put"), str):
                            options.append((day, number(row["strike-price"], positive=True), row["call"], row["put"]))
            first = min(x[0] for x in options)
            options = sorted(x for x in options if x[0] == first)
            chosen = min(options, key=lambda x: abs(x[1]-strike)) if strike is not None else options[len(options)//2]
            return chosen[2], chosen[3]
        except (KeyError, TypeError, ValueError):
            raise QuoteUnavailable("OPTION_CHAIN_UNAVAILABLE") from None


class Session:
    """DXLink handshake state machine, independently testable without a socket."""
    def __init__(self, service: QuoteService, token: StreamToken, now: datetime):
        self.service, self.token = service, token
        self.decoder, self.phase = FeedDecoder(service), "SETUP"
        self.keepalive = 30.0
        service.begin(token.expires_at)

    @staticmethod
    def setup():
        return {"type": "SETUP", "channel": 0, "version": "0.1-DXF-JS/0.3.0",
                "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60}

    def receive(self, message: dict, received: datetime) -> list[dict]:
        try:
            return self._receive(message, received)
        except QuoteUnavailable as exc:
            # Invalidate before WebSocket context teardown can wait for close.
            self.service.disconnect(str(exc))
            raise

    def _receive(self, message: dict, received: datetime) -> list[dict]:
        kind, channel = message.get("type"), message.get("channel")
        if kind in {"ERROR", "CHANNEL_CLOSED"}:
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
                return [{"type": "CHANNEL_REQUEST", "channel": 3, "service": "FEED", "parameters": {"contract": "AUTO"}}]
            raise QuoteUnavailable("DXLINK_AUTH_STATE_INVALID")
        if kind == "CHANNEL_OPENED" and channel == 3 and self.phase == "CHANNEL":
            self.phase = "CONFIG"
            return [{"type": "FEED_SETUP", "channel": 3, "acceptAggregationPeriod": 0.1,
                     "acceptDataFormat": "COMPACT", "acceptEventFields": {k: list(v) for k,v in FIELDS.items()}}]
        if kind == "FEED_CONFIG" and channel == 3 and self.phase == "CONFIG":
            # Some servers send a preliminary fieldless config; wait within handshake deadline.
            if "eventFields" not in message:
                return []
            self.decoder.configure(message)
            self.service.ready(received)
            self.phase = "READY"
            return [{"type": "FEED_SUBSCRIPTION", "channel": 3, "reset": True,
                     "add": [{"type": k, "symbol": v.streamer_symbol}
                             for v in self.service.identities.values() for k in FIELDS]}]
        if kind == "FEED_DATA" and channel == 3 and self.phase == "READY":
            self.decoder.data(message, received)
            return []
        if kind == "KEEPALIVE" and channel == 0:
            return []  # No quote timestamp or health revival here.
        raise QuoteUnavailable("DXLINK_PROTOCOL_UNEXPECTED")


def connect_ws(url, **kwargs):
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


def capture(client: ReadClient, service: QuoteService, *, seconds=30, reconnects=1,
            connect=connect_ws, clock=utcnow, monotonic=time.monotonic, sleep=time.sleep,
            on_observation=None) -> dict:
    """Finite synchronous capture; can run in a caller-owned thread.

    Bounded reconnect obtains a new token/generation and fresh events. Permission,
    protocol/schema and rate-limit errors stop immediately. No catch-all retries.
    A caller-owned long-lived service may use the same Session/health interfaces;
    this function does not activate a desk runner.
    """
    if type(seconds) is not int or not 1 <= seconds <= 600 or type(reconnects) is not int or not 0 <= reconnects <= 2:
        raise QuoteUnavailable("INVALID_CAPTURE_BOUNDS")
    if service.environment != client.environment:
        service.disconnect("QUOTE_ENVIRONMENT_MISMATCH")
        raise QuoteUnavailable("QUOTE_ENVIRONMENT_MISMATCH")
    end, attempts, reason, observations = monotonic()+seconds, 0, None, []
    authenticated, configured, levels = False, False, []
    while monotonic() < end and attempts <= reconnects:
        attempts += 1
        try:
            token = client.stream_token()
            levels.append(token.entitlement)
            session = Session(service, token, clock())
            with connect(token.url) as ws:
                ws.send(json.dumps(session.setup()))
                started, last_message, last_send = monotonic(), monotonic(), monotonic()
                while monotonic() < end:
                    now = monotonic()
                    if clock() >= token.expires_at:
                        raise QuoteUnavailable("QUOTE_TOKEN_EXPIRED")
                    if session.phase != "READY" and now-started >= 10:
                        raise QuoteUnavailable("DXLINK_HANDSHAKE_TIMEOUT")
                    if now-last_message >= session.keepalive*2:
                        raise QuoteUnavailable("DXLINK_HEARTBEAT_TIMEOUT")
                    if now-last_send >= session.keepalive:
                        ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
                        last_send = now
                    try:
                        raw = ws.recv(timeout=min(0.5, max(0.001, end-now)))
                    except TimeoutError:
                        continue
                    except Exception:
                        service.disconnect("DXLINK_TRANSPORT_FAILURE")
                        raise
                    try:
                        message = json_read(raw)
                    except QuoteUnavailable as exc:
                        service.disconnect(str(exc))
                        raise
                    received = clock()
                    last_message = monotonic()
                    for response in session.receive(message, received):
                        ws.send(json.dumps(response))
                    authenticated |= session.phase in {"CHANNEL", "CONFIG", "READY"}
                    configured |= session.phase == "READY"
                    if message.get("type") == "FEED_DATA":
                        view = service.inspect(received)
                        observations.append(view)
                        if on_observation is not None:
                            on_observation(view)
                        # Bounded diagnostic artifacts, not raw frames or historical storage.
                        if len(observations) > 500:
                            observations.pop(0)
                reason = "CAPTURE_COMPLETE"
                break
        except QuoteUnavailable as exc:
            reason = str(exc)
            break
        except Exception:
            reason = "DXLINK_TRANSPORT_FAILURE"
            service.disconnect(reason)
            if attempts <= reconnects and monotonic() < end:
                sleep(min(2**(attempts-1), max(0, end-monotonic())))
        finally:
            service.disconnect(reason or "DISCONNECTED")
    return dict(attempts=attempts, stop_reason=reason or "CAPTURE_DEADLINE", observations=observations,
                requests=client.requests, connected_after_capture=service.connected,
                rest_authentication_seen=client._access is not None,
                streaming_authentication_seen=authenticated, field_schema_accepted=configured,
                entitlement_levels=levels)
