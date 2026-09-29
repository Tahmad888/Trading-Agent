"""TradingView's built-in formulas, written out in plain pandas from the Pine
Script reference (ta.sma, ta.ema, ta.rma, ta.rsi, ta.macd, ta.stoch, ta.atr,
ta.dmi, ta.bb, ta.kc). The tests check the TA-Lib wrappers against these, so
a wrong setting or a changed TA-Lib default shows up as a failing test.
"""

import numpy as np
import pandas as pd


def pine_ema(src: pd.Series, n: int) -> pd.Series:
    # Pine: sum := na(sum[1]) ? src : alpha * src + (1 - alpha) * nz(sum[1])
    return src.ewm(alpha=2 / (n + 1), adjust=False).mean()


def pine_rma(src: pd.Series, n: int) -> pd.Series:
    # Pine: seeded with ta.sma(src, n), then alpha = 1 / n
    vals = src.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    start = next(i for i in range(len(vals)) if not np.isnan(vals[i]))
    seed = start + n - 1
    out[seed] = vals[start:seed + 1].mean()
    for i in range(seed + 1, len(vals)):
        out[i] = out[i - 1] + (vals[i] - out[i - 1]) / n
    return pd.Series(out, index=src.index)


def pine_rsi(close: pd.Series, n: int) -> pd.Series:
    ch = close.diff()
    up, down = pine_rma(ch.clip(lower=0), n), pine_rma((-ch).clip(lower=0), n)
    return 100 - 100 / (1 + up / down)


def pine_macd(close, fast, slow, signal):
    line = pine_ema(close, fast) - pine_ema(close, slow)
    sig = pine_ema(line, signal)
    return line, sig, line - sig


def pine_stoch(df, k, k_smooth, d):
    raw = 100 * (df["close"] - df["low"].rolling(k).min()) / (
        df["high"].rolling(k).max() - df["low"].rolling(k).min())
    slow_k = raw.rolling(k_smooth).mean()
    return slow_k, slow_k.rolling(d).mean()


def true_range(df):
    prev = df["close"].shift()
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
                     axis=1).max(axis=1).iloc[1:]


def pine_atr(df, n):
    return pine_rma(true_range(df), n).reindex(df.index)


def pine_dmi(df, di_len, adx_len):
    up, down = df["high"].diff(), -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0).iloc[1:]
    minus_dm = down.where((down > up) & (down > 0), 0.0).iloc[1:]
    tr = pine_rma(true_range(df), di_len)
    plus = 100 * pine_rma(plus_dm, di_len) / tr
    minus = 100 * pine_rma(minus_dm, di_len) / tr
    total = (plus + minus).replace(0, 1)
    adx = 100 * pine_rma(((plus - minus).abs() / total).dropna(), adx_len)
    return plus.reindex(df.index), minus.reindex(df.index), adx.reindex(df.index)


def pine_bb(close, n, mult):
    basis = close.rolling(n).mean()
    dev = mult * close.rolling(n).std(ddof=0)
    return basis + dev, basis, basis - dev


def pine_kc(df, n, mult, atr_len):
    basis = pine_ema(df["close"], n)
    rng = pine_atr(df, atr_len)
    return basis + mult * rng, basis, basis - mult * rng
