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
from desk.bar_contract import BarProvenance
from desk.data_basis import VolumeBasis

HOST = "api.webull.com"
SANDBOX_HOST = "api.sandbox.webull.com"   # Sandbox delay_minutes=0 is NOT proof of real-time data.
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
MINUTE_TIMESPANS = frozenset({"M1", "M5", "M15", "M30", "M60", "M120", "M240"})
TRADING_SESSIONS = frozenset({"PRE", "RTH", "ATH", "OVN"})


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
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 bar_profile: Callable[[str, str], BarProvenance | None] | None = None,
                 volume_profile: Callable[[str, str], VolumeBasis | None] | None = None,
                 bar_timestamp_unit: str | None = None):
        if not app_key or not app_secret:
            raise WebullError("Webull app key and secret are not set")
        self._key, self._secret, self._token = app_key, app_secret, access_token or None
        self._host, self._timeout, self._min_interval = host, timeout, min_interval
        self._transport, self._clock = transport, clock
        self._bar_profile, self._bar_timestamp_unit = bar_profile, bar_timestamp_unit
        self._volume_profile = volume_profile
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
            # HTTPError owns a response body too; translating it must close that body.
            with e:
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
        payload = {"symbols": list(symbols), "category": category, "timespan": timespan,
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
