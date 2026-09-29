"""The desk-built watchlist: the Friday leader scan plus the day's movers.

Blueprint v2.4 section 3, step 4; serves the watch step. No fixed favourites
list (Taz, 29 Sep 2026): the list is the weekly leaders that pass the Trend
Template, the day's news and earnings movers, SPY, QQQ and IWM always, and any
ticker Taz adds. Taz can remove anything on Fridays.

Plan B (blueprint section 13): if pulling ~1,000 names' daily bars a week hits
data limits, the leader scan ranks only the S&P 500 and Nasdaq-100 members;
if that fails too, it pauses, the Friday note says so, and last week's list
keeps running.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import pandas as pd

from desk.bars import BarDataError
from desk.indicators import daily_features
from desk.playbook.filters import trend_template

ALWAYS = ("SPY", "QQQ", "IWM")
# The leader scan's universe: Webull's top-200 lists, merged. [Assumption] Together they cover the
# liquid names that moved most over 1 week to 1 year; price and volume are checked on our own bars.
UNIVERSE_LISTS = (("gainers", "DAY_5"), ("gainers", "MONTH_1"), ("gainers", "MONTH_3"),
                  ("gainers", "WEEK_52"), ("most_active", "TURNOVER"), ("most_active", "VOLUME"))
MOVER_LISTS = (("gainers", "PRE_MARKET"), ("gainers", "DAY_1"), ("most_active", "RELATIVE_VOLUME_10D"))
MIN_GAP = 0.10                   # [Sourced] Kullamägi: episodic pivots gap 10% or more
MIN_PRICE = 10.0                 # [Sourced] blueprint section 3: price above $10
MIN_AVG_VOLUME = 1_000_000       # [Sourced] blueprint section 3: over 1 million shares a day
VOLUME_AVG_BARS = 50             # [Assumption] "a day" judged on the 50-day average
MAX_LEADERS = 50                 # [Sourced] blueprint section 3: about 30 to 50 names
# 1, 3 and 6-month strength, plus the weighted 12-month RS proxy. [Assumption] equal weight on each rank.
STRENGTH_BARS = {"1m": 21, "3m": 63, "6m": 126}


@dataclass(frozen=True)
class Leader:
    symbol: str
    score: float                 # 0-100, the average percentile rank across the strength measures
    returns: dict[str, float]


@dataclass
class LeaderScan:
    leaders: list[Leader]
    ranked: int                  # names with usable bars that passed price and volume
    skipped: dict[str, str] = field(default_factory=dict)   # symbol -> why (bad data, too cheap, thin)
    failed_template: int = 0


def _strength(close: pd.Series) -> dict[str, float]:
    out = {k: close.iloc[-1] / close.iloc[-1 - n] - 1 for k, n in STRENGTH_BARS.items()}
    out["rs12m"] = sum(w * (close.iloc[-1] / close.iloc[-1 - n] - 1)
                       for w, n in ((0.4, 63), (0.2, 126), (0.2, 189), (0.2, 252)))
    return out


def leader_scan(bars: Mapping[str, pd.DataFrame], spy_close: pd.Series,
                limit: int = MAX_LEADERS) -> LeaderScan:
    """Rank a universe's daily bars by strength and keep the top names that pass the Trend Template."""
    rows, skipped = {}, {}
    for sym, df in bars.items():
        try:
            if len(df) < 260:
                raise BarDataError("under a year of bars")
            if df["close"].iloc[-1] <= MIN_PRICE:
                skipped[sym] = f"price under ${MIN_PRICE:.0f}"
                continue
            if df["volume"].iloc[-VOLUME_AVG_BARS:].mean() < MIN_AVG_VOLUME:
                skipped[sym] = "under 1M shares a day"
                continue
            rows[sym] = _strength(df["close"])
        except (BarDataError, KeyError, IndexError) as e:
            skipped[sym] = f"bad data: {e}"
    if not rows:
        return LeaderScan([], 0, skipped)
    table = pd.DataFrame(rows).T
    score = (table.rank(pct=True).mean(axis=1) * 100).sort_values(ascending=False)
    leaders, failed = [], 0
    for sym in score.index:
        if len(leaders) >= limit:
            break
        try:
            gate = trend_template(daily_features(bars[sym]), spy_close)
        except BarDataError as e:
            skipped[sym] = f"bad data: {e}"
            continue
        if not gate.passed:
            failed += 1
            continue
        leaders.append(Leader(sym, round(float(score[sym]), 1), table.loc[sym].round(4).to_dict()))
    return LeaderScan(leaders, len(rows), skipped, failed)


def build_watchlist(leaders: Iterable[str], movers: Iterable[str] = (), added: Iterable[str] = (),
                    removed: Iterable[str] = ()) -> dict[str, list[str]]:
    """Symbol -> where it came from. Taz's removals win over every source except SPY, QQQ and IWM."""
    out: dict[str, list[str]] = {}
    gone = {s.upper() for s in removed}
    for source, names in (("always", ALWAYS), ("leader scan", leaders), ("mover", movers), ("Taz", added)):
        for s in names:
            s = s.upper().strip()
            if s and (source in ("always", "Taz") or s not in gone):
                out.setdefault(s, []).append(source)
    return out


def _num(row: Mapping, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _price(row: Mapping) -> float:
    return _num(row, "price") or _num(row, "close")


def _rankings(source, lists, skipped: dict[str, str]) -> list[dict]:
    rows = []
    for kind, arg in lists:
        try:
            rows += getattr(source, kind)(arg)
        except BarDataError as e:                    # one list failing doesn't stop the others; it's logged
            skipped[f"{kind}:{arg}"] = str(e)
    return rows


def universe(source, skipped: dict[str, str]) -> list[str]:
    """Names for the Friday leader scan: Webull's top lists, over $10, common-stock tickers only."""
    rows = _rankings(source, UNIVERSE_LISTS, skipped)
    return sorted({r["symbol"] for r in rows if _price(r) > MIN_PRICE and r["symbol"].isalpha()})


def movers(source, skipped: dict[str, str]) -> list[str]:
    """This morning's names over $10 up 10%+ (pre-market or today), or trading 2× their usual volume.

    The episodic pivot's candidates; the setup's own check decides. [Assumption] 2× relative volume.
    """
    out = set()
    for kind, arg in MOVER_LISTS:
        for r in _rankings(source, [(kind, arg)], skipped):
            big = (_num(r, "relative_volume_10d") >= 2.0 if arg == "RELATIVE_VOLUME_10D"
                   else _num(r, "change_ratio") >= MIN_GAP)
            if big and _price(r) > MIN_PRICE and r["symbol"].isalpha():
                out.add(r["symbol"])
    return sorted(out)
