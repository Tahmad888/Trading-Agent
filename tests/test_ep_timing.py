"""Point-in-time EP entry timing, without a time-of-day entry cutoff."""

from dataclasses import replace
from datetime import datetime, timedelta

import pandas as pd
import pytest

from desk import scanner as sc
from desk.bars import BarDataError
from tests.test_scanner import DAY, ET, Fake, m15, sig


def ep():
    return sig("5_qullamaggie_episodic_pivot", trigger=100, stop=95)


def flat_session(count=26):
    return m15([(99, 100, 98, 99)] * count)


@pytest.mark.parametrize("hour,minute", [(10, 0), (10, 45), (11, 0), (11, 15), (14, 0), (15, 45)])
def test_first_breakout_can_occur_throughout_day_one(hour, minute):
    now = datetime(2026, 9, 29, hour, minute, tzinfo=ET)
    count = int((now - datetime(2026, 9, 29, 9, 30, tzinfo=ET)).total_seconds() // 900)
    bars = flat_session(count)
    bars.iloc[-1, bars.columns.get_loc("high")] = 101
    hit, level, why = sc.entry_hit(ep(), bars, now)
    assert hit and level == 100 and "15-minute" in why


def test_both_ranges_crossed_produce_one_scanner_result():
    bars = flat_session(5)
    bars.iloc[-1, bars.columns.get_loc("high")] = 101
    now = datetime(2026, 9, 29, 10, 45, tzinfo=ET)
    result = sc.intraday_scan(Fake({("LEAD", "M15"): bars}), [ep()], now)
    assert len(result.triggered) == 1
    assert result.triggered[0]["entry_level"] == 100
    assert "60-minute" in result.triggered[0]["why"]


def test_fifteen_minute_cross_does_not_need_first_hour_to_finish():
    bars = flat_session(2)
    bars.iloc[-1, bars.columns.get_loc("high")] = 101
    hit, level, why = sc.entry_hit(ep(), bars, datetime(2026, 9, 29, 10, 0, tzinfo=ET))
    assert hit and level == 100 and "60-minute" not in why


def test_touching_range_is_not_a_breakout():
    assert not sc.entry_hit(ep(), flat_session(8), datetime(2026, 9, 29, 11, 30, tzinfo=ET))[0]


def test_future_and_forming_bar_values_do_not_change_earlier_result():
    bars = flat_session(10)
    bars.loc[bars.index[2]:, "high"] = 999
    now = datetime(2026, 9, 29, 10, 7, tzinfo=ET)
    assert sc.entry_hit(ep(), bars, now) == sc.entry_hit(ep(), bars.iloc[:2], now)
    assert not sc.entry_hit(ep(), bars, now)[0]
    assert sc.entry_hit(ep(), bars, datetime(2026, 9, 29, 10, 15, tzinfo=ET))[0]


def test_forming_opening_bar_is_not_a_range():
    assert not sc.entry_hit(ep(), flat_session(), datetime(2026, 9, 29, 9, 44, tzinfo=ET))[0]


def test_extended_hours_cannot_change_opening_range():
    bars = flat_session(2)
    bars.iloc[-1, bars.columns.get_loc("high")] = 101
    premarket = bars.iloc[:1].copy()
    premarket.index = premarket.index - pd.Timedelta(hours=2)
    premarket["high"] = 500
    now = datetime(2026, 9, 29, 10, 0, tzinfo=ET)
    assert sc.entry_hit(ep(), pd.concat([premarket, bars]), now) == sc.entry_hit(ep(), bars, now)


@pytest.mark.parametrize("missing", [0, 2, 7])
def test_missing_opening_hour_constituent_or_latest_bar_fails_closed(missing):
    bars = flat_session(8).drop(flat_session(8).index[missing])
    now = datetime(2026, 9, 29, 11, 30, tzinfo=ET)
    with pytest.raises(BarDataError, match="missing, stale"):
        sc.entry_hit(ep(), bars, now)
    rec = sc.intraday_scan(Fake({("LEAD", "M15"): bars}), [ep()], now)
    assert not rec.triggered and "LEAD" in rec.skipped


def test_unsorted_duplicate_or_misaligned_bars_fail_closed():
    bars = flat_session(4)
    now = datetime(2026, 9, 29, 10, 30, tzinfo=ET)
    misaligned = bars.copy()
    misaligned.index = misaligned.index + pd.Timedelta(minutes=1)
    for bad in (bars.iloc[::-1], pd.concat([bars, bars.iloc[-1:]]), misaligned):
        with pytest.raises(BarDataError):
            sc.entry_hit(ep(), bad, now)


def test_previous_day_signal_cannot_be_reused_on_next_day():
    tomorrow = DAY + timedelta(days=1)
    bars = m15([(99, 100, 98, 99), (99, 101, 98, 100)], tomorrow)
    assert not sc.entry_hit(ep(), bars, datetime(2026, 9, 30, 10, 0, tzinfo=ET))[0]


@pytest.mark.parametrize("hour,minute", [(9, 29), (16, 0), (17, 0)])
def test_no_regular_session_entry_outside_session(hour, minute):
    assert not sc.entry_hit(ep(), flat_session(), datetime(2026, 9, 29, hour, minute, tzinfo=ET))[0]


def test_day_one_after_a_weekend():
    monday = pd.Timestamp("2026-10-05 10:00", tz=ET)
    signal = replace(ep(), as_of=pd.Timestamp("2026-10-02", tz=ET))
    bars = m15([(99, 100, 98, 99), (99, 101, 98, 100)], monday.date())
    assert sc.entry_hit(signal, bars, monday.to_pydatetime())[0]


def test_naive_timestamps_are_not_guessed():
    now = datetime(2026, 9, 29, 10, 0, tzinfo=ET)
    bars = flat_session(2)
    with pytest.raises(BarDataError, match="timezone"):
        sc.entry_hit(ep(), bars, now.replace(tzinfo=None))
    with pytest.raises(BarDataError, match="timezone"):
        sc.entry_hit(ep(), bars.tz_localize(None), now)
