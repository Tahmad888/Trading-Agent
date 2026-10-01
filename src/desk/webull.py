"""The desk's own Webull market-data connection (read only).

Blueprint v2.3/v2.4 step 3, serving the watch and analyze steps. Plain HTTPS
with Webull OpenAPI's HMAC-SHA1 request signing, so no SDK is needed. Only
market data and read-only security reference lookups are here. No order or account
operations are allowed (CLAUDE.md: Webull is market data only).

Keys come from environment variables only (names in .env.example):
WEBULL_APP_KEY, WEBULL_APP_SECRET, and WEBULL_ACCESS_TOKEN when the app has
2FA on. Any HTTP error, timeout, rate limit or malformed reply raises
WebullError, a BarDataError, so the caller makes no plan (fail closed).

Sources (Sourced, fetched 29 Sep 2026): developer.webull.com/apis/docs/
authentication/signature (signing), reference/historical-bars (bars),
reference/snapshot, reference/earnings-calendar, rate-limits (production 60 and
sandbox 30 requests a minute per app key/endpoint, checked 1 Oct 2026). Daily bars come forward-adjusted for dividends; minute
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
import math
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from urllib import error, parse, request

import pandas as pd

from desk.bars import BarDataError, bars_from_webull
from desk.bar_contract import BarProvenance
from desk.data_basis import VolumeBasis
from desk.symbols import canonical_symbol, webull_symbol, WEBULL_IDENTITIES

HOST = "api.webull.com"
SANDBOX_HOST = "api.sandbox.webull.com"   # Sandbox delay_minutes=0 is NOT proof of real-time data.
BARS_PATH = "/market-data/stocks/bars/list"
SNAPSHOT_PATH = "/market-data/stocks/snapshots/list"
INCOME_PATH = "/market-data/fundamentals/income-statements/get"
EARNINGS_PATH = "/market-data/fundamentals/earnings-calendars/list"
DIVIDENDS_PATH = "/market-data/fundamentals/dividend-calendars/list"
FUND_SPLITS_PATH = "/market-data/fundamentals/fund-splits/get"
DISPLAY_ACTIONS_PATH = "/market-data/instruments/stocks/corporate-actions/list"
# Rankings, top 200 each, not paginated (Sourced: webull-openapi-python-sdk 3.0.2, screener requests v2).
GAINERS_PATH = "/market-data/screeners/gainers-losers/list"
ACTIVES_PATH = "/market-data/screeners/top-actives/list"
INSTRUMENTS_PATH = "/trading/instruments/stocks/profiles/list"  # GET reference data only
GAINER_PERIODS = frozenset({"PRE_MARKET", "AFTER_MARKET", "MIN_3", "MIN_5", "DAY_1", "DAY_5",
                            "MONTH_1", "MONTH_3", "WEEK_52"})
ACTIVE_KINDS = frozenset({"VOLUME", "RELATIVE_VOLUME_10D", "TURNOVER", "TURNOVER_RATE", "AMPLITUDE"})
MAX_SYMBOLS_PER_BARS_CALL = 20
TIMESPANS = frozenset({"M1", "M5", "M15", "M30", "M60", "M120", "M240", "D", "W", "M", "Y"})
MINUTE_TIMESPANS = frozenset({"M1", "M5", "M15", "M30", "M60", "M120", "M240"})
TRADING_SESSIONS = frozenset({"PRE", "RTH", "ATH", "OVN"})


class WebullError(BarDataError):
    """Webull didn't give usable data. Means no trade."""


