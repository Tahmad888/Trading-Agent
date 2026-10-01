"""Bounded XNYS cash-equity session calendar; not an options expiry calendar."""
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from desk.bars import BarDataError

ET = ZoneInfo("America/New_York")
FIRST, LAST = date(2000, 1, 3), date(2028, 12, 29)


@lru_cache(maxsize=1)
def exchange():
    return xcals.get_calendar("XNYS", start=FIRST.isoformat(), end=LAST.isoformat())


def _bounded(day):
    if not FIRST <= day <= LAST:
        raise BarDataError("Date outside maintained exchange-calendar coverage (2000–2028)")


def trading_day(day: date) -> bool:
    _bounded(day)
    return bool(exchange().is_session(pd.Timestamp(day)))


def session(day: date) -> tuple[pd.Timestamp, pd.Timestamp]:
    if not trading_day(day):
        raise BarDataError(f"{day} is not an exchange session")
    label = pd.Timestamp(day)
    return exchange().session_open(label).tz_convert(ET), exchange().session_close(label).tz_convert(ET)


def sessions(start: date, end: date) -> list[date]:
    _bounded(start)
    _bounded(end)
    return [stamp.date() for stamp in exchange().sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))]


def next_trading_day(day: date) -> date:
    day += timedelta(days=1)
    while not trading_day(day):
        day += timedelta(days=1)
    return day


def previous_trading_day(day: date) -> date:
    day -= timedelta(days=1)
    while not trading_day(day):
        day -= timedelta(days=1)
    return day


def clock(now: datetime) -> pd.Timestamp:
    stamp = pd.Timestamp(now)
    if stamp.tzinfo is None or pd.isna(stamp):
        raise BarDataError("Decision clock must be timezone aware")
    _bounded(stamp.tz_convert(ET).date())
    return stamp.tz_convert(ET)


def latest_closed_session(now: datetime) -> date:
    stamp = clock(now)
    day = stamp.date()
    if trading_day(day) and stamp >= session(day)[1]:
        return day
    return previous_trading_day(day)
