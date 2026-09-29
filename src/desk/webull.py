"""The desk's own Webull market-data connection (read only).

Blueprint v2.3/v2.4 step 3, serving the watch and analyze steps. Plain HTTPS
with Webull OpenAPI's HMAC-SHA1 request signing, so no SDK is needed. Only
market-data endpoints are here; this module has no order, account or trading
calls, and none may be added (CLAUDE.md: Webull is market data only).

Keys come from environment variables only (names in .env.example):
WEBULL_APP_KEY, WEBULL_APP_SECRET, and WEBULL_ACCESS_TOKEN when the app has
2FA on. Any HTTP error, timeout, rate limit or malformed reply raises
WebullError, a BarDataError, so the caller makes no plan (fail closed).

Sources (Sourced, fetched 29 Sep 2026): developer.webull.com/apis/docs/
authentication/signature (signing), reference/historical-bars (bars),
reference/snapshot, reference/earnings-calendar, rate-limits (60 requests a
minute per endpoint). Daily bars come forward-adjusted for dividends; minute
bars are unadjusted.

Plan B: Webull's official Python SDK (webull-openapi-python-sdk) or its
official MCP server, with the same keys; if Webull's API data is unavailable,
Alpaca's market-data API, whose free feed is IEX-only, so volumes differ.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from urllib import error, parse, request

import pandas as pd

from desk.bars import BarDataError, bars_from_webull

HOST = "api.webull.com"
SANDBOX_HOST = "api.sandbox.webull.com"   # Checked 29 Sep 2026: paper-trade keys work here, with delay_minutes 0
BARS_PATH = "/market-data/stocks/bars/list"
SNAPSHOT_PATH = "/market-data/stocks/snapshots/list"
EARNINGS_PATH = "/market-data/fundamentals/earnings-calendars/list"
# Rankings, top 200 each, not paginated (Sourced: webull-openapi-python-sdk 3.0.2, screener requests v2).
GAINERS_PATH = "/market-data/screeners/gainers-losers/list"
ACTIVES_PATH = "/market-data/screeners/top-actives/list"
GAINER_PERIODS = frozenset({"PRE_MARKET", "AFTER_MARKET", "MIN_3", "MIN_5", "DAY_1", "DAY_5",
                            "MONTH_1", "MONTH_3", "WEEK_52"})
ACTIVE_KINDS = frozenset({"VOLUME", "RELATIVE_VOLUME_10D", "TURNOVER", "TURNOVER_RATE", "AMPLITUDE"})
MAX_SYMBOLS_PER_BARS_CALL = 20
TIMESPANS = frozenset({"M1", "M5", "M15", "M30", "M60", "M120", "M240", "D", "W", "M", "Y"})


class WebullError(BarDataError):
    """Webull didn't give usable data. Means no trade."""


def _encode(s: str) -> str:
    return parse.quote(s, safe="")


def body_md5(body: str) -> str:
    return hashlib.md5(body.encode()).hexdigest().upper()


