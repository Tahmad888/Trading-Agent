"""Indicators, computed the way TradingView computes them.

Blueprint v2.3 section 4 and step 3. TA-Lib 0.8.1 does the arithmetic, and
every call passes its settings explicitly, because TA-Lib's defaults differ
from TradingView's. Two indicators are built on top of TA-Lib: Keltner
(TA-Lib centres its own on the typical price, TradingView on the close) and
VWAP (TA-Lib's runs across every bar it's given, TradingView's restarts each
session).

Settings are TradingView's defaults until Taz sends his (Assumption). With
the ~1,000 daily bars the desk loads (rule 6), the start-up difference between
TA-Lib's and TradingView's moving-average seeds has died out.

Plan B: if TA-Lib won't install on the runner, these become plain pandas
formulas. tests/tv_reference.py already has them, written from the Pine
Script reference, and the tests hold both versions to the same values.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import talib

from desk.bars import BarDataError
from desk.data_basis import decision_rel_volume, volume_basis

EASTERN = "America/New_York"


@dataclass(frozen=True)
class Settings:
    """Indicator settings. Defaults are TradingView's (Assumption until Taz sends his)."""
    sma_lengths: tuple[int, ...] = (5, 18, 20, 50, 150, 200)
    ema_lengths: tuple[int, ...] = (9, 10, 20, 21)
    rsi_length: int = 14
    rsi_short_length: int = 2          # Connors RSI(2)
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    stoch_k: int = 14
    stoch_k_smooth: int = 3
    stoch_d: int = 3
    atr_length: int = 14
    adx_length: int = 14               # TradingView's DMI: DI length and ADX smoothing both 14
    bb_length: int = 20
    bb_mult: float = 2.0
    kc_length: int = 20
    kc_mult: float = 2.0
    kc_atr_length: int = 10
    adr_length: int = 20
    volume_avg_length: int = 50
    year_bars: int = 252               # 52 weeks of daily bars


def _arr(s: pd.Series) -> np.ndarray:
    return s.to_numpy(dtype=np.float64)


def _series(values: np.ndarray, index: pd.Index, name: str) -> pd.Series:
    return pd.Series(values, index=index, name=name)


def sma(close: pd.Series, length: int) -> pd.Series:
    return _series(talib.SMA(_arr(close), timeperiod=length), close.index, f"sma_{length}")


def ema(close: pd.Series, length: int) -> pd.Series:
    return _series(talib.EMA(_arr(close), timeperiod=length), close.index, f"ema_{length}")


def rsi(close: pd.Series, length: int) -> pd.Series:
    """Wilder's RSI, as TradingView's ta.rsi."""
    return _series(talib.RSI(_arr(close), timeperiod=length), close.index, f"rsi_{length}")


def macd(close: pd.Series, fast: int, slow: int, signal: int) -> pd.DataFrame:
    line, sig, hist = talib.MACD(_arr(close), fastperiod=fast, slowperiod=slow, signalperiod=signal)
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": hist}, index=close.index)


def stochastic(df: pd.DataFrame, k: int, k_smooth: int, d: int) -> pd.DataFrame:
    """TradingView's Stoch: %K = SMA(raw stochastic, k_smooth), %D = SMA(%K, d)."""
    slowk, slowd = talib.STOCH(_arr(df["high"]), _arr(df["low"]), _arr(df["close"]),
                               fastk_period=k, slowk_period=k_smooth, slowk_matype=talib.MA_Type.SMA,
                               slowd_period=d, slowd_matype=talib.MA_Type.SMA)
    return pd.DataFrame({"stoch_k": slowk, "stoch_d": slowd}, index=df.index)


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    """Wilder's ATR, as TradingView's ta.atr."""
    return _series(talib.ATR(_arr(df["high"]), _arr(df["low"]), _arr(df["close"]), timeperiod=length),
                   df.index, f"atr_{length}")


def adx(df: pd.DataFrame, length: int) -> pd.DataFrame:
    h, lo, c = _arr(df["high"]), _arr(df["low"]), _arr(df["close"])
    return pd.DataFrame({
        "adx": talib.ADX(h, lo, c, timeperiod=length),
        "plus_di": talib.PLUS_DI(h, lo, c, timeperiod=length),
        "minus_di": talib.MINUS_DI(h, lo, c, timeperiod=length),
    }, index=df.index)


def bollinger(close: pd.Series, length: int, mult: float) -> pd.DataFrame:
    upper, mid, lower = talib.BBANDS(_arr(close), timeperiod=length, nbdevup=mult, nbdevdn=mult,
                                     matype=talib.MA_Type.SMA)
    return pd.DataFrame({"bb_upper": upper, "bb_mid": mid, "bb_lower": lower,
                         "bb_width": (upper - lower) / mid}, index=close.index)


def keltner(df: pd.DataFrame, length: int, mult: float, atr_length: int) -> pd.DataFrame:
    """TradingView's Keltner Channels: EMA(close, length) ± mult × ATR(atr_length)."""
    mid = ema(df["close"], length)
    band = mult * atr(df, atr_length)
    return pd.DataFrame({"kc_upper": mid + band, "kc_mid": mid, "kc_lower": mid - band}, index=df.index)