class WebullHTTPError(WebullError):
    """Sanitized route diagnostics; never include credentials/query/body."""
    def __init__(self, status: int, host: str, path: str):
        self.status, self.host, self.path = status, host, path
        hint = {401: "credentials/authentication rejected",
                403: "access denied; verify product entitlement and environment",
                404: "route unavailable on this host; verify API product/version",
                429: "rate limit; quota is shared by app key and endpoint"}.get(status, "provider failure")
        if status == 404 and path == DISPLAY_ACTIONS_PATH:
            hint += "; this path is documented under Display Solution, not retail Non-Display"
        super().__init__(f"Webull HTTP {status} {host}{path}: {hint}")


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
                 host: str = HOST, timeout: float = 10.0, min_interval: float | None = None,
                 transport: Transport = _urlopen,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 bar_profile: Callable[[str, str], BarProvenance | None] | None = None,
                 volume_profile: Callable[[str, str], VolumeBasis | None] | None = None,
                 bar_timestamp_unit: str | None = None):
        if not app_key or not app_secret:
            raise WebullError("Webull app key and secret are not set")
        if host not in (HOST, SANDBOX_HOST):
            raise WebullError("Unsupported Webull retail host; a different API product needs its own connection")
        if min_interval is None:
            min_interval = 2.1 if host == SANDBOX_HOST else 1.05
        if isinstance(min_interval, bool) or not isinstance(min_interval, (int, float)) or not math.isfinite(min_interval) or min_interval < 0:
            raise WebullError("min_interval must be finite and nonnegative")
        self._key, self._secret, self._token = app_key, app_secret, access_token or None
        self._host, self._timeout, self._min_interval = host, timeout, min_interval
        self._transport, self._clock = transport, clock
        self._bar_profile, self._bar_timestamp_unit = bar_profile, bar_timestamp_unit
        self._volume_profile = volume_profile
        self._last_call: dict[str, float] = {}
        self._security_cache: dict[str, tuple[float, dict]] = {}

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
        # Sourced: sandbox 30/60s; production 60/60s per app key and endpoint.
        # Other processes sharing this key still consume the same provider quota.
        wait = self._last_call[path] + self._min_interval - time.monotonic() if path in self._last_call else 0
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
            # HTTPError owns a response body too; translating it must close that body.
            with e:
                raise WebullHTTPError(e.code, self._host, path) from e
        except (error.URLError, TimeoutError, OSError) as e:
            raise WebullError(f"Webull unreachable: {e}") from e
        finally:
            self._last_call[path] = time.monotonic()
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as e:
            raise WebullError("Webull reply is not JSON") from e

    def bars(self, symbols: Sequence[str], *, category: str, timespan: str, count: int = 1000,
             sessions: str | None = None, max_delay_minutes: int = 0,
             start_time: int | None = None, end_time: int | None = None,
             real_time_required: bool = True) -> dict[str, pd.DataFrame]:
        """Bars for up to 20 symbols, oldest first.

        A symbol missing from the reply, or data delayed more than
        max_delay_minutes (Webull's delay_minutes field), is an error.
        That field alone does not establish freshness. Minute bars default to RTH;
        explicit extended sessions are for diagnostics, without a decision profile.
        Optional bounds are inclusive UTC epoch milliseconds applied to bar labels,
        not a guarantee of complete coverage or completed bars. The real-time flag
        controls the request, not entitlement or independent completion checks.
        """
        symbols = list(dict.fromkeys(canonical_symbol(s) for s in symbols))
        if not 1 <= len(symbols) <= MAX_SYMBOLS_PER_BARS_CALL:
            raise WebullError(f"1 to {MAX_SYMBOLS_PER_BARS_CALL} symbols per call")
        if timespan not in TIMESPANS or category not in ("US_STOCK", "US_ETF"):
            raise WebullError("bad timespan or category")
        if type(real_time_required) is not bool:
            raise WebullError("real_time_required must be a boolean")
        minute_bars = timespan in MINUTE_TIMESPANS
        if sessions is None and minute_bars:
            sessions = "RTH"
        selected = None
        if sessions is not None:
            if not isinstance(sessions, str):
                raise WebullError("bad trading sessions")
            selected = sessions.split(",")
            if not selected or len(set(selected)) != len(selected) or not set(selected) <= TRADING_SESSIONS:
                raise WebullError("bad trading sessions")
        bounds = {}
        for name, value in (("start_time", start_time), ("end_time", end_time)):
            if value is not None:
                if type(value) is not int or value < 0:
                    raise WebullError(f"{name} must be non-negative integer epoch milliseconds")
                try:
                    bounds[name] = pd.to_datetime(value, unit="ms", utc=True)
                except (ValueError, OverflowError) as exc:
                    raise WebullError(f"{name} outside supported timestamp range") from exc
        if start_time is not None and end_time is not None and start_time > end_time:
            raise WebullError("start_time must not follow end_time")
        payload = {"symbols": [webull_symbol(s) for s in symbols], "category": category, "timespan": timespan,
                   "count": count, "real_time_required": real_time_required}
        if selected is not None:
            payload["trading_sessions"] = sessions
        if start_time is not None:
            payload["start_time"] = start_time
        if end_time is not None:
            payload["end_time"] = end_time
        reply = self._call("POST", BARS_PATH, payload=payload)
        # Checked 29 Sep 2026: the reply is {"result": [{"symbol", "result": [bars], "delay_minutes"}]}.
        if isinstance(reply, Mapping):
            reply = reply.get("result")
        if not isinstance(reply, list):
            raise WebullError("unexpected bars reply")
        out = {}
        for item in reply:
            if isinstance(item, Mapping) and isinstance(item.get("symbol"), str):
                item = self._normalize_identity(item)
            if isinstance(item, Mapping) and item.get("symbol") in symbols:
                try:
                    raw_delay = item["delay_minutes"]
                    if isinstance(raw_delay, bool) or not (type(raw_delay) is int or
                            isinstance(raw_delay, str) and raw_delay.isdigit()):
                        raise ValueError("missing/non-integer delay")
                    delay = int(raw_delay)
                    if delay < 0:
                        raise ValueError("negative delay")
                except (KeyError, TypeError, ValueError) as e:
                    raise WebullError("bad delay_minutes") from e
                if delay > max_delay_minutes:
                    raise WebullError(f"{item['symbol']} data is {delay} minutes delayed")
                symbol = item["symbol"]
                if symbol in out:
                    raise WebullError("duplicate symbol in bars reply")
                rows = item.get("result") or []
                if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
                    raise WebullError("unexpected bar rows")
                if minute_bars and any(not isinstance(row.get("trading_session"), str) or
                                       row["trading_session"] not in selected for row in rows):
                    raise WebullError("missing or unexpected intraday trading_session")
                df = bars_from_webull(rows, timestamp_unit=self._bar_timestamp_unit)
                if "start_time" in bounds:
                    df = df[df.index >= bounds["start_time"]].copy()
                if "end_time" in bounds:
                    df = df[df.index <= bounds["end_time"]].copy()
                if df.empty:
                    raise WebullError(f"no bars for {symbol} in requested window")
                df.attrs["webull_request"] = dict(payload)
                df.attrs["provider_identity_raw"] = {"symbol": item["provider_symbol"], "instrument_id": item.get("instrument_id")}
                df.attrs["provider_identity"] = {"symbol": symbol, "instrument_id": item.get("instrument_id")}
                # This identifies the native channel, not its undocumented trade
                # inclusion or share-adjustment rules. No cross-channel equivalence.
                df.attrs["volume_basis"] = {"source": "Webull OpenAPI",
                    "evidence_ref": "https://developer.webull.com/apis/docs/reference/historical-bars/",
                    "channel": "minute:" + ",".join(selected) if minute_bars else "native:" + timespan,
                    "definition_id": None, "units": "shares", "share_basis_id": None}
                if minute_bars:
                    df.attrs["provider_sessions"] = sorted({row["trading_session"] for row in rows})
                profile = self._bar_profile(symbol, timespan) if self._bar_profile else None
                if minute_bars and selected != ["RTH"]:
                    profile = None  # extended-session diagnostics cannot claim a regular-only profile
                volume = self._volume_profile(symbol, timespan) if self._volume_profile else None
                if volume and (not minute_bars or selected == ["RTH"]):
                    df.attrs["volume_basis"] = volume.model_dump(mode="json")
                if profile and profile.price_basis and (
                    profile.price_basis.symbol != symbol or item.get("instrument_id") is None or
                    profile.price_basis.security_id != str(item["instrument_id"])
                ):
                    raise WebullError("Price evidence does not match returned Webull instrument identity")
                # Unknown provider semantics remain readable, but cannot produce a signal.
                df.attrs["bar_provenance"] = (profile.model_copy(update={"delay_minutes": delay}).model_dump()
                    if profile else {"source": "Webull OpenAPI", "timeframe": timespan, "delay_minutes": delay,
                                     "timestamp_semantics": "unknown"})
                df.attrs["received_at"] = self._clock().isoformat()
                out[symbol] = df
        missing = [s for s in symbols if s not in out]
        if missing:
            raise WebullError(f"no bars for {missing}")
        return out

    @staticmethod
    def _normalize_identity(row):
        raw_symbol = row.get("symbol")
        if not isinstance(raw_symbol, str):
            raise WebullError("invalid provider symbol")
        symbol = canonical_symbol(raw_symbol)
        reviewed = WEBULL_IDENTITIES.get(symbol)
        if reviewed and row.get("instrument_id") != reviewed[1]:
            raise WebullError("Webull alias instrument identity differs from reviewed mapping")
        return {**row, "symbol": symbol, "provider_symbol": raw_symbol}

    def security_metadata(self, symbols: Sequence[str]) -> list[dict]:
        """Read-only instrument lookup; five-minute in-process cache, no option gate.

        Sourced: retail instrument-list docs and official SDK 3.0.2 request v3.
        Explicit-symbol pagination is followed without silently accepting truncation.
        """
        if any(not isinstance(s, str) or not s.strip() for s in symbols):
            raise WebullError("instrument lookup requires ticker strings")
        names = list(dict.fromkeys(canonical_symbol(s) for s in symbols))
        if not names or len(names) > 100 or any(not isinstance(n, str) or not n for n in names):
            raise WebullError("instrument lookup requires 1..100 symbols")
        current = time.monotonic()
        needed = [n for n in names if n not in self._security_cache or current - self._security_cache[n][0] >= 300]
        received = []
        cursor, seen = None, set()
        if needed:
            while True:
                query = {"symbols": ",".join(webull_symbol(s) for s in needed), "category": "US_STOCK"}
                if cursor:
                    query["pagination_key"] = cursor
                reply = self._call("GET", INSTRUMENTS_PATH, query)
                rows = reply.get("data") if isinstance(reply, dict) else reply
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise WebullError("unexpected instrument reference response")
                received.extend({**self._normalize_identity(row), "observed_at": self._clock().isoformat()} for row in rows)
                cursor = reply.get("pagination_key") if isinstance(reply, dict) else None
                if not cursor:
                    break
                if not isinstance(cursor, str) or cursor in seen or len(seen) >= 100:
                    raise WebullError("invalid instrument pagination")
                seen.add(cursor)
            # Only cache unambiguous identities; callers validate every row's fields.
            unambiguous = all(row.get("symbol") in needed for row in received) and len({row.get("symbol") for row in received}) == len(received)
            for name in needed if unambiguous else []:
                matches = [row for row in received if row.get("symbol") == name]
                if len(matches) == 1:
                    self._security_cache[name] = (time.monotonic(), matches[0])
        return received + [self._security_cache[n][1] for n in names if n not in needed]

    def snapshot(self, symbols: Sequence[str], *, category: str) -> list[dict]:
        reply = self._call("GET", SNAPSHOT_PATH, {"symbols": ",".join(webull_symbol(s) for s in symbols), "category": category,
                                                  "extend_hour_required": "false",
                                                  "overnight_required": "false"})
        if not isinstance(reply, list):
            raise WebullError("unexpected snapshot reply")
        return reply

    def earnings_calendar(self, symbol: str) -> list[dict]:
        """Past and expected reports for one US stock (ETFs aren't covered)."""
        reply = self._call("GET", EARNINGS_PATH, {"symbol": webull_symbol(symbol), "category": "US_STOCK"})
        if not isinstance(reply, list):
            raise WebullError("unexpected earnings reply")
        return reply

    def quarterly_income(self, symbol: str, *, count: int = 5):
        """Raw observations only; SDK 3.0.2 route. No inferred accounting/time basis."""
        if type(count) is not int or not 1 <= count <= 20:
            raise WebullError("income observation count must be 1..20")
        if not isinstance(symbol, str) or not symbol.strip():
            raise WebullError("income observations require a symbol")
        return self._call("GET", INCOME_PATH, {"symbol": webull_symbol(symbol),
                          "category": "US_STOCK", "type": "QUARTERLY", "count": str(count)})

    def dividend_calendar(self, symbol: str) -> list[dict]:
        """Recent stock dividends only; NOT complete action or split coverage."""
        return self._partial_action_rows(DIVIDENDS_PATH, symbol, "US_STOCK")

    def fund_splits(self, symbol: str) -> list[dict]:
        """Fund-only partial evidence; never use this endpoint to attest stock splits."""
        # The official Fundamentals.get_fund_splits uses US_STOCK for US funds.
        # Its category vocabulary is not inferred from the bars endpoint.
        return self._partial_action_rows(FUND_SPLITS_PATH, symbol, "US_STOCK")

    def _partial_action_rows(self, path: str, symbol: str, category: str) -> list[dict]:
        if not isinstance(symbol, str) or not symbol.strip():
            raise WebullError("A nonempty symbol is required")
        reply = self._call("GET", path, {"symbol": webull_symbol(symbol), "category": category})
        if not isinstance(reply, list) or not all(isinstance(row, dict) for row in reply):
            raise WebullError("Unexpected partial corporate-action reply")
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
