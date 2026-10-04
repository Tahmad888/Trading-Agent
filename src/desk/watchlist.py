"""The desk-built watchlist: the Friday leader scan plus the day's movers.

Blueprint v2.4 section 3, step 4; serves the watch step. No fixed favourites
list (Taz, 29 Sep 2026): the list is the weekly leaders that pass the Trend
Template, the day's news and earnings movers, SPY, QQQ and IWM always, and any
ticker Taz adds. User changes apply on the next scheduled scan; core ETFs remain.

Plan B: a partial or failed weekly build preserves the previous list with an
explicit status and age. Core and user names still require independently valid
metadata and bars. A constituent-universe fallback is not implemented.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
import math

import pandas as pd

from desk.bars import BarDataError
from desk.security import securities
from desk.symbols import canonical_symbol
from desk.data_basis import VolumeUnavailable, decision_window, volume_evidence
from desk.indicators import daily_features
from desk.playbook.filters import trend_template

ALWAYS = ("SPY", "QQQ", "IWM")
# The leader scan's universe: Webull's top-200 lists, merged. [Assumption] Together they cover the
# liquid names that moved most over 1 week to 1 year; price and volume are checked on our own bars.
UNIVERSE_LISTS = (("gainers", "DAY_5"), ("gainers", "MONTH_1"), ("gainers", "MONTH_3"),
                  ("gainers", "WEEK_52"), ("most_active", "TURNOVER"), ("most_active", "VOLUME"))
MOVER_LISTS = (("gainers", "PRE_MARKET"), ("gainers", "DAY_1"), ("most_active", "RELATIVE_VOLUME_10D"))
BEARISH_LISTS = (("losers", "DAY_1"), ("losers", "DAY_5"), ("losers", "MONTH_1"))
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


def _strength(close: pd.Series) -> dict[str, float]:
    out = {k: close.iloc[-1] / close.iloc[-1 - n] - 1 for k, n in STRENGTH_BARS.items()}
    out["rs12m"] = sum(w * (close.iloc[-1] / close.iloc[-1 - n] - 1)
                       for w, n in ((0.4, 63), (0.2, 126), (0.2, 189), (0.2, 252)))
    return out


LIQUIDITY_RULE = f"discovery-liquidity:mean{VOLUME_AVG_BARS}>={MIN_AVG_VOLUME}:v1"
# Terminal outcome of every universe candidate (G5a checkpoint 2). "criterion": a written
# discovery rule excluded it; "source_failure": its own data could not be evaluated;
# "limit": ranked but not reached before the list was full; "selected": a leader.
OUTCOMES = ("selected", "criterion", "source_failure", "limit")


@dataclass
class LeaderScan:
    leaders: list[Leader]
    ranked: int                  # names with usable bars that passed price and volume
    skipped: dict[str, str] = field(default_factory=dict)   # symbol -> why (bad data, too cheap, thin)
    failed_template: int = 0
    outcomes: dict[str, dict] = field(default_factory=dict)  # symbol -> {"outcome", "stage", "reason"}
    population: list[str] = field(default_factory=list)     # the ranked population, best first
    evidence: dict[str, dict] = field(default_factory=dict)  # symbol -> Alpaca liquidity evidence


def _liquid(df: pd.DataFrame) -> tuple[bool, dict | None]:
    """Mean daily volume over the window at least ``MIN_AVG_VOLUME``.

    Alpaca SIP attached: exact Decimal over the latest final native-daily sessions
    (``total >= MIN_AVG_VOLUME * VOLUME_AVG_BARS``), with persisted evidence. Otherwise
    the unchanged Webull path.
    """
    window = decision_window(df, VOLUME_AVG_BARS, final_only=True)
    if window.source == "webull":
        return not bool(df["volume"].iloc[-VOLUME_AVG_BARS:].mean() < MIN_AVG_VOLUME), None
    total = sum(window.values, Decimal(0))
    met = total >= Decimal(MIN_AVG_VOLUME) * VOLUME_AVG_BARS
    return met, volume_evidence("discovery:liquidity", LIQUIDITY_RULE,
                                {"total": str(total), "sessions": VOLUME_AVG_BARS, "met": met},
                                [(window.decision, window.used())])


def leader_scan(bars: Mapping[str, pd.DataFrame], spy_close: pd.Series,
                limit: int = MAX_LEADERS) -> LeaderScan:
    """Rank a universe's daily bars by strength and keep the top names that pass the Trend Template.

    Every candidate ends with one recorded outcome. A name whose own data fails is
    excluded from the disclosed ranking population; the rank math is unchanged.
    """
    rows, skipped, outcomes, evidence = {}, {}, {}, {}

    def out(sym, outcome, stage, reason):
        outcomes[sym] = {"outcome": outcome, "stage": stage, "reason": reason}

    for sym, df in bars.items():
        try:
            if len(df) < 260:
                skipped[sym] = "bad data: under a year of bars"
                out(sym, "criterion", "history", "under a year of bars")
                continue
            if df["close"].iloc[-1] <= MIN_PRICE:
                skipped[sym] = f"price under ${MIN_PRICE:.0f}"
                out(sym, "criterion", "price", skipped[sym])
                continue
            liquid, used = _liquid(df)
            if used is not None:
                evidence[sym] = used
            if not liquid:
                skipped[sym] = "under 1M shares a day"
                out(sym, "criterion", "liquidity", skipped[sym])
                continue
            rows[sym] = _strength(df["close"])
        except VolumeUnavailable as e:
            skipped[sym] = str(e)
            out(sym, "source_failure", "liquidity", e.code)
        except (BarDataError, KeyError, IndexError) as e:
            skipped[sym] = f"bad data: {e}"
            out(sym, "source_failure", "ranking_input", str(e))
    if not rows:
        return LeaderScan([], 0, skipped, outcomes=outcomes, evidence=evidence)
    table = pd.DataFrame(rows).T
    score = (table.rank(pct=True).mean(axis=1) * 100).sort_values(ascending=False)
    leaders, failed = [], 0
    for sym in score.index:
        if len(leaders) >= limit:
            out(sym, "limit", "max_leaders", f"ranked after the {limit}-name list was full")
            continue
        try:
            gate = trend_template(daily_features(bars[sym]), spy_close)
        except BarDataError as e:
            skipped[sym] = f"bad data: {e}"
            out(sym, "source_failure", "trend_template", str(e))
            continue
        if not gate.passed:
            failed += 1
            out(sym, "criterion", "trend_template", "failed the Trend Template")
            continue
        leaders.append(Leader(sym, round(float(score[sym]), 1), table.loc[sym].round(4).to_dict()))
        out(sym, "selected", "leader", "selected")
    return LeaderScan(leaders, len(rows), skipped, failed, outcomes, list(score.index), evidence)


def build_watchlist(leaders: Iterable[str], movers: Iterable[str] = (), added: Iterable[str] = (),
                    removed: Iterable[str] = (), *, bearish: Iterable[str] = ()) -> dict[str, list[str]]:
    """Symbol -> where it came from. Taz's removals win over every source except SPY, QQQ and IWM."""
    out: dict[str, list[str]] = {}
    gone = {canonical_symbol(s) for s in removed}
    for source, names in (("always", ALWAYS), ("leader scan", leaders), ("mover", movers), ("bearish", bearish), ("Taz", added)):
        for s in names:
            s = canonical_symbol(s)
            if s and (source == "always" or s not in gone):
                if source not in out.setdefault(s, []):
                    out[s].append(source)
    return out