def session_vwap(df: pd.DataFrame, tz: str = EASTERN) -> pd.Series:
    """VWAP of the typical price, restarting each trading day in US Eastern time."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    day = df.index.tz_convert(tz).date
    pv = (typical * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum()
    return (pv / vol.where(vol > 0)).rename("vwap")


def anchored_vwap(df: pd.DataFrame, anchor: pd.Timestamp) -> pd.Series:
    """VWAP of the typical price from the anchor bar on; empty before it."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    after = df.index >= anchor
    pv = (typical * df["volume"]).where(after).cumsum()
    vol = df["volume"].where(after).cumsum()
    return (pv / vol.where(vol > 0)).where(after).rename("avwap")


def adr_pct(df: pd.DataFrame, length: int) -> pd.Series:
    """Average daily range in percent: mean of (high ÷ low − 1) × 100 (Assumption, from Kullamägi's use)."""
    return ((df["high"] / df["low"] - 1) * 100).rolling(length).mean().rename(f"adr_pct_{length}")


def rs_line(close: pd.Series, benchmark_close: pd.Series) -> pd.Series:
    """The stock's close ÷ the benchmark's (SPY's), on the dates both have."""
    both = pd.concat([close, benchmark_close], axis=1, join="inner").dropna()
    return (both.iloc[:, 0] / both.iloc[:, 1]).rename("rs_line")


def weighted_12m_return(close: pd.Series) -> pd.Series:
    """The common public clone of IBD's RS formula: 0.4·ROC(63) + 0.2·ROC(126, 189, 252).

    The latest quarter gets the extra weight. Assumption: it's a stand-in for
    IBD's proprietary RS Rating, ranked across a list by the leader scan.
    """
    roc = {n: close / close.shift(n) - 1 for n in (63, 126, 189, 252)}
    return (0.4 * roc[63] + 0.2 * roc[126] + 0.2 * roc[189] + 0.2 * roc[252]).rename("rs_weighted_12m")


def discovery_features(df: pd.DataFrame, s: Settings = Settings()) -> pd.DataFrame:
    """Only what the leader build's Trend Template reads (G5a checkpoint 3).

    Close, SMA50/150/200 and the 252-session high/low, with the feature pack's own
    definitions and settings. Nothing recursive is computed, so a scoped discovery
    window never yields a NaN-padded frame for general setup evaluation.
    """
    c = df["close"]
    out = pd.concat([df, sma(c, 50), sma(c, 150), sma(c, 200),
                     df["high"].rolling(s.year_bars).max().rename("high_52w"),
                     df["low"].rolling(s.year_bars).min().rename("low_52w")], axis=1)
    out.attrs = dict(df.attrs)
    return out


def daily_features(df: pd.DataFrame, s: Settings = Settings()) -> pd.DataFrame:
    """Every indicator the feature pack uses, one column each, for daily bars."""
    if df.attrs.get("history_scope"):
        # Recursive indicators (EMA, Wilder RSI/ATR/ADX) need their full warm-up.
        raise BarDataError("Discovery-scoped history cannot feed setup evaluation")
    c = df["close"]
    parts = [
        *(sma(c, n) for n in s.sma_lengths),
        *(ema(c, n) for n in s.ema_lengths),
        rsi(c, s.rsi_length), rsi(c, s.rsi_short_length),
        macd(c, s.macd_fast, s.macd_slow, s.macd_signal),
        stochastic(df, s.stoch_k, s.stoch_k_smooth, s.stoch_d),
        atr(df, s.atr_length), atr(df, s.kc_atr_length),
        adx(df, s.adx_length),
        bollinger(c, s.bb_length, s.bb_mult),
        keltner(df, s.kc_length, s.kc_mult, s.kc_atr_length),
        adr_pct(df, s.adr_length),
        (df["volume"] / df["volume"].rolling(s.volume_avg_length).mean()).rename("rel_volume"),
        df["high"].rolling(s.year_bars).max().rename("high_52w"),
        df["low"].rolling(s.year_bars).min().rename("low_52w"),
        weighted_12m_return(c),
    ]
    out = pd.concat([df] + parts, axis=1)
    out = out.loc[:, ~out.columns.duplicated()]
    out.attrs = dict(df.attrs)  # concat with indicator Series otherwise loses evidence
    try:
        basis = volume_basis(df.iloc[-1:], allow_developing=True)
        if basis.valid_from is not None:
            # Every relative-volume point needs its own full same-share rolling window.
            valid = pd.Series(df.index.tz_convert(EASTERN).date >= basis.valid_from, index=df.index)
            covered = valid.rolling(s.volume_avg_length).sum() == s.volume_avg_length
            out.loc[~covered, "rel_volume"] = np.nan
    except BarDataError:
        # Math-only inputs can still produce price indicators. Unknown or mixed
        # volume must not masquerade as a usable relative-volume observation.
        out["rel_volume"] = np.nan
    alpaca = decision_rel_volume(df, s.volume_avg_length)
    if alpaca is not None:
        # G5a: attached Alpaca SIP volume replaces Webull's in this feature only;
        # same denominator semantics, NaN where its window is not proven.
        out["rel_volume"] = alpaca
    if df.attrs.get("developing_as_of"):
        developing = pd.Timestamp(df.attrs["developing_as_of"]).tz_convert(EASTERN).date()
        out.loc[out.index.tz_convert(EASTERN).date >= developing, "rel_volume"] = np.nan
    return out


def latest(features: pd.DataFrame) -> pd.Series:
    """The last bar's values. Any missing value means the bars didn't warm up: fail closed."""
    row = features.iloc[-1]
    missing = row.index[row.isna()].tolist()
    if missing:
        raise BarDataError(f"indicators not warmed up: {missing}")
    return row
