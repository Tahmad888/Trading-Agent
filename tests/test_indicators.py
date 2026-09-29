from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from desk import indicators as ind
from desk.bars import BarDataError, bars_from_webull, require, validate
from tests import tv_reference as tv

# Compare the last 300 of 1,000 bars: by then every seed difference between
# TA-Lib and TradingView has died out, which is why the desk loads ~1,000 bars.
TAIL = 300


def random_bars(n=1_000, seed=7, freq="B") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, n)))
    open_ = close * np.exp(rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n))
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.date_range("2022-10-03", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


@pytest.fixture(scope="module")
def bars():
    return random_bars()


def same(a: pd.Series, b: pd.Series, tol=1e-6):
    a, b = a.iloc[-TAIL:], b.iloc[-TAIL:]
    assert not a.isna().any() and not b.isna().any()
    np.testing.assert_allclose(a.to_numpy(), b.to_numpy(), rtol=tol, atol=tol)


def test_sma_and_ema_match_tradingview(bars):
    c = bars["close"]
    for n in (5, 20, 50, 200):
        same(ind.sma(c, n), c.rolling(n).mean())
    for n in (9, 10, 21):
        same(ind.ema(c, n), tv.pine_ema(c, n))


def test_rsi_matches_tradingview(bars):
    for n in (14, 2):
        same(ind.rsi(bars["close"], n), tv.pine_rsi(bars["close"], n))


def test_macd_matches_tradingview(bars):
    got = ind.macd(bars["close"], 12, 26, 9)
    line, sig, hist = tv.pine_macd(bars["close"], 12, 26, 9)
    same(got["macd"], line)
    same(got["macd_signal"], sig)
    same(got["macd_hist"], hist)


def test_stochastic_matches_tradingview(bars):
    got = ind.stochastic(bars, 14, 3, 3)
    k, d = tv.pine_stoch(bars, 14, 3, 3)
    same(got["stoch_k"], k)
    same(got["stoch_d"], d)


def test_atr_adx_match_tradingview(bars):
    same(ind.atr(bars, 14), tv.pine_atr(bars, 14))
    got = ind.adx(bars, 14)
    plus, minus, adx = tv.pine_dmi(bars, 14, 14)
    same(got["plus_di"], plus)
    same(got["minus_di"], minus)
    same(got["adx"], adx)


def test_bollinger_and_keltner_match_tradingview(bars):
    bb = ind.bollinger(bars["close"], 20, 2.0)
    for col, ref in zip(["bb_upper", "bb_mid", "bb_lower"], tv.pine_bb(bars["close"], 20, 2.0)):
        same(bb[col], ref)
    kc = ind.keltner(bars, 20, 2.0, 10)
    for col, ref in zip(["kc_upper", "kc_mid", "kc_lower"], tv.pine_kc(bars, 20, 2.0, 10)):
        same(kc[col], ref)


def test_keltner_centres_on_close_not_typical_price(bars):
    kc = ind.keltner(bars, 20, 2.0, 10)
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    assert not np.allclose(kc["kc_mid"].iloc[-TAIL:], ind.ema(typical, 20).iloc[-TAIL:])


def test_session_vwap_restarts_each_eastern_day():
    # Two sessions of 15-minute bars, 9:30-16:00 ET (13:30-20:00 UTC in summer).
    idx = (list(pd.date_range("2026-09-24 13:30", periods=26, freq="15min", tz="UTC"))
           + list(pd.date_range("2026-09-25 13:30", periods=26, freq="15min", tz="UTC")))
    df = random_bars(n=52).set_axis(pd.DatetimeIndex(idx))
    vwap = ind.session_vwap(df)
    typical = (df["high"] + df["low"] + df["close"]) / 3
    day2 = df.index >= pd.Timestamp("2026-09-25", tz="UTC")
    assert vwap[day2].iloc[0] == pytest.approx(typical[day2].iloc[0])
    expect = (typical[day2] * df["volume"][day2]).sum() / df["volume"][day2].sum()
    assert vwap.iloc[-1] == pytest.approx(expect)


def test_anchored_vwap_starts_at_anchor(bars):
    anchor = bars.index[900]
    av = ind.anchored_vwap(bars, anchor)
    assert av.iloc[:900].isna().all()
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    assert av.iloc[900] == pytest.approx(typical.iloc[900])


def test_adr_rs_and_weighted_return(bars):
    adr = ind.adr_pct(bars, 20)
    assert adr.iloc[-1] == pytest.approx(((bars["high"] / bars["low"] - 1) * 100).iloc[-20:].mean())
    spy = bars["close"] * 2
    assert ind.rs_line(bars["close"], spy).iloc[-1] == pytest.approx(0.5)
    c = bars["close"]
    w = ind.weighted_12m_return(c).iloc[-1]
    r = lambda n: c.iloc[-1] / c.iloc[-1 - n] - 1  # noqa: E731
    assert w == pytest.approx(0.4 * r(63) + 0.2 * (r(126) + r(189) + r(252)))


def test_daily_features_warm_up_and_fail_closed(bars):
    feats = ind.daily_features(bars)
    row = ind.latest(feats)
    assert row["sma_200"] > 0 and 0 <= row["rsi_14"] <= 100
    with pytest.raises(BarDataError, match="not warmed up"):
        ind.latest(ind.daily_features(bars.iloc[:150]))


def webull_row(t, o, h, lo, c, v):
    return {"symbol": "SPY", "time": t, "open": str(o), "high": str(h), "low": str(lo),
            "close": str(c), "volume": str(v), "trading_session": ""}


def test_bars_from_webull_sorts_oldest_first():
    df = bars_from_webull([webull_row("2026-09-28T04:00:00.000+0000", 10, 11, 9, 10.5, 100),
                           webull_row("2026-09-25T04:00:00.000+0000", 9, 10, 8.5, 9.8, 120)])
    assert df.index.is_monotonic_increasing and df["close"].iloc[-1] == 10.5
    assert str(df.index.tz) == "UTC"


@pytest.mark.parametrize("row", [
    webull_row("2026-09-28T04:00:00.000+0000", 10, 9, 9.5, 10, 100),       # high below open
    webull_row("2026-09-28T04:00:00.000+0000", 10, 11, 9, "", 100),        # missing close
    webull_row("2026-09-28T04:00:00.000+0000", 10, 11, 9, 10, -5),         # negative volume
    {"time": "2026-09-28T04:00:00.000+0000", "open": "1"},                 # missing fields
])
def test_bad_webull_rows_fail_closed(row):
    with pytest.raises(BarDataError):
        bars_from_webull([row])


def test_duplicate_bars_fail_closed(bars):
    with pytest.raises(BarDataError, match="duplicate"):
        validate(pd.concat([bars.iloc[:5], bars.iloc[4:5]]).sort_index())


def test_require_warm_up_and_freshness(bars):
    with pytest.raises(BarDataError, match="warm up"):
        require(bars.iloc[:100], min_bars=260)
    last = bars.index[-1]
    require(bars, min_bars=260, now=last + timedelta(days=1), max_age=pd.Timedelta(days=4))
    with pytest.raises(BarDataError, match="old"):
        require(bars, min_bars=260, now=last + timedelta(days=5), max_age=pd.Timedelta(days=4))