def _num(row: Mapping, key: str) -> float:
    try:
        value = float(row.get(key) or 0)
        return value if math.isfinite(value) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _price(row: Mapping) -> float:
    return _num(row, "price") or _num(row, "close")


def _rankings(source, lists, skipped: dict[str, str]) -> list[dict]:
    rows = []
    for kind, arg in lists:
        try:
            result = source.gainers(arg, losers=True) if kind == "losers" else getattr(source, kind)(arg)
            if not isinstance(result, list) or any(not isinstance(r, Mapping) or not isinstance(r.get("symbol"), str) or not r["symbol"].strip() for r in result):
                raise BarDataError("malformed ranking rows")
            for row in result:
                try:
                    if _price(row) <= 0:
                        raise ValueError("missing positive price")
                    for key in ("price", "close", "change_ratio", "relative_volume_10d"):
                        if row.get(key) is not None and not math.isfinite(float(row[key])):
                            raise ValueError("nonfinite ranking field")
                    rows.append(row)
                except (TypeError, ValueError):
                    skipped[f"{kind}:{arg}:{row['symbol']}"] = "malformed numeric ranking data"
        except (BarDataError, AttributeError) as e:                    # one list failing doesn't stop the others; it's logged
            skipped[f"{kind}:{arg}"] = str(e)
    return rows


