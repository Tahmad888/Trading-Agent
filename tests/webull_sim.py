"""Synthetic Webull OpenAPI for G5a checkpoint 3 tests (labelled fixtures, not Webull data).

``WebullSim`` is a transport for the real ``WebullData`` client. It answers the bars,
instrument-profile and ranking routes from per-symbol daily series, so the whole
parser, vendor wrapper, scanner and scan-log path runs unchanged. Nothing here
touches the network. Rows go out newest first, as Webull's do.

Provider behaviors (``mode``): ``bounded`` honors ``start_time``, ``end_time`` and
``count``; ``ignores_start`` honors only ``end_time`` and ``count``; ``excess``
ignores ``start_time`` and ``count`` and returns up to 1000 rows (excess old rows). ``cap[symbol]`` returns at
most that many of the newest rows (a provider that stops short).
"""
from __future__ import annotations

from datetime import date, timedelta
import json
import math
from urllib import parse

import pandas as pd

from desk.calendar import ET, session, sessions
from desk.webull import ACTIVES_PATH, BARS_PATH, GAINERS_PATH, INSTRUMENTS_PATH


def label(day: date) -> str:
    return pd.Timestamp(day).tz_localize(ET).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def utc(stamp) -> str:
    return pd.Timestamp(stamp).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def ms(value) -> pd.Timestamp:
    return pd.Timestamp(int(value), unit="ms", tz="UTC")


def series(end: date, n: int, *, start=50.0, growth=0.0015, wave=0.02, period=9.0, volume=2_000_000):
    """``n`` sessions ending at ``end``: {day: (open, high, low, close, volume)}."""
    days = sessions(end - timedelta(days=2 * n + 40), end)[-n:]
    out = {}
    for t, day in enumerate(days):
        close = round(start * math.exp(growth * t) * (1 + wave * math.sin(t / period)), 4)
        open_ = round(close * (1 - 0.004 * math.sin(t / 3.0)), 4)
        out[day] = (open_, round(max(open_, close) * 1.008, 4), round(min(open_, close) * 0.992, 4), close, volume)
    return out


class WebullSim:
    def __init__(self, *, mode="bounded"):
        self.mode = mode
        self.world: dict[str, dict] = {}
        self.lists: dict[str, list] = {}
        self.extra: dict[str, list] = {}         # raw rows appended to a symbol's daily reply
        self.short_anchor: set[str] = set()
        self.wrong_id: dict[str, str] = {}
        self.delay: dict[str, int] = {}
        self.requests: list[tuple[str, dict]] = []
        self.cap: dict[str, int] = {}             # at most this many newest daily rows for a symbol

    def add(self, symbol, ohlcv: dict, *, sub="COMMON_STOCK", instrument_id=None):
        self.world[symbol] = {"id": instrument_id or f"wb:{symbol}", "sub": sub,
                              "rows": {d: {"time": label(d), **dict(zip(("open", "high", "low", "close", "volume"),
                                                                         (str(v) for v in row)))}
                                       for d, row in ohlcv.items()}}
        return self

    def row(self, symbol, day) -> dict:
        return self.world[symbol]["rows"][day]

    def drop(self, symbol, *days):
        for day in days:
            self.world[symbol]["rows"].pop(day)

    def universe(self, *names, price=150):
        self.lists["MONTH_3"] = [{"symbol": s, "price": price} for s in names]

    def daily_requests(self):
        return [b for p, b in self.requests if p == BARS_PATH and b["timespan"] == "D"]

    def __call__(self, req, timeout):
        parts = parse.urlsplit(req.full_url)
        query = dict(parse.parse_qsl(parts.query))
        if parts.path == INSTRUMENTS_PATH:
            self.requests.append((parts.path, query))
            names = query["symbols"].split(",")
            data = [{"symbol": s, "instrument_id": self.world[s]["id"], "name": s, "currency": "USD",
                     "category": "US_STOCK", "sub_category": self.world[s]["sub"], "exchange_code": "TEST"}
                    for s in names if s in self.world]
            return json.dumps({"data": data}).encode()
        if parts.path in (GAINERS_PATH, ACTIVES_PATH):
            self.requests.append((parts.path, query))
            key = query.get("rank_type")
            return json.dumps(self.lists.get(key, []) if query.get("direction") != "ASC" else []).encode()
        assert parts.path == BARS_PATH, req.full_url
        body = json.loads(req.data)
        self.requests.append((parts.path, body))
        out = []
        for symbol in body["symbols"]:
            if symbol not in self.world:
                continue
            world = self.world[symbol]
            end = ms(body["end_time"]) if "end_time" in body else None
            start = ms(body["start_time"]) if "start_time" in body else None
            if body["timespan"] == "D":
                items = [(pd.Timestamp(d).tz_localize(ET), dict(r)) for d, r in sorted(world["rows"].items())]
                items = [(t, r) for t, r in items if end is None or t <= end]
                if self.mode == "bounded" and start is not None:
                    items = [(t, r) for t, r in items if t >= start]
                rows = [r for _, r in items][-(1000 if self.mode == "excess" else body["count"]):]
                if symbol in self.cap:
                    rows = rows[-self.cap[symbol]:]
                rows += [dict(r) for r in self.extra.get(symbol, [])]
            else:
                rows = []
                for day in sessions(start.tz_convert(ET).date(), end.tz_convert(ET).date()):
                    opened, closed = session(day)
                    close = float(world["rows"][day]["close"])
                    bars = pd.date_range(opened, closed - pd.Timedelta(minutes=15), freq="15min")
                    if symbol in self.short_anchor:
                        bars = bars[:-1]
                    for i, t in enumerate(bars):
                        c = close if i == len(bars) - 1 else round(close * (1 + 0.001 * math.sin(i)), 4)
                        rows.append({"time": utc(t), "open": str(c), "high": str(round(c * 1.002, 4)),
                                     "low": str(round(c * 0.998, 4)), "close": str(c), "volume": "1000",
                                     "trading_session": "RTH"})
                rows = rows[-body["count"]:]
            out.append({"symbol": symbol, "instrument_id": self.wrong_id.get(symbol, world["id"]),
                        "delay_minutes": self.delay.get(symbol, 0), "result": list(reversed(rows))})
        return json.dumps({"result": out}).encode()
