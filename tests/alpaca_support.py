"""Synthetic Alpaca provider for G5a checkpoint 2 tests (labelled fixtures, not Alpaca data).

``AlpacaSim`` answers the two documented read-only routes the desk uses, the paper
asset list and the SIP bars route, from per-symbol volume functions. It records
every URL it was sent; nothing here touches the network.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import uuid
from urllib import parse

import pandas as pd

from desk.alpaca_assets import IdentityStore
from desk.alpaca_source import AlpacaVolumeProvider, DecisionVolumeSource
from desk.alpaca_volume import AlpacaVolumeClient, HttpReply, RequestBudget, VolumeCache
from desk.calendar import ET, session, sessions

KEY, SECRET = "fixture-key-id-0000", "fixture-secret-9999"


def asset_id(symbol: str, salt: str = "") -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture-asset:" + symbol + salt))


def asset_row(symbol, *, id=None, status="active", cls="us_equity", exchange="NASDAQ", name=None):
    return {"id": id or asset_id(symbol), "class": cls, "exchange": exchange, "symbol": symbol,
            "name": name or f"{symbol} fixture asset", "status": status, "tradable": True}


def z(stamp) -> str:
    return pd.Timestamp(stamp).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


class AlpacaSim:
    """Answers GET /v2/assets and GET /v2/stocks/bars from volume functions.

    ``daily(symbol, day) -> volume`` and ``rth(symbol, stamp) -> volume``; a function
    returning None omits that bar. ``status`` is a queue of HTTP statuses to return
    before healthy replies (``bar_status``: bar requests only); ``malformed`` symbols get a negative volume row;
    ``page_rows`` splits bar replies into pages.
    """

    def __init__(self, *, symbols=(), daily=None, rth=None, assets=None, status=(), malformed=(),
                 page_rows=None, echo_key=False, bar_status=()):
        self.daily = daily or (lambda s, d: 2_000_000)
        self.rth = rth or (lambda s, t: 600_000)
        self.assets = assets if assets is not None else [asset_row(s) for s in symbols]
        self.status, self.malformed, self.page_rows = list(status), set(malformed), page_rows
        self.echo_key = echo_key
        self.bar_status = list(bar_status)        # statuses for bar requests only (asset list healthy)
        self.sent: list[str] = []
        self.methods: list[str] = []

    def __call__(self, req, timeout):
        url = req.full_url
        self.sent.append(url)
        self.methods.append(req.get_method())
        if self.status:
            return HttpReply(self.status.pop(0), {"x-request-id": "fixture"}, b"")
        parts = parse.urlsplit(url)
        query = dict(parse.parse_qsl(parts.query))
        if parts.netloc == "paper-api.alpaca.markets" and parts.path == "/v2/assets":
            body = json.dumps(self.assets).encode()
            if self.echo_key:
                body = body.replace(b"fixture asset", KEY.encode())
            return HttpReply(200, {"x-request-id": "fixture"}, body)
        assert parts.netloc == "data.alpaca.markets" and parts.path == "/v2/stocks/bars", url
        if self.bar_status:
            return HttpReply(self.bar_status.pop(0), {"x-request-id": "fixture"}, b"")
        start, end = pd.Timestamp(query["start"]), pd.Timestamp(query["end"])
        rows = {}
        for symbol in query["symbols"].split(","):
            out = []
            if query["timeframe"] == "1Day":
                for day in sessions(start.tz_convert(ET).date(), end.tz_convert(ET).date()):
                    stamp = pd.Timestamp(day).tz_localize(ET)
                    v = self.daily(symbol, day)
                    if start <= stamp <= end and v is not None:
                        out.append({"t": z(stamp), "o": 1, "h": 1, "l": 1, "c": 1, "v": v, "n": 1})
            else:
                for day in sessions(start.tz_convert(ET).date(), end.tz_convert(ET).date()):
                    opened, closed = session(day)
                    t = opened
                    while t < closed:
                        v = self.rth(symbol, t)
                        if start <= t <= end and v is not None:
                            out.append({"t": z(t), "v": v})
                        t += timedelta(minutes=15)
            if symbol in self.malformed and out:
                out[0] = {**out[0], "v": -1}
            if out:
                rows[symbol] = out
        token = query.get("page_token")
        if self.page_rows:
            flat = [(s, r) for s, rs in rows.items() for r in rs]
            at = int(token or 0)
            chunk = flat[at:at + self.page_rows]
            page: dict = {}
            for s, r in chunk:
                page.setdefault(s, []).append(r)
            following = str(at + self.page_rows) if at + self.page_rows < len(flat) else None
            body = {"bars": page, "next_page_token": following}
        else:
            body = {"bars": rows, "next_page_token": None}
        return HttpReply(200, {"x-request-id": "fixture"}, json.dumps(body).encode())

    def calls(self, kind=None) -> int:
        if kind == "assets":
            return sum("/v2/assets" in u for u in self.sent)
        if kind == "bars":
            return sum("/v2/stocks/bars" in u for u in self.sent)
        return len(self.sent)


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def provider(path, sim, clock, *, budget=6) -> AlpacaVolumeProvider:
    """A fresh run's provider over a persistent cache/identity file (``path``)."""
    cache = VolumeCache(path)
    client = AlpacaVolumeClient(KEY, SECRET, budget=RequestBudget(budget), transport=sim, clock_fn=clock,
                                cache=cache)
    return AlpacaVolumeProvider(client, IdentityStore(path))


def with_volume(source, path, sim, clock, *, budget=6) -> DecisionVolumeSource:
    return DecisionVolumeSource(source, provider(path, sim, clock, budget=budget))


def dry_daily(last: date, *, base=2_000_000, dry=1_000_000, days=10):
    """Native daily volume that dries up over the last ``days`` sessions through ``last``."""
    recent = set(sessions(last - timedelta(days=40), last)[-days:])
    return lambda symbol, day: dry if day in recent else base