def universe(source, skipped: dict[str, str], *, considered: list[str] | None = None) -> list[str]:
    """Friday candidate universe: top lists over $10 with supported security metadata.

    ``considered`` receives every listed name over $10 before the metadata check, so a
    build can give each one a recorded outcome.
    """
    rows = _rankings(source, UNIVERSE_LISTS, skipped)
    names = sorted({canonical_symbol(r["symbol"]) for r in rows if _price(r) > MIN_PRICE})
    if considered is not None:
        considered.extend(names)
    return sorted(securities(source, names, skipped))


def movers(source, skipped: dict[str, str]) -> list[str]:
    """This morning's names over $10 up 10%+ (pre-market or today), or trading 2× their usual volume.

    The episodic pivot's candidates; the setup's own check decides. [Assumption] 2× relative volume.
    """
    out = set()
    for kind, arg in MOVER_LISTS:
        for r in _rankings(source, [(kind, arg)], skipped):
            big = (_num(r, "relative_volume_10d") >= 2.0 if arg == "RELATIVE_VOLUME_10D"
                   else _num(r, "change_ratio") >= MIN_GAP)
            if big and _price(r) > MIN_PRICE:
                out.add(canonical_symbol(r["symbol"]))
    return sorted(securities(source, sorted(out), skipped))


def bearish_candidates(source, skipped: dict[str, str]) -> list[str]:
    """Declining liquid-universe candidates; setup rules determine bearish eligibility.

    Source selection is a coverage assumption, not a new short-entry threshold.
    Optionability and borrow availability are deliberately not discovery filters.
    """
    rows = _rankings(source, BEARISH_LISTS, skipped)
    names = sorted({canonical_symbol(r["symbol"]) for r in rows
                    if _price(r) > MIN_PRICE and _num(r, "change_ratio") < 0})
    return sorted(securities(source, names, skipped))


def overlay_picks(watchlist, picks):
    """Apply current operator changes every run, preserving independent source tags."""
    if not isinstance(picks, Mapping) or any(
        not isinstance(picks.get(k, []), list) or any(not isinstance(s, str) or not s.strip() for s in picks.get(k, []))
        for k in ("add", "remove")
    ):
        raise BarDataError("user picks must contain add/remove lists of ticker strings")
    if isinstance(watchlist, str):
        raise BarDataError("watchlist must be a symbol list or a source mapping")
    removed = {canonical_symbol(s) for s in picks.get("remove", [])}
    out = {}
    entries = watchlist.items() if isinstance(watchlist, Mapping) else ((s, ["watchlist"]) for s in watchlist)
    for symbol, origins in entries:
        if not isinstance(symbol, str) or not isinstance(origins, (list, tuple)) or any(not isinstance(tag, str) for tag in origins):
            raise BarDataError("malformed watchlist symbol/source mapping")
        symbol = canonical_symbol(symbol)
        if symbol and (symbol not in removed or symbol in ALWAYS):
            out[symbol] = list(dict.fromkeys(out.get(symbol, []) + list(origins)))
    for symbol, origins in build_watchlist([], added=picks.get("add", []), removed=removed).items():
        out[symbol] = list(dict.fromkeys(out.get(symbol, []) + origins))
    return out
