"""Shared GET-only Tradier market-data transport. No analytics, account or order routes."""
from urllib.parse import urlencode
from contextlib import contextmanager
import threading
from desk.tastytrade_quotes import QuoteUnavailable, aware
from desk.tastytrade_transport import request_json, utcnow

HOSTS = {"production": "https://api.tradier.com", "sandbox": "https://sandbox.tradier.com"}
ROUTES = {"quotes": "/v1/markets/quotes", "expirations": "/v1/markets/options/expirations",
          "chains": "/v1/markets/options/chains"}
MAX_REQUESTS = 12   # diagnostic workload bound, not a trading limit
MARKET_DATA_PER_MINUTE = {"production": 120, "sandbox": 60}


class RequestBudgetExceeded(QuoteUnavailable):
    """Local collectors must raise this typed failure before sending a request.

    Unknown exceptions remain sanitized transport failures, never text-classified.
    """
    def __init__(self):
        super().__init__("REST_REQUEST_BUDGET")


class LocalRateLimitExceeded(RequestBudgetExceeded):
    def __init__(self, code="LOCAL_MARKET_DATA_RATE_LIMIT"):
        QuoteUnavailable.__init__(self, code)

class TradierClient:
    """GET-only market-data client on the audited transport (TLS, no redirects, safe errors)."""

    def __init__(self, token: str, *, environment="production", max_requests=MAX_REQUESTS, request=request_json,
                 clock=utcnow, operational=False, reserve_request=None):
        if environment not in HOSTS:
            raise QuoteUnavailable("ENVIRONMENT_INVALID")
        if not isinstance(token, str) or not token.strip():
            raise QuoteUnavailable("CREDENTIALS_MISSING")
        if type(max_requests) is not int or not 1 <= max_requests <= MAX_REQUESTS:
            raise QuoteUnavailable("REQUEST_BUDGET_INVALID")
        if type(operational) is not bool or (operational and not callable(reserve_request)):
            raise QuoteUnavailable("OPERATIONAL_REQUEST_POLICY_REQUIRED")
        self._token, self.environment, self.max_requests = token, environment, max_requests
        self._request, self.clock, self.requests, self.log = request, clock, 0, []
        self.operational, self.reserve_request = operational, reserve_request
        self._local, self._lock = threading.local(), threading.Lock()

    def use_operational_policy(self, reserve_request):
        """Explicit risk composition selects workload policy; never resets a counter."""
        if not callable(reserve_request):
            raise QuoteUnavailable("OPERATIONAL_REQUEST_POLICY_REQUIRED")
        with self._lock:
            if self.requests and not self.operational:
                raise QuoteUnavailable("USED_DIAGNOSTIC_CLIENT_CANNOT_CHANGE_POLICY")
            if self.requests and self.operational and self.reserve_request != reserve_request:
                raise QuoteUnavailable("OPERATIONAL_RATE_SCOPE_CHANGED")
            self.operational, self.reserve_request = True, reserve_request

    @contextmanager
    def quote_workload(self):
        """One explicit refresh permits one batched quote GET, not lifetime polling."""
        if getattr(self._local, "workload", None) is not None:
            raise QuoteUnavailable("NESTED_QUOTE_WORKLOAD_REFUSED")
        self._local.workload = {"used": False}
        try:
            yield
        finally:
            self._local.workload = None

    def accounting(self):
        return {"reserved_attempts": self.requests, "dispatch_attempts": len(self.log),
                "confirmed_network_sends": None, "network_send_status": "NOT_ATTESTED",
                "legacy_count_meaning": "reserved dispatch attempts, not confirmed network sends",
                "mode": "OPERATIONAL_WORKLOAD" if self.operational else "BOUNDED_DIAGNOSTIC"}

    def get(self, route: str, query: dict) -> dict:
        if route not in ROUTES:
            raise QuoteUnavailable("ROUTE_NOT_ALLOWED")
        at = aware(self.clock())
        with self._lock:
            if self.operational:
                workload = getattr(self._local, "workload", None)
                if workload is None or route != "quotes":
                    raise QuoteUnavailable("OPERATIONAL_QUOTE_WORKLOAD_REQUIRED")
                if workload["used"]:
                    raise RequestBudgetExceeded()
                self.reserve_request(self.environment, at)
                workload["used"] = True
            elif self.requests >= self.max_requests:
                raise RequestBudgetExceeded()
            self.requests += 1
            entry = {"route": ROUTES[route], "dispatch_attempted_at": at.isoformat(),
                     "network_send_status": "NOT_ATTESTED"}
            self.log.append(entry)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                   "User-Agent": "trading-desk-tradier-check/1"}
        try:
            reply = self._request("GET", HOSTS[self.environment] + ROUTES[route] + "?" + urlencode(query),
                                  headers, None)
        except QuoteUnavailable as exc:
            entry.update(received_at=aware(self.clock()).isoformat(), outcome=str(exc))
            raise
        except Exception:
            entry.update(received_at=aware(self.clock()).isoformat(), outcome="REST_TRANSPORT_FAILURE")
            raise QuoteUnavailable("REST_TRANSPORT_FAILURE") from None
        entry["received_at"] = aware(self.clock()).isoformat()
        # Tradier can answer HTTP 200 with a fault/errors body: never an observation.
        if not isinstance(reply, dict) or "fault" in reply or "errors" in reply:
            entry["outcome"] = "REST_ERROR"
            raise QuoteUnavailable("REST_ERROR")
        entry["outcome"] = "OK"
        return reply


def listed(value, *, text=False):
    """Tradier returns one item or a list; null means none. Text items only where documented."""
    if value is None:
        return []
    if isinstance(value, dict) or (text and isinstance(value, str)):
        return [value]
    if isinstance(value, list):
        return value
    raise QuoteUnavailable("REPLY_SHAPE_INVALID")


def wire_symbol(symbol: str) -> str:
    return symbol.replace(".", "/")  # observed: Tradier returned BRK/B (diagnostic pairing only)
