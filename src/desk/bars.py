"""Price bars: parse, check, and hand to the indicator module.

Blueprint v2.3 step 3. Bars are a pandas DataFrame with columns open, high,
low, close and volume, indexed by the bar's start time in UTC, oldest first.
Anything malformed raises BarDataError, and the caller treats that as no
data, so no plan is made (fail closed).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from numbers import Real

import numpy as np
import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]


class BarDataError(ValueError):
    """Bars are missing, malformed, too few or too old. Means no trade."""


class BarRowError(BarDataError):
    """A bar defect attributable to one provider row (index, time, field, reason)."""

    def __init__(self, message: str, defect: dict):
        super().__init__(message)
        self.defect = defect


def parse_time(raw, *, timestamp_unit: str | None = None) -> pd.Timestamp:
    """One bar label under the timestamp contract: ISO text with a zone, or an integer
    in an explicitly configured unit. Anything else raises ValueError."""
    numeric = isinstance(raw, Real) or (isinstance(raw, str) and raw.lstrip("-").isdigit())
    if numeric:
        if isinstance(raw, bool) or timestamp_unit not in {"s", "ms", "us", "ns"}:
            raise ValueError("numeric bar time needs an explicit timestamp unit")
        if isinstance(raw, Real) and raw != int(raw):
            raise ValueError("fractional timestamps require an exact integer in a finer unit")
        return pd.to_datetime(int(raw), unit=timestamp_unit, utc=True)
    if not isinstance(raw, str):
        raise ValueError("bar time must be text or an integer")
    stamp = pd.Timestamp(raw)
    if stamp.tzinfo is None:
        raise ValueError("bar time must include a timezone")
    return stamp.tz_convert("UTC")


def row_defect(row: Mapping) -> tuple[str, str] | None:
    """The first OHLCV defect of one row as (field, reason), or None. Time is separate."""
    values = {}
    for c in COLUMNS:
        if c not in row:
            return c, "missing field"
        try:
            if isinstance(row[c], bool):
                raise TypeError
            values[c] = float(row[c])
        except (TypeError, ValueError, OverflowError):
            return c, "non-numeric value"
        if not np.isfinite(values[c]):
            return c, "missing or non-numeric values"
    for c in ("open", "high", "low", "close"):
        if values[c] <= 0:
            return c, "non-positive price or negative volume"
    if values["volume"] < 0:
        return "volume", "non-positive price or negative volume"
    if values["high"] < max(values["open"], values["close"], values["low"]):
        return "high", "high/low don't contain open and close"
    if values["low"] > min(values["open"], values["close"], values["high"]):
        return "low", "high/low don't contain open and close"
    return None


def sanitized(row: Mapping) -> dict:
    """Only the label and OHLCV of a provider row, as text, for a bounded defect record."""
    return {k: (row.get(k) if isinstance(row.get(k), (str, int, float)) or row.get(k) is None
                else type(row.get(k)).__name__) for k in ("time", *COLUMNS) if k in row}


def defects(rows: Sequence[Mapping], *, timestamp_unit: str | None = None) -> list[dict]:
    """Every row defect in provider order: {"index", "time", "field", "reason"}."""
    seen, out = {}, []
    for i, row in enumerate(rows):
        try:
            if not isinstance(row, Mapping) or "time" not in row:
                raise ValueError("missing bar time")
            stamp = parse_time(row["time"], timestamp_unit=timestamp_unit)
        except (TypeError, ValueError, OverflowError) as e:
            out.append({"index": i, "time": None, "field": "time", "reason": f"invalid bar time: {e}"})
            continue
        if stamp in seen:
            out.append({"index": i, "time": stamp.isoformat(), "field": "time",
                        "reason": f"duplicate bar times (also row {seen[stamp]})"})
        seen.setdefault(stamp, i)
        found = row_defect(row)
        if found:
            out.append({"index": i, "time": stamp.isoformat(), "field": found[0], "reason": found[1]})
    return out


def diagnose(rows: Sequence[Mapping], *, timestamp_unit: str | None = None, error: str = "") -> dict | None:
    """The defect behind ``error`` (else the first in provider order), or None."""
    found = defects(rows, timestamp_unit=timestamp_unit)
    return next((d for d in found if d["reason"].split(" (")[0] in error), found[0] if found else None)


def describe(defect: dict, total: int | None = None) -> str:
    where = defect.get("time") or "an unreadable time"
    of = f" of {total}" if total is not None else ""
    return f"{defect['reason']} at {where} (row {defect['index']}{of}, field {defect['field']})"


def bars_from_webull(rows: Iterable[Mapping], *, timestamp_unit: str | None = None) -> pd.DataFrame:
    """Parse Webull's bar rows (prices and volume come as strings, newest first).

    A rejection names the first offending row (index in provider order, timestamp,
    field and reason) as ``BarRowError.defect`` (G5a checkpoint 3 diagnostics).
    """
    rows = list(rows)
    if not rows:
        raise BarDataError("no bars")
    try:
        try:
            times = [parse_time(row["time"], timestamp_unit=timestamp_unit) for row in rows]
            df = pd.DataFrame({
                "time": pd.to_datetime(times, utc=True),
                **{c: [float(r[c]) for r in rows] for c in COLUMNS},
            })
        except (KeyError, TypeError, ValueError, OverflowError) as e:
            raise BarDataError(f"bad bar row: {e}") from e
        df = df.set_index("time").sort_index()
        return validate(df)
    except BarRowError:
        raise
    except BarDataError as e:
        defect = diagnose(rows, timestamp_unit=timestamp_unit, error=str(e))
        if defect is None:
            raise
        defect = {**defect, "rows": len(rows), "row": sanitized(rows[defect["index"]])
                  if isinstance(rows[defect["index"]], Mapping) else None}
        raise BarRowError(f"{e}: {describe(defect, len(rows))}", defect) from e


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