def compact_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def sign(*, path: str, query: Mapping[str, str], signing_headers: Mapping[str, str],
         body: str | None, app_secret: str) -> str:
    """Webull OpenAPI signature: base64(HMAC-SHA1(secret + "&", urlencode(path&params[&md5(body)])))."""
    params = {**{k: str(v) for k, v in query.items()}, **signing_headers}
    str1 = "&".join(f"{k}={params[k]}" for k in sorted(params))
    str3 = f"{path}&{str1}" + (f"&{body_md5(body)}" if body else "")
    digest = hmac.new((app_secret + "&").encode(), _encode(str3).encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


Transport = Callable[[request.Request, float], bytes]


def _urlopen(req: request.Request, timeout: float) -> bytes:
    with request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class WebullData:
    """Read-only Webull market data. Build it with from_env()."""

    def __init__(self, app_key: str, app_secret: str, access_token: str | None = None, *,
                 host: str = HOST, timeout: float = 10.0, min_interval: float = 1.05,
                 transport: Transport = _urlopen,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if not app_key or not app_secret:
            raise WebullError("Webull app key and secret are not set")
        self._key, self._secret, self._token = app_key, app_secret, access_token or None
        self._host, self._timeout, self._min_interval = host, timeout, min_interval
        self._transport, self._clock = transport, clock
        self._last_call: dict[str, float] = {}

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ, **kw) -> WebullData:
        kw.setdefault("host", env.get("WEBULL_HOST") or HOST)
        if kw["host"] not in (HOST, SANDBOX_HOST):
            raise WebullError(f"WEBULL_HOST must be {HOST} or {SANDBOX_HOST}")
        return cls(env.get("WEBULL_APP_KEY", ""), env.get("WEBULL_APP_SECRET", ""),
                   env.get("WEBULL_ACCESS_TOKEN") or None, **kw)

    def _headers(self, path: str, query: Mapping[str, str], body: str | None) -> dict[str, str]:
        signing = {
            "x-app-key": self._key,
            "x-signature-algorithm": "HMAC-SHA1",
            "x-signature-version": "1.0",
            "x-signature-nonce": uuid.uuid4().hex,
            "x-timestamp": self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": self._host,
        }
        headers = {**signing, "x-version": "v3",
                   "x-signature": sign(path=path, query=query, signing_headers=signing,
                                       body=body, app_secret=self._secret),
                   "Accept": "application/json"}
        if body:
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["x-access-token"] = self._token
        return headers

    def _call(self, method: str, path: str, query: Mapping[str, str] | None = None, payload=None):
        # Stay under 60 calls a minute per endpoint (Webull blocks IPs that keep hitting the limit).
        wait = self._last_call.get(path, 0.0) + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        query = {k: str(v) for k, v in (query or {}).items()}
        body = compact_json(payload) if payload is not None else None
        url = f"https://{self._host}{path}" + (f"?{parse.urlencode(query)}" if query else "")
        req = request.Request(url, data=body.encode() if body else None, method=method,
                              headers=self._headers(path, query, body))
        try:
            raw = self._transport(req, self._timeout)
        except error.HTTPError as e:
            hint = {401: "keys rejected", 403: "no OpenAPI market-data subscription",
                    429: "rate limit"}.get(e.code, "")
            raise WebullError(f"Webull HTTP {e.code} {hint}".strip()) from e
        except (error.URLError, TimeoutError, OSError) as e:
            raise WebullError(f"Webull unreachable: {e}") from e
        finally:
            self._last_call[path] = time.monotonic()
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as e:
            raise WebullError("Webull reply is not JSON") from e

    def bars(self, symbols: Sequence[str], *, category: str, timespan: str, count: int = 1000,
             sessions: str | None = None, max_delay_minutes: int = 0) -> dict[str, pd.DataFrame]:
        """Bars for up to 20 symbols, oldest first.

        A symbol missing from the reply, or data delayed more than
        max_delay_minutes (Webull's delay_minutes field), is an error.
        """
        if not 1 <= len(symbols) <= MAX_SYMBOLS_PER_BARS_CALL:
            raise WebullError(f"1 to {MAX_SYMBOLS_PER_BARS_CALL} symbols per call")
        if timespan not in TIMESPANS or category not in ("US_STOCK", "US_ETF"):
            raise WebullError("bad timespan or category")
        payload = {"symbols": list(symbols), "category": category, "timespan": timespan,
                   "count": count, "real_time_required": False}
        if sessions:
            payload["trading_sessions"] = sessions
        reply = self._call("POST", BARS_PATH, payload=payload)
        # Checked 29 Sep 2026: the reply is {"result": [{"symbol", "result": [bars], "delay_minutes"}]}.
        if isinstance(reply, Mapping):
            reply = reply.get("result")
        if not isinstance(reply, list):
            raise WebullError("unexpected bars reply")
        out = {}
        for item in reply:
            if isinstance(item, Mapping) and item.get("symbol") in symbols:
                try:
                    delay = int(item.get("delay_minutes", 0) or 0)
                except (TypeError, ValueError) as e:
                    raise WebullError("bad delay_minutes") from e
                if delay > max_delay_minutes:
                    raise WebullError(f"{item['symbol']} data is {delay} minutes delayed")
                out[item["symbol"]] = bars_from_webull(item.get("result") or [])
        missing = [s for s in symbols if s not in out]
        if missing:
            raise WebullError(f"no bars for {missing}")
        return out

    def snapshot(self, symbols: Sequence[str], *, category: str) -> list[dict]:
        reply = self._call("GET", SNAPSHOT_PATH, {"symbols": ",".join(symbols), "category": category,
                                                  "extend_hour_required": "false",
                                                  "overnight_required": "false"})
        if not isinstance(reply, list):
            raise WebullError("unexpected snapshot reply")
        return reply

    def earnings_calendar(self, symbol: str) -> list[dict]:
        """Past and expected reports for one US stock (ETFs aren't covered)."""
        reply = self._call("GET", EARNINGS_PATH, {"symbol": symbol, "category": "US_STOCK"})
        if not isinstance(reply, list):
            raise WebullError("unexpected earnings reply")
        return reply

    def _ranking(self, path: str, query: Mapping[str, str]) -> list[dict]:
        reply = self._call("GET", path, query)
        # Not yet seen live: accept a plain list or one wrapped in "data" or "result".
        if isinstance(reply, Mapping):
            reply = reply.get("data", reply.get("result"))
        if not isinstance(reply, list) or not all(isinstance(r, Mapping) and r.get("symbol") for r in reply):
            raise WebullError("unexpected ranking reply")
        return [dict(r) for r in reply]

    def gainers(self, period: str, *, losers: bool = False) -> list[dict]:
        """Top 200 US stocks by price change over a period (DAY_1, MONTH_3, ...)."""
        if period not in GAINER_PERIODS:
            raise WebullError(f"period must be one of {sorted(GAINER_PERIODS)}")
        return self._ranking(GAINERS_PATH, {"rank_type": period, "category": "US_STOCK",
                                            "sort_by": "CHANGE_RATIO", "direction": "ASC" if losers else "DESC"})

    def most_active(self, by: str = "TURNOVER") -> list[dict]:
        """Top 200 US stocks by trading activity (TURNOVER is dollar volume)."""
        if by not in ACTIVE_KINDS:
            raise WebullError(f"by must be one of {sorted(ACTIVE_KINDS)}")
        return self._ranking(ACTIVES_PATH, {"category": "US_STOCK", "rank_type": by})
