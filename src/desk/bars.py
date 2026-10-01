"""Price bars: parse, check, and hand to the indicator module.

Blueprint v2.3 step 3. Bars are a pandas DataFrame with columns open, high,
low, close and volume, indexed by the bar's start time in UTC, oldest first.
Anything malformed raises BarDataError, and the caller treats that as no
data, so no plan is made (fail closed).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from numbers import Real

import numpy as np
import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]


class BarDataError(ValueError):
    """Bars are missing, malformed, too few or too old. Means no trade."""


def bars_from_webull(rows: Iterable[Mapping], *, timestamp_unit: str | None = None) -> pd.DataFrame:
    """Parse Webull's bar rows (prices and volume come as strings, newest first)."""
    rows = list(rows)
    if not rows:
        raise BarDataError("no bars")
    try:
        times = []
        for row in rows:
            raw = row["time"]
            numeric = isinstance(raw, Real) or (isinstance(raw, str) and raw.lstrip("-").isdigit())
            if numeric:
                if isinstance(raw, bool) or timestamp_unit not in {"s", "ms", "us", "ns"}:
                    raise ValueError("numeric bar time needs an explicit timestamp unit")
                if isinstance(raw, Real) and raw != int(raw):
                    raise ValueError("fractional timestamps require an exact integer in a finer unit")
                stamp = pd.to_datetime(int(raw), unit=timestamp_unit, utc=True)
            else:
                stamp = pd.Timestamp(raw)
                if stamp.tzinfo is None:
                    raise ValueError("bar time must include a timezone")
            times.append(stamp)
        df = pd.DataFrame({
            "time": pd.to_datetime(times, utc=True),
            **{c: [float(r[c]) for r in rows] for c in COLUMNS},
        })
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        raise BarDataError(f"bad bar row: {e}") from e
    df = df.set_index("time").sort_index()
    return validate(df)


def validate(df: pd.DataFrame) -> pd.DataFrame:
    """Reject bars a trader's screen would never show."""
    if list(df.columns) != COLUMNS:
        raise BarDataError(f"columns must be {COLUMNS}")
    if df.empty:
        raise BarDataError("no bars")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None or df.index.hasnans:
        raise BarDataError("Bar index must contain timezone-aware timestamps")
    if df.index.has_duplicates:
        raise BarDataError("duplicate bar times")
    if not df.index.is_monotonic_increasing:
        raise BarDataError("bars out of order")
    if not all(pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c]) for c in COLUMNS):
        raise BarDataError("Bar columns must be numeric")
    try:
        vals = df.to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise BarDataError("Non-numeric bars") from exc
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
    validate(df)
    if len(df) < min_bars:
        raise BarDataError(f"{len(df)} bars, need at least {min_bars} to warm up the indicators")
    if max_age is not None:
        if now is None:
            raise BarDataError("max_age needs now")
        if pd.Timestamp(now).tzinfo is None:
            raise BarDataError("freshness clock must be timezone aware")
        age = pd.Timestamp(now) - df.index[-1]
        if age < pd.Timedelta(0) or age > max_age:
            raise BarDataError(f"last bar is {age} old, limit {max_age}")
    return df
