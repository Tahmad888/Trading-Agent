"""Daily-bar checks for the 10 wave-1 setups: is the setup armed for the next session?

Blueprint v2.4 section 5, build step 4; serves the analyze and plan steps.
Each check reads the last daily bar of desk.indicators.daily_features and the
numbers on its card (desk.playbook.cards). A Signal says the daily chart shows
the setup, with the level the entry must break, the stop and what each check
saw. The intraday part (the first 15-minute or 1-hour high) is checked at
entry time, and the risk engine still sizes or rejects the trade.

Plain code only: no model decides whether a setup is there. Missing data raises
BarDataError (fail closed); a setup that isn't there returns None.

Plan B: if a check misses setups Taz sees on his screen, it logs the chart to
the journal as a miss and the rule is fixed only with his approval (rule 5).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from desk.bars import BarDataError
from desk.indicators import anchored_vwap, ema, rs_line
from desk.playbook.cards import CARDS, INDEX_ETFS, Card, Direction
from desk.playbook.filters import GateResult, MarketSize, stage4_puts_allowed

TICK = 0.05                       # [Assumption] "just under" a level: 5 cents


@dataclass(frozen=True)
class Context:
    symbol: str
    market: MarketSize
    template: GateResult | None = None      # None for names that skip it (index ETFs)
    spy_close: pd.Series | None = None
    today_open: float | None = None         # the episodic pivot's gap day
    early_volume: float | None = None       # first 30 minutes' volume on the gap day


@dataclass(frozen=True)
class Signal:
    setup_id: str
    symbol: str
    direction: Direction
    as_of: pd.Timestamp
    trigger: float                # the level the entry must break (or the entry level)
    stop: float
    target: float | None = None
    saw: dict[str, str] = field(default_factory=dict)   # what each check saw, in plain words

    def __post_init__(self):
        for name in ("trigger", "stop", "target"):
            v = getattr(self, name)
            if v is not None:
                object.__setattr__(self, name, round(float(v), 2))
        if (self.direction == "long") != (self.stop < self.trigger):
            raise ValueError(f"{self.setup_id} {self.symbol}: stop {self.stop} is on the wrong side of {self.trigger}")

    @property
    def risk_per_share(self) -> float:
        return abs(self.trigger - self.stop)


def _need(row: pd.Series, *cols: str) -> None:
    bad = [c for c in cols if pd.isna(row[c])]
    if bad:
        raise BarDataError(f"not warmed up: {bad}")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def swings(s: pd.Series, n: int, kind: str) -> list[int]:
    """Positions of swing highs (kind="high") or lows: the extreme of n bars either side."""
    v = s.to_numpy()
    out = []
    for i in range(n, len(v) - n):
        window = v[i - n:i + n + 1]
        if (kind == "high" and v[i] == window.max()) or (kind == "low" and v[i] == window.min()):
            if not out or i - out[-1] > n:
                out.append(i)
    return out


def _long_ok(card: Card, ctx: Context) -> str | None:
    """Why a long can't be taken, or None."""
    if ctx.market is MarketSize.NO_NEW_LONGS:
        return "market filter: no new longs"
    if card.trend_template and not (ctx.template and ctx.template.passed):
        return "fails the Trend Template"
    return None


