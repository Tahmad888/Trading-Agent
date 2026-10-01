"""Decision-grade bar validation, with explicit evidence and completion semantics."""
from datetime import datetime, timedelta
from typing import Annotated, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from desk.bars import BarDataError, COLUMNS, validate
from desk.calendar import ET, clock, latest_closed_session, session, sessions, trading_day
from desk.data_basis import PriceBasis, price_basis, compatible_prices


class BarProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: Annotated[str, Field(min_length=1)]
    evidence_ref: Annotated[str, Field(min_length=1)]
    timeframe: Literal["D", "M15", "M60", "W"]
    timestamp_semantics: Literal["start", "session_label"]
    session: Literal["regular"]
    delay_minutes: Annotated[int, Field(ge=0, strict=True)]
    adjustment: Literal["unadjusted", "split_adjusted", "split_dividend_adjusted", "total_return"]
    price_scale_id: Annotated[str, Field(min_length=1)]  # legacy display label; no eligibility authority
    price_basis: PriceBasis | None = None  # legacy labels remain readable, not decision evidence


def provenance(df: pd.DataFrame, timeframe: str) -> BarProvenance:
    try:
        meta = BarProvenance.model_validate(df.attrs.get("bar_provenance"))
    except ValidationError as exc:
        raise BarDataError("Unknown bar provenance/timestamp/session/adjustment semantics") from exc
    if meta.timeframe != timeframe or meta.delay_minutes != 0:
        raise BarDataError("Wrong timeframe or delayed decision data")
    expected = "session_label" if timeframe in {"D", "W"} else "start"
    if meta.timestamp_semantics != expected:
        raise BarDataError("Unexpected timestamp meaning")
    return meta


def completed_daily(df: pd.DataFrame, now: datetime, *, require_latest=True) -> pd.DataFrame:
    provenance(df, "D")
    validate(df)
    stamp = clock(now)
    local = df.index.tz_convert(ET)
    if not (local == local.normalize()).all():
        raise BarDataError("Daily bars must be ET session labels, not inferred UTC dates")
    cutoff = latest_closed_session(now)
    available = _available_at(df, stamp)
    data_cutoff = latest_closed_session(available)
    out = df[local.date <= min(cutoff, data_cutoff)].copy()
    if df.attrs.get("developing_as_of"):
        developing_day = clock(df.attrs["developing_as_of"]).date()
        out = out[out.index.tz_convert(ET).date != developing_day].copy()
    if out.empty:
        raise BarDataError("No completed daily bars")
    dates = list(out.index.tz_convert(ET).date)
    if dates != sessions(dates[0], dates[-1]):
        raise BarDataError("Missing or non-session daily bars")
    if require_latest and dates[-1] != cutoff:
        raise BarDataError(f"Stale daily data: expected completed session {cutoff}")
    price_basis(out, now)
    out.attrs["validated_at"] = stamp.isoformat()
    return out


def completed_intraday(df: pd.DataFrame, now: datetime, *, require_latest=True, session_day=None) -> pd.DataFrame:
    provenance(df, "M15")
    validate(df)
    stamp = clock(now)
    available = _available_at(df, stamp)
    day = session_day or stamp.date()
    if not trading_day(day):
        return df.iloc[:0].copy()
    opened, closed = session(day)
    # Extended-hours rows cannot enter ranges; a future/forming bar is never evidence.
    interval = pd.Timedelta(minutes=15)
    out = df[(df.index >= opened) & (df.index < closed) & (df.index + interval <= available)].copy()
    expected = pd.date_range(opened, min(stamp.floor("15min"), closed) - interval, freq=interval).tz_convert("UTC")
    if stamp < opened + interval:
        return out
    if require_latest and not out.index.equals(expected):
        raise BarDataError("missing, stale, or misaligned completed M15 session bars")
    if not out.empty:
        price_basis(out, now)
    out.attrs["validated_at"] = stamp.isoformat()
    return out


def _available_at(df, decision_clock):
    """A cached forming bar does not complete merely because wall time advances."""
    raw = df.attrs.get("received_at")
    if raw is None:
        return decision_clock  # explicit synthetic/normalized series may have no receipt field
    received = clock(raw)
    if received > decision_clock:
        raise BarDataError("Bar receipt is later than the decision clock")
    return received


def check_price_scale(basis: dict | None, intraday: pd.DataFrame, now=None, *, symbol=None):
    """Revalidate structured evidence; legacy string-only signals require rebuilding."""
    if now is None:
        raise BarDataError("Price scale verification requires an explicit decision clock")
    provenance(intraday, "M15")
    return compatible_prices(basis, intraday, now, symbol=symbol)


def _aggregate(group):
    return [group.open.iloc[0], group.high.max(), group.low.min(), group.close.iloc[-1], group.volume.sum()]


