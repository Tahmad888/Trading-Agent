"""The two gates every long passes: Minervini's Trend Template and the market filter.

Blueprint v2.4 section 5, step 4; serves the watch and analyze steps. Both are
plain code on daily bars from desk.indicators. Missing or unaligned data raises
BarDataError, so no plan is made (fail closed). The index ETFs in the RSI(2)
setup skip the Trend Template; the scanner decides who is checked.

Plan B: if a gate misreads a chart Taz can see, the ticket shows each check's
pass or fail, so he can overrule it by hand, and the rule is fixed only with his
approval (rule 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from desk.bars import BarDataError
from desk.indicators import rs_line

# Trend Template numbers. [Sourced] Minervini, Trade Like a Stock Market Wizard (2013),
# as written in blueprint section 5, unless marked otherwise.
ABOVE_52W_LOW = 1.25        # at least 25% above the 52-week low
FROM_52W_HIGH = 0.75        # within 25% of the 52-week high
RISING_200_BARS = 21        # 200-day rising for at least a month
RS_AVG_BARS = 50            # [Assumption] RS line above its 50-day average
RS_NEAR_HIGH = 0.90         # [Assumption] "near its 52-week high" means within 10%
YEAR_BARS = 252

# Market filter. [Assumption] a desk default drawn from O'Neil and Kullamägi.
RISING_50_BARS = 5          # [Assumption] "rising" 50-day: higher than a week ago


def _need(values: dict[str, float]) -> None:
    missing = [k for k, v in values.items() if pd.isna(v)]
    if missing:
        raise BarDataError(f"not enough bars for {missing}")


@dataclass(frozen=True)
class GateResult:
    """Each check in plain words, so the ticket can show which one failed."""
    checks: dict[str, bool]

    def __post_init__(self):
        object.__setattr__(self, "checks", {k: bool(v) for k, v in self.checks.items()})

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    @property
    def failed(self) -> list[str]:
        return [k for k, ok in self.checks.items() if not ok]


def trend_template(features: pd.DataFrame, benchmark_close: pd.Series) -> GateResult:
    """Minervini's Trend Template on the last daily bar.

    features comes from desk.indicators.daily_features; benchmark_close is SPY's
    daily close. Both must end on the same day, or the data is treated as stale.
    """
    if len(features) < YEAR_BARS or len(benchmark_close) == 0:
        raise BarDataError("Trend Template needs a year of daily bars")
    if features.index[-1] != benchmark_close.index[-1]:
        raise BarDataError("stock and SPY bars end on different days")
    last = features.iloc[-1]
    sma200_month_ago = features["sma_200"].iloc[-1 - RISING_200_BARS]
    rs = rs_line(features["close"], benchmark_close)
    if len(rs) < YEAR_BARS or rs.index[-1] != features.index[-1]:
        raise BarDataError("RS line needs a year of dates shared with SPY")
    rs_now, rs_avg, rs_high = rs.iloc[-1], rs.iloc[-RS_AVG_BARS:].mean(), rs.iloc[-YEAR_BARS:].max()
    close, s50, s150, s200 = last["close"], last["sma_50"], last["sma_150"], last["sma_200"]
    _need({"close": close, "sma_50": s50, "sma_150": s150, "sma_200": s200,
           "sma_200 a month ago": sma200_month_ago, "high_52w": last["high_52w"],
           "low_52w": last["low_52w"], "rs_line": rs_now})
    return GateResult({
        "price above the 150 and 200-day": close > s150 and close > s200,
        "150-day above the 200-day": s150 > s200,
        "200-day rising for a month": s200 > sma200_month_ago,
        "50-day above the 150 and 200-day": s50 > s150 and s50 > s200,
        "price above the 50-day": close > s50,
        "at least 25% above the 52-week low": close >= ABOVE_52W_LOW * last["low_52w"],
        "within 25% of the 52-week high": close >= FROM_52W_HIGH * last["high_52w"],
        "RS line above its 50-day": rs_now > rs_avg,
        "RS line near its 52-week high": rs_now >= RS_NEAR_HIGH * rs_high,
    })


class MarketSize(str, Enum):
    FULL = "full"
    HALF = "half"
    NO_NEW_LONGS = "no new longs"


@dataclass(frozen=True)
class IndexTrend:
    symbol: str
    above_rising_50: bool
    below_50_and_50_below_200: bool


def index_trend(symbol: str, features: pd.DataFrame) -> IndexTrend:
    last = features.iloc[-1]
    s50_before = features["sma_50"].iloc[-1 - RISING_50_BARS] if len(features) > RISING_50_BARS else float("nan")
    _need({f"{symbol} close": last["close"], f"{symbol} sma_50": last["sma_50"],
           f"{symbol} sma_200": last["sma_200"], f"{symbol} sma_50 a week ago": s50_before})
    return IndexTrend(
        symbol,
        above_rising_50=bool(last["close"] > last["sma_50"] and last["sma_50"] > s50_before),
        below_50_and_50_below_200=bool(last["close"] < last["sma_50"] and last["sma_50"] < last["sma_200"]),
    )


def market_filter(spy: pd.DataFrame, qqq: pd.DataFrame) -> tuple[MarketSize, str]:
    """Full, half or no new longs, from SPY's and QQQ's daily features, with the reason.

    Blueprint: full when both are above a rising 50-day, half when only one is,
    no new longs when both are below their 50-day and the 50-day is below the
    200-day. Any other mix (both below a 50-day that is still above the 200-day,
    say) is half size [Assumption].
    """
    if spy.index[-1] != qqq.index[-1]:
        raise BarDataError("SPY and QQQ bars end on different days")
    a, b = index_trend("SPY", spy), index_trend("QQQ", qqq)
    up = [t.symbol for t in (a, b) if t.above_rising_50]
    if len(up) == 2:
        return MarketSize.FULL, "SPY and QQQ are both above a rising 50-day"
    if a.below_50_and_50_below_200 and b.below_50_and_50_below_200:
        return MarketSize.NO_NEW_LONGS, "SPY and QQQ are both below their 50-day, and the 50-day is below the 200-day"
    if len(up) == 1:
        return MarketSize.HALF, f"only {up[0]} is above a rising 50-day"
    return MarketSize.HALF, "neither is above a rising 50-day, but they are not both in a downtrend"


def stage4_puts_allowed(size: MarketSize) -> bool:
    """Weinstein stage 4 breakdown puts run only when the market filter is below full size."""
    return size is not MarketSize.FULL
