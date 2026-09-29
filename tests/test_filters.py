import numpy as np
import pandas as pd
import pytest

from desk.bars import BarDataError
from desk.indicators import daily_features
from desk.playbook.filters import MarketSize, market_filter, stage4_puts_allowed, trend_template


def bars_from_path(close: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range("2023-01-02", periods=len(close), freq="B", tz="UTC")
    return pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995,
                         "close": close, "volume": np.full(len(close), 2e6)}, index=idx)


def path(*legs: tuple[int, float], start=100.0) -> np.ndarray:
    """A made-up chart: each leg is (bars, total % move), joined smoothly."""
    out, price = [], start
    for n, pct in legs:
        step = (1 + pct / 100) ** (1 / n)
        for _ in range(n):
            price *= step
            out.append(price)
    return np.array(out)


N = 600
SPY = bars_from_path(path((N, 20)))          # a slow, steady market


def feats(close):
    return daily_features(bars_from_path(close))


def test_a_leader_passes_every_check():
    r = trend_template(feats(path((N, 150))), SPY["close"])
    assert r.passed, r.failed


def test_a_falling_stock_fails_with_named_checks():
    r = trend_template(feats(path((N - 100, 60), (100, -40))), SPY["close"])
    assert not r.passed
    assert "price above the 50-day" in r.failed and "within 25% of the 52-week high" in r.failed


def test_a_stock_lagging_spy_fails_only_on_relative_strength():
    # Up 10% while SPY is up 20%: its trend looks fine, but its RS line is falling.
    r = trend_template(feats(path((N, 10))), SPY["close"])
    assert set(r.failed) <= {"RS line above its 50-day", "RS line near its 52-week high",
                             "at least 25% above the 52-week low"}
    assert "RS line above its 50-day" in r.failed


def test_trend_template_fails_closed_on_short_or_unaligned_bars():
    with pytest.raises(BarDataError):
        trend_template(feats(path((200, 30))), SPY["close"])
    with pytest.raises(BarDataError, match="different days"):
        trend_template(feats(path((N, 150))), SPY["close"].iloc[:-1])


def index(close):
    return daily_features(bars_from_path(close))


UP = path((N, 30))
DOWN = path((N - 150, 20), (150, -25))       # below a falling 50-day, which is under the 200-day
DIP = path((N - 10, 40), (10, -6))           # a short dip below a 50-day that is still above the 200-day


def test_market_filter_sizes():
    assert market_filter(index(UP), index(UP))[0] is MarketSize.FULL
    size, why = market_filter(index(UP), index(DOWN))
    assert size is MarketSize.HALF and "SPY" in why
    assert market_filter(index(DOWN), index(DOWN))[0] is MarketSize.NO_NEW_LONGS
    assert market_filter(index(DIP), index(DIP))[0] is MarketSize.HALF


def test_stage4_puts_only_below_full_size():
    assert not stage4_puts_allowed(MarketSize.FULL)
    assert stage4_puts_allowed(MarketSize.HALF) and stage4_puts_allowed(MarketSize.NO_NEW_LONGS)


def test_market_filter_fails_closed():
    with pytest.raises(BarDataError):
        market_filter(index(UP), index(UP[:-1]))
    with pytest.raises(BarDataError):
        market_filter(index(UP[:100]), index(UP[:100]))