def developing_daily_from_m15(daily: pd.DataFrame, m15: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Point-in-time developing daily bar, composed only of completed regular M15s.

    A provider's eventually finalized daily close cannot stand in for a 15:45 snapshot.
    The snapshot is deliberately marked developing, not usable as a completed day.
    """
    stamp = clock(now)
    opened, closed = session(stamp.date())
    if not opened < stamp < closed:
        raise BarDataError("Developing daily snapshot requires an open regular session")
    history = completed_daily(daily, now)
    intraday = completed_intraday(m15, now)
    basis = check_price_scale(price_basis(history, now).model_dump(mode="json"), intraday, now)
    if intraday.empty:
        raise BarDataError("No completed constituents for developing daily bar")
    row = pd.DataFrame([_aggregate(intraday)], columns=COLUMNS,
                       index=pd.DatetimeIndex([stamp.normalize()]).tz_convert("UTC"))
    out = pd.concat([history, row])
    out.attrs = {**history.attrs, "developing_as_of": stamp.isoformat(),
                 "constituents_through": (intraday.index[-1] + pd.Timedelta(minutes=15)).isoformat(),
                 "developing_components": {"open": "first completed RTH M15 open; not provider daily open",
                                           "high": "maximum completed RTH M15 high; not provider daily high",
                                           "low": "minimum completed RTH M15 low; not verified equivalent to provider daily low",
                                           "close": "last completed RTH M15 close; not the finalized daily close",
                                           "volume": "sum of completed RTH M15 volumes; not provider daily volume",
                                           "volume_basis": intraday.attrs.get("volume_basis")}}
    # History and the new row are on the revalidated current action basis. Preserve
    # history's normalization (raw current-session prices need no double adjustment).
    out.attrs["bar_provenance"] = {**history.attrs["bar_provenance"], "price_basis":
        basis.model_copy(update={"normalization": price_basis(history, now).normalization}).model_dump(mode="json")}
    return out


def hourly_from_m15(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """09:30-anchored hours; the final session bar may be 30 minutes, explicitly."""
    provenance(df, "M15")
    validate(df)
    stamp = clock(now)
    rows, labels, ends = [], [], []
    dates = sorted(set(df[df.index <= stamp].index.tz_convert(ET).date))
    if not dates or dates != sessions(dates[0], dates[-1]):
        raise BarDataError("Missing hourly source sessions")
    expected_day = (stamp.date() if trading_day(stamp.date()) and stamp >= session(stamp.date())[0] + pd.Timedelta(minutes=15)
                    else latest_closed_session(now))
    if dates[-1] < expected_day:
        raise BarDataError("Stale hourly source session")
    for day in dates:
        opened, closed = session(day)
        frame = df[df.index.tz_convert(ET).date == day]
        regular = frame[(frame.index >= opened) & (frame.index < closed)]
        if day == dates[0] and day < stamp.date() and not regular.empty and regular.index[0] != opened:
            continue  # truncated leading history; never emit a partial first hour
        bars = completed_intraday(frame, stamp, session_day=day)
        start = opened
        while start < closed:
            end = min(start + pd.Timedelta(hours=1), closed)
            if end > stamp:
                break
            group = bars[(bars.index >= start) & (bars.index < end)]
            expected = pd.date_range(start, end - pd.Timedelta(minutes=15), freq="15min").tz_convert("UTC")
            if not group.index.equals(expected):
                raise BarDataError("Incomplete hourly constituents")
            rows.append(_aggregate(group)); labels.append(start); ends.append(end.isoformat())
            start = end
    if not rows:
        raise BarDataError("No completed hourly bars")
    out = pd.DataFrame(rows, columns=COLUMNS, index=pd.DatetimeIndex(labels).tz_convert("UTC"))
    out.attrs = {**df.attrs, "bar_provenance": provenance(df, "M15").model_copy(update={"timeframe": "M60"}).model_dump(),
                 "bar_ends": ends, "validated_at": stamp.isoformat()}
    price_basis(out, now)  # per-session validation alone cannot detect a split between sessions
    return out


def weekly_from_daily(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    bars = completed_daily(df, now)
    stamp = clock(now)
    dates = bars.index.tz_convert(ET).date
    weeks = [day - timedelta(days=day.weekday()) for day in dates]
    rows, labels = [], []
    for week in sorted(set(weeks)):
        days = sessions(week, week + timedelta(days=4))
        if not days or session(days[-1])[1] > stamp:
            continue  # forming week, even if its last supplied daily bar is complete
        group = bars[[w == week for w in weeks]]
        if list(group.index.tz_convert(ET).date) != days:
            if week == weeks[0]:
                continue  # a history request may start midweek; do not emit that partial week
            raise BarDataError("Incomplete weekly constituents")
        rows.append(_aggregate(group)); labels.append(pd.Timestamp(days[0], tz=ET))
    if not rows:
        raise BarDataError("No completed weekly bars")
    out = pd.DataFrame(rows, columns=COLUMNS, index=pd.DatetimeIndex(labels).tz_convert("UTC"))
    out.attrs = {**df.attrs, "bar_provenance": provenance(df, "D").model_copy(update={"timeframe": "W"}).model_dump(),
                 "validated_at": stamp.isoformat()}
    return out
