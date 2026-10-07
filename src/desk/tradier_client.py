"""Shared GET-only Tradier market-data transport. No analytics, account or order routes."""
from urllib.parse import urlencode
from desk.tastytrade_quotes import QuoteUnavailable, aware
from desk.tastytrade_transport import request_json, utcnow

HOSTS = {"production": "https://api.tradier.com", "sandbox": "https://sandbox.tradier.com"}
ROUTES = {"quotes": "/v1/markets/quotes", "expirations": "/v1/markets/options/expirations",
          "chains": "/v1/markets/options/chains"}
MAX_REQUESTS = 12   # diagnostic workload bound, not a trading limit


class RequestBudgetExceeded(QuoteUnavailable):
    """Local collectors must raise this typed failure before sending a request.

    Unknown exceptions remain sanitized transport failures, never text-classified.
    """
    def __init__(self):
        super().__init__("REST_REQUEST_BUDGET")

class TradierClient:
    """GET-only market-data client on the audited transport (TLS, no redirects, safe errors)."""

    def __init__(self, token: str, *, environment="production", max_requests=MAX_REQUESTS, request=request_json,
                 clock=utcnow):
        if environment not in HOSTS:
            raise QuoteUnavailable("ENVIRONMENT_INVALID")
        if not isinstance(token, str) or not token.strip():
            raise QuoteUnavailable("CREDENTIALS_MISSING")
        if type(max_requests) is not int or not 1 <= max_requests <= MAX_REQUESTS:
            raise QuoteUnavailable("REQUEST_BUDGET_INVALID")
        self._token, self.environment, self.max_requests = token, environment, max_requests
        self._request, self.clock, self.requests, self.log = request, clock, 0, []

    def get(self, route: str, query: dict) -> dict:
        if route not in ROUTES:
            raise QuoteUnavailable("ROUTE_NOT_ALLOWED")
        if self.requests >= self.max_requests:
            raise RequestBudgetExceeded()
        self.requests += 1
        entry = {"route": ROUTES[route], "sent_at": aware(self.clock()).isoformat()}
        self.log.append(entry)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                   "User-Agent": "trading-desk-tradier-check/1"}
        try:
            reply = self._request("GET", HOSTS[self.environment] + ROUTES[route] + "?" + urlencode(query),
                                  headers, None)
        except QuoteUnavailable as exc:
            entry.update(received_at=aware(self.clock()).isoformat(), outcome=str(exc))
            raise
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
