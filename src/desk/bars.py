"""Price bars: parse, check, and hand to the indicator module.

Blueprint v2.3 step 3. Bars are a pandas DataFrame with columns open, high,
low, close and volume, indexed by the bar's start time in UTC, oldest first.
Anything malformed raises BarDataError, and the caller treats that as no
data, so no plan is made (fail closed).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

import numpy as np
import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]


class BarDataError(ValueError):
    """Bars are missing, malformed, too few or too old. Means no trade."""


def bars_from_webull(rows: Iterable[Mapping]) -> pd.DataFrame:
    """Parse Webull's bar rows (prices and volume come as strings, newest first)."""
    rows = list(rows)
    if not rows:
        raise BarDataError("no bars")
    try:
        df = pd.DataFrame({
            "time": pd.to_datetime([r["time"] for r in rows], utc=True),
            **{c: [float(r[c]) for r in rows] for c in COLUMNS},
        })
    except (KeyError, TypeError, ValueError) as e:
        raise BarDataError(f"bad bar row: {e}") from e
    df = df.set_index("time").sort_index()
    return validate(df)


def validate(df: pd.DataFrame) -> pd.DataFrame:
    """Reject bars a trader's screen would never show."""
    if list(df.columns) != COLUMNS:
        raise BarDataError(f"columns must be {COLUMNS}")
    if df.empty:
        raise BarDataError("no bars")
    if df.index.has_duplicates:
        raise BarDataError("duplicate bar times")
    if not df.index.is_monotonic_increasing:
        raise BarDataError("bars out of order")
    vals = df.to_numpy(dtype=float)
    if not np.isfinite(vals).all():
        raise BarDataError("missing or non-numeric values")
    if (df[["open", "high", "low", "close"]] <= 0).any().any() or (df["volume"] < 0).any():
        raise BarDataError("non-positive price or negative volume")
    if ((df["high"] < df[["open", "close", "low"]].max(axis=1))
            | (df["low"] > df[["open", "close", "high"]].min(axis=1))).any():
        raise BarDataError("high/low don't contain open and close")
    return df


def require(df: pd.DataFrame, *, min_bars: int, now: datetime | None = None,
            max_age: pd.Timedelta | None = None) -> pd.DataFrame:
    """Fail closed when there aren't enough bars to warm up, or the last is too old."""
    if len(df) < min_bars:
        raise BarDataError(f"{len(df)} bars, need at least {min_bars} to warm up the indicators")
    if max_age is not None:
        if now is None:
            raise BarDataError("max_age needs now")
        age = pd.Timestamp(now) - df.index[-1]
        if age > max_age:
            raise BarDataError(f"last bar is {age} old, limit {max_age}")
    return df