# 1. Qullamaggie breakout
def qullamaggie_breakout(f: pd.DataFrame, card: Card, ctx: Context) -> Signal | None:
    last = f.iloc[-1]
    _need(last, "sma_20", "adr_pct_20")
    if _long_ok(card, ctx) or last["adr_pct_20"] < card.p("min_adr_pct"):
        return None
    pre = int(card.p("prior_move_bars"))
    for n in range(int(card.p("base_min_bars")), int(card.p("base_max_bars")) + 1):
        base, before = f.iloc[-n:], f.iloc[-n - pre:-n]
        if len(before) < pre:
            break
        top_at = before["high"].to_numpy().argmax()
        run = before["high"].iloc[top_at] / before["low"].iloc[:top_at + 1].min() - 1
        if run < card.p("prior_move"):
            continue
        first, last5 = base.iloc[:10], base.iloc[-5:]
        tight = (last5["high"].max() - last5["low"].min()) < card.p("tightening") * (first["high"].max() - first["low"].min())
        riding = (base["close"] > base["sma_20"]).mean() >= card.p("above_20d_share")
        higher_lows = base["low"].iloc[n // 2:].min() >= base["low"].iloc[:n // 2].min()
        base_high = base["high"].max()
        if tight and riding and higher_lows and last["close"] < base_high:
            stop = base_high * (1 - card.p("max_stop_adr") * last["adr_pct_20"] / 100)
            return Signal(card.id, ctx.symbol, "long", f.index[-1], base_high, stop, None, {
                "prior move": f"up {_pct(run)} before the base", "base": f"{n} days, tightening, above the 20-day",
                "ADR%": f"{last['adr_pct_20']:.1f}", "trigger": f"base high {base_high:.2f}"})
    return None


def _contractions(f: pd.DataFrame, start: int, n: int) -> list[tuple[float, float, float]]:
    """(high, low, depth) of each pullback from a swing high to the lowest low before the next one."""
    highs = [i for i in swings(f["high"], n, "high") if i >= start]
    if not highs or highs[0] != start:
        highs = [start] + highs
    out = []
    for a, b in zip(highs, highs[1:] + [len(f)]):
        low = f["low"].iloc[a:b].min()
        h = f["high"].iloc[a]
        out.append((h, low, 1 - low / h))
    return out


# 2. Minervini VCP
def minervini_vcp(f: pd.DataFrame, card: Card, ctx: Context) -> Signal | None:
    if _long_ok(card, ctx):
        return None
    look = int(card.p("base_lookback"))
    window = f.iloc[-look:]
    start = len(f) - look + int(window["high"].to_numpy().argmax())
    if len(f) - start < card.p("base_min_bars"):
        return None
    c = _contractions(f, start, int(card.p("pivot_bars")))
    # The last contraction may still be forming; it must have made its low already.
    if len(c) < card.p("min_contractions"):
        return None
    depths = [d for _, _, d in c]
    shrinking = all(b <= card.p("shrink") * a for a, b in zip(depths, depths[1:]))
    pivot, low, last_depth = c[-1]
    vol10 = f["volume"].iloc[-10:].mean()
    vol50 = f["volume"].iloc[-50:].mean()
    close = f["close"].iloc[-1]
    stop = low - TICK
    if not (shrinking and last_depth <= card.p("last_max_depth") and vol10 < card.p("dry_volume") * vol50
            and close < pivot and (pivot - stop) / pivot <= card.p("max_stop_pct")):
        return None
    return Signal(card.id, ctx.symbol, "long", f.index[-1], pivot, stop, None, {
        "contractions": " → ".join(_pct(d) for d in depths),
        "volume": f"10-day average is {vol10 / vol50:.0%} of the 50-day",
        "trigger": f"pivot {pivot:.2f}, no fill above {pivot * (1 + card.p('max_chase')):.2f}"})


# 3. O'Neil cup-with-handle
def oneil_cup_with_handle(f: pd.DataFrame, card: Card, ctx: Context) -> Signal | None:
    if _long_ok(card, ctx):
        return None
    hmax, hmin = int(card.p("handle_max_bars")), int(card.p("handle_min_bars"))
    recent = f["high"].iloc[-hmax - 1:]
    r = len(f) - len(recent) + int(recent.to_numpy().argmax())       # right-side high = handle start
    handle = f.iloc[r:]
    if not hmin <= len(handle) - 1 <= hmax:
        return None
    lo, hi = r - int(card.p("cup_max_bars")), r - int(card.p("cup_min_bars"))
    if lo < int(card.p("prior_advance_bars")):
        lo = int(card.p("prior_advance_bars"))
    if hi <= lo:
        return None
    left = lo + int(f["high"].iloc[lo:hi].to_numpy().argmax())
    L, R = f["high"].iloc[left], f["high"].iloc[r]
    cup = f.iloc[left:r + 1]
    bottom_at = int(cup["low"].to_numpy().argmin())
    bottom = cup["low"].iloc[bottom_at]
    depth = 1 - bottom / L
    advance = L / f["low"].iloc[left - int(card.p("prior_advance_bars")):left].min() - 1
    handle_low = handle["low"].iloc[1:].min()
    handle_depth = 1 - handle_low / R
    rounded = 0.2 <= bottom_at / (len(cup) - 1) <= 0.8
    vol50 = f["volume"].iloc[-50:].mean()
    ok = (advance >= card.p("prior_advance") and card.p("cup_min_depth") <= depth <= card.p("cup_max_depth")
          and rounded and R >= card.p("right_side") * L and R <= L * 1.05
          and handle_depth <= card.p("handle_max_depth") and handle_low > bottom + (L - bottom) / 2
          and handle["volume"].iloc[1:].mean() < vol50 and f["close"].iloc[-1] < R)
    if not ok:
        return None
    pivot = R + (0.10 if R < 100 else R * 0.001)
    stop = max(pivot * (1 - card.p("stop_pct")), handle_low - TICK)
    return Signal(card.id, ctx.symbol, "long", f.index[-1], pivot, stop, pivot * 1.20, {
        "prior advance": f"up {_pct(advance)}", "cup": f"{_pct(depth)} deep over {len(cup) // 5} weeks",
        "handle": f"{_pct(handle_depth)} deep over {len(handle) - 1} days on light volume",
        "trigger": f"handle high {pivot:.2f}; earnings gate checked separately"})


# 4. Darvas box
def darvas_box(f: pd.DataFrame, card: Card, ctx: Context) -> Signal | None:
    if _long_ok(card, ctx):
        return None
    last = f.iloc[-1]
    _need(last, "high_52w", "adr_pct_20")
    recent = f["high"].iloc[-int(card.p("new_high_bars")):]
    t = len(f) - len(recent) + int(recent.to_numpy().argmax())
    top = f["high"].iloc[t]
    after = f.iloc[t + 1:]
    if top < f["high_52w"].iloc[t] or len(after) < card.p("confirm_bars") or (after["high"] >= top).any():
        return None
    bottom = after["low"].min()
    height = (top - bottom) / top * 100
    if height > card.p("max_height_adr") * last["adr_pct_20"] or last["close"] < bottom:
        return None
    return Signal(card.id, ctx.symbol, "long", f.index[-1], top, bottom - TICK, None, {
        "new high": f"52-week high {top:.2f}, {len(after)} days ago",
        "box": f"{bottom:.2f} to {top:.2f} ({height:.1f}% tall)",
        "trigger": f"box top {top:.2f} on volume {card.p('breakout_volume')}× the 50-day"})


# 5. Qullamaggie episodic pivot (the gap day itself)
def episodic_pivot(f: pd.DataFrame, card: Card, ctx: Context) -> Signal | None:
    if ctx.today_open is None or ctx.market is MarketSize.NO_NEW_LONGS:
        return None
    last = f.iloc[-1]                                     # the day before the gap
    _need(last, "adr_pct_20")
    gap = ctx.today_open / last["close"] - 1
    base = f.iloc[-int(card.p("sideways_bars")):]
    hi, lo = base["high"].max(), base["low"].min()
    mid = (hi + lo) / 2
    sideways = (hi / lo - 1) <= card.p("sideways_range") and abs(last["close"] / mid - 1) <= card.p("near_middle")
    vol50 = f["volume"].iloc[-50:].mean()
    heavy = ctx.early_volume is not None and ctx.early_volume >= card.p("early_volume") * vol50
    if gap < card.p("min_gap") or not sideways or not heavy:
        return None
    stop = ctx.today_open * (1 - card.p("max_stop_adr") * last["adr_pct_20"] / 100)
    return Signal(card.id, ctx.symbol, "long", f.index[-1], ctx.today_open, stop, None, {
        "gap": f"up {_pct(gap)} at the open", "before": f"sideways {len(base)} days in a {_pct(hi / lo - 1)} range",
        "early volume": f"{ctx.early_volume / vol50:.0%} of a normal day",
        "trigger": "either completed 15-minute or 60-minute opening-range high on day one; earnings growth checked separately"})


def growth_group(card: Card, eps_growth: float | None, sales_growth: float | None) -> str | None:
    """The episodic pivot's earnings gate: None if it fails, else the group the journal compares.

    Year-on-year growth as fractions (0.30 = 30%). EPS or sales counts. Both
    groups trade the same size with the same rules; only the tag differs.
    Missing numbers fail the gate (fail closed).
    """
    best = max((g for g in (eps_growth, sales_growth) if g is not None), default=None)
    if best is None or best < card.p("min_growth"):
        return None
    return "50%+" if best >= card.p("strong_growth") else "25-50%"


# 6. Kell EMA crossback
def kell_crossback(f: pd.DataFrame, card: Card, ctx: Context) -> Signal | None:
    if _long_ok(card, ctx):
        return None
    last = f.iloc[-1]
    _need(last, "ema_10", "ema_20", "atr_14")
    k = int(card.p("rising_bars"))
    rising = last["ema_10"] > f["ema_10"].iloc[-1 - k] and last["ema_20"] > f["ema_20"].iloc[-1 - k]
    above = (f["ema_10"] > f["ema_20"]).to_numpy()
    fresh = int(card.p("fresh_bars"))
    if not (above[-1] and rising) or above[-fresh:].all() or not above[-fresh:].any():
        return None
    cross = len(f) - fresh + int(np.flatnonzero(~above[-fresh:])[-1]) + 1
    band = card.p("touch_atr") * f["atr_14"]
    touch = f["low"] <= np.maximum(f["ema_10"], f["ema_20"]) + band
    lifted = f["low"] > f["ema_10"] + band
    since = slice(cross, len(f) - 1)
    first_lift = np.flatnonzero(lifted.iloc[since].to_numpy())
    if not touch.iloc[-1] or last["close"] < last["ema_20"] or not len(first_lift):
        return None
    run_start = len(f) - 1                               # today's pullback may have started days ago
    while run_start > cross and touch.iloc[run_start - 1]:
        run_start -= 1
    if touch.iloc[cross + first_lift[0]:run_start].any():
        return None                                      # not the first pullback
    return Signal(card.id, ctx.symbol, "long", f.index[-1], last["high"], last["low"] - TICK, None, {
        "trend": f"10 EMA crossed over the 20 EMA {len(f) - cross} days ago, both rising",
        "pullback": "first dip to the 10/20 EMA since", "trigger": f"today's high {last['high']:.2f} on 1-hour bars"})


# 7. Luk pullback and reclaim
def luk_reclaim(f: pd.DataFrame, card: Card, ctx: Context) -> Signal | None:
    if _long_ok(card, ctx):
        return None
    last = f.iloc[-1]
    _need(last, "ema_9", "ema_21", "atr_14", "adr_pct_20")
    runs = {k: f["close"].iloc[-1] / f["close"].iloc[-1 - n] - 1 for k, n in (("1m", 21), ("3m", 63), ("6m", 126))}
    ema50 = ema(f["close"], 50).iloc[-1]
    if max(runs.values()) < card.p("min_run") or not last["ema_9"] > last["ema_21"] > ema50:
        return None
    anchor_bars = f.iloc[-int(card.p("anchor_bars")):]
    avwap = anchored_vwap(f, anchor_bars["low"].idxmin()).iloc[-1]
    near = card.p("near_atr") * last["atr_14"]
    levels = {"21 EMA": last["ema_21"], "anchored VWAP": avwap}
    hit = {k: v for k, v in levels.items() if not pd.isna(v) and last["low"] <= v + near and last["close"] >= v - near}
    if not hit:
        return None
    name, level = max(hit.items(), key=lambda kv: kv[1])
    stop = max(last["low"] - TICK, level * (1 - card.p("max_stop_adr") * last["adr_pct_20"] / 100))
    best = max(runs, key=runs.get)
    return Signal(card.id, ctx.symbol, "long", f.index[-1], level, stop, None, {
        "run": f"up {_pct(runs[best])} over {best}", "pullback": f"to the {name} at {level:.2f}",
        "trigger": "a 15-minute close back above it"})


# 8. Raschke Holy Grail, both ways
def holy_grail(f: pd.DataFrame, card: Card, ctx: Context) -> Signal | None:
    last = f.iloc[-1]
    _need(last, "adx", "plus_di", "minus_di", "ema_20", "sma_50", "atr_14")
    n, k = int(card.p("swing_bars")), int(card.p("adx_rising_bars"))
    band = card.p("touch_atr") * last["atr_14"]
    recent = f.iloc[-n:]
    up = last["plus_di"] > last["minus_di"]
    s = len(f) - n + int((recent["high"] if up else -recent["low"]).to_numpy().argmax())
    adx_ok = f["adx"].iloc[s] > card.p("min_adx") and f["adx"].iloc[s] >= f["adx"].iloc[s - k]
    if not adx_ok or s == len(f) - 1:
        return None
    if up:
        if _long_ok(card, ctx) or not (last["low"] <= last["ema_20"] + band and last["close"] > last["sma_50"]):
            return None
        stop = f["low"].iloc[s:].min() - TICK
        return Signal(card.id, ctx.symbol, "long", f.index[-1], last["high"], stop, f["high"].iloc[s], {
            "trend": f"ADX {f['adx'].iloc[s]:.0f} and rising at the swing high, +DI over −DI",
            "pullback": "touched the 20 EMA", "trigger": f"today's high {last['high']:.2f}"})
    if not (last["high"] >= last["ema_20"] - band and last["close"] < last["sma_50"]):
        return None
    stop = f["high"].iloc[s:].max() + TICK
    return Signal(card.id, ctx.symbol, "short", f.index[-1], last["low"], stop, f["low"].iloc[s], {
        "trend": f"ADX {f['adx'].iloc[s]:.0f} and rising at the swing low, −DI over +DI",
        "rally": "touched the 20 EMA", "trigger": f"today's low {last['low']:.2f}"})


# 9. Weinstein stage 4 breakdown (puts)
def weinstein_stage4(f: pd.DataFrame, card: Card, ctx: Context) -> Signal | None:
    if not stage4_puts_allowed(ctx.market) or ctx.spy_close is None:
        return None
    last = f.iloc[-1]
    _need(last, "sma_150")
    top = int(card.p("top_bars"))
    support = f["low"].iloc[-top - 1:-1].min()
    turning = last["sma_150"] < f["sma_150"].iloc[-1 - int(card.p("turn_bars"))]
    rs = rs_line(f["close"], ctx.spy_close)
    rs_falling = rs.iloc[-1] < rs.iloc[-int(card.p("rs_avg_bars")):].mean()
    # A top, not a long decline: the 150-day was rising within the top's window.
    topped = f["sma_150"].iloc[-top - 1:].max() > f["sma_150"].iloc[-top - 1]
    if not (last["close"] < support and turning and rs_falling and topped):
        return None
    stop = f["high"].iloc[-int(card.p("rally_bars")):].max() + TICK
    return Signal(card.id, ctx.symbol, "short", f.index[-1], last["close"], stop, None, {
        "top": f"150-day flattened and turned down; support {support:.2f}",
        "breakdown": f"closed at {last['close']:.2f}, under support", "RS": "falling against SPY",
        "trigger": "buy the put next session while price stays under support"})


# 10. Connors RSI(2), index ETFs, both ways
def connors_rsi2(f: pd.DataFrame, card: Card, ctx: Context) -> Signal | None:
    if ctx.symbol not in INDEX_ETFS:
        return None
    last = f.iloc[-1]
    _need(last, "sma_200", "rsi_2", "atr_10")
    stop_dist = card.p("stop_atr") * last["atr_10"]
    if last["close"] > last["sma_200"] and last["rsi_2"] < card.p("rsi_low"):
        if ctx.market is MarketSize.NO_NEW_LONGS:
            return None
        d, stop = "long", last["close"] - stop_dist
    elif last["close"] < last["sma_200"] and last["rsi_2"] > card.p("rsi_high"):
        d, stop = "short", last["close"] + stop_dist
    else:
        return None
    return Signal(card.id, ctx.symbol, d, f.index[-1], last["close"], stop, None, {
        "trend": f"{'above' if d == 'long' else 'below'} the 200-day", "RSI(2)": f"{last['rsi_2']:.1f}",
        "exit": f"a close {'above' if d == 'long' else 'below'} the 5-day average, or 10 days"})


CHECKS: dict[str, Callable[[pd.DataFrame, Card, Context], Signal | None]] = {
    "1_qullamaggie_breakout": qullamaggie_breakout,
    "2_minervini_vcp": minervini_vcp,
    "3_oneil_cup_with_handle": oneil_cup_with_handle,
    "4_darvas_box": darvas_box,
    "5_qullamaggie_episodic_pivot": episodic_pivot,
    "6_kell_ema_crossback": kell_crossback,
    "7_luk_pullback_reclaim": luk_reclaim,
    "8_raschke_holy_grail": holy_grail,
    "9_weinstein_stage4_breakdown": weinstein_stage4,
    "10_connors_rsi2": connors_rsi2,
}


def scan(features: pd.DataFrame, ctx: Context) -> list[Signal]:
    """Every setup armed on this ticker's last daily bar."""
    out = []
    for setup_id, check in CHECKS.items():
        sig = check(features, CARDS[setup_id], ctx)
        if sig is not None:
            out.append(sig)
    return out
