from dataclasses import replace
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from desk import scanner as sc
from desk.bar_contract import (completed_daily, completed_intraday, developing_daily_from_m15,
                               hourly_from_m15, weekly_from_daily, check_price_scale)
from desk.bars import BarDataError, bars_from_webull, validate
from desk.calendar import session, trading_day
from tests.test_scanner import daily, m15, sig, Fake, ET, frames, stamp
from tests.test_webull import bar, client, FakeTransport


def now(day="2026-09-29", time="10:00"):
    return pd.Timestamp(f"{day} {time}", tz=ET).to_pydatetime()


def intraday(count=26, day=date(2026, 9, 29)):
    return m15([(100, 102, 99, 101)] * count, day=day)


@pytest.mark.parametrize("day", [date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26),
                                date(2028, 7, 3), date(2028, 11, 24)])
def test_published_early_close_calendar_and_slots(day):
    assert session(day)[1].hour == 13
    slots = sc.scheduled_slots(day)
    assert slots[-1].hour == 13
    assert any(s.hour == 13 and s.minute == 10 for s in slots)
    assert not any(13 < s.hour <= 16 for s in slots)
    assert slots[12].time().isoformat() == "12:45:00"
    assert sc.due_slot(datetime.combine(day, datetime.min.time(), ET).replace(hour=16, minute=10)) is None


def test_calendar_holidays_dst_and_coverage():
    assert not trading_day(date(2026, 7, 3))
    assert session(date(2026, 7, 2))[1].hour == 16  # not an early close in NYSE's 2026 schedule
    assert trading_day(date(2027, 12, 31))  # Jan 1, 2028 Saturday is not observed Friday
    assert session(date(2026, 3, 6))[0].tz_convert("UTC").hour == 14
    assert session(date(2026, 3, 9))[0].tz_convert("UTC").hour == 13
    assert session(date(2026, 11, 2))[0].tz_convert("UTC").hour == 14
    with pytest.raises(BarDataError):
        sc.scheduled_slots(date(2029, 1, 2))


def test_early_close_dispatch_and_entry_end(tmp_path):
    day = date(2026, 11, 27)
    record = sc.run(Fake(frames(day)), ["LEAD"], sc.ScanLog(tmp_path), now(str(day), "13:11"))
    assert record.kind == "close" and not record.error
    record = sc.run(Fake(frames(day)), [], sc.ScanLog(tmp_path), now(str(day), "13:41"))
    assert record.kind == "leader"
    assert not sc.entry_hit(replace(sig(), as_of=pd.Timestamp("2026-11-25", tz=ET)), intraday(14, day), now(str(day), "13:00"))[0]


def test_daily_completion_is_session_based_not_wall_clock_age():
    frame = daily([100] * 10, end=date(2026, 9, 28))
    friday = completed_daily(frame, now("2026-09-28", "09:00"))
    assert friday.index[-1].tz_convert(ET).date() == date(2026, 9, 25)
    assert completed_daily(frame, now("2026-09-28", "16:00")).index[-1].tz_convert(ET).date() == date(2026, 9, 28)
    with pytest.raises(BarDataError, match="Stale daily"):
        completed_daily(frame.iloc[:-1], now("2026-09-28", "16:00"))


def test_daily_gaps_unknown_semantics_and_wrong_labels_fail():
    frame = daily([100] * 10)
    for bad in (frame.drop(frame.index[3]), frame.set_axis(frame.index.tz_convert("UTC").normalize())):
        with pytest.raises(BarDataError):
            completed_daily(bad, now(time="16:10"))
    frame.attrs.clear()
    with pytest.raises(BarDataError, match="provenance"):
        completed_daily(frame, now(time="16:10"))


def test_general_entries_ignore_forming_and_future_bars():
    frame = intraday()
    frame.iloc[2:, frame.columns.get_loc("high")] = 999
    stamp = now(time="10:07")
    assert sc.entry_hit(sig(), frame, stamp) == sc.entry_hit(sig(), frame.iloc[:2], stamp)
    assert not sc.entry_hit(sig(), frame, stamp)[0]
    assert sc.entry_hit(sig(), frame, now(time="10:15"))[0]


@pytest.mark.parametrize("index", [0, 1, 5])
def test_general_entry_requires_current_complete_session(index):
    frame = intraday(6)
    with pytest.raises(BarDataError):
        sc.entry_hit(sig(), frame.drop(frame.index[index]), now(time="11:00"))


def test_split_scale_mismatch_rebuilds_signal():
    frame = intraday(2)
    frame.attrs["bar_provenance"]["price_basis"]["actions"] = [{
        "source": "fixture", "evidence_ref": "fixture", "event_id": "split-1", "revision": "1",
        "kind": "split", "effective_session": "2026-09-29"}]
    with pytest.raises(BarDataError, match="price basis"):
        sc.entry_hit(sig(), frame, now())
    with pytest.raises(BarDataError):
        check_price_scale(None, frame)


def test_hourly_aggregation_has_no_partial_or_missing_constituents():
    frame = intraday()
    with pytest.raises(BarDataError, match="No completed hourly"):
        hourly_from_m15(frame, now(time="10:29"))
    hour = hourly_from_m15(frame, now(time="10:30"))
    assert len(hour) == 1 and hour.volume.iloc[0] == 400000
    assert hour.index[0].tz_convert(ET).strftime("%H:%M") == "09:30"
    with pytest.raises(BarDataError):
        hourly_from_m15(frame.drop(frame.index[1]), now(time="10:30"))
    complete = hourly_from_m15(frame, now(time="16:00"))
    assert len(complete) == 7 and complete.volume.iloc[-1] == 200000
    assert complete.attrs["bar_ends"][-1].startswith("2026-09-29T16:00")


def test_hourly_history_spans_sessions_and_shortened_day():
    first = intraday(26, date(2026, 11, 25))
    second = intraday(14, date(2026, 11, 27))
    hourly = hourly_from_m15(stamp(pd.concat([first, second]), "M15"), now("2026-11-27", "13:00"))
    assert len(hourly) == 11  # 7 regular-session bars plus 4 on the early close


def test_weekly_aggregation_waits_for_last_scheduled_session():
    frame = daily([100] * 20, end=date(2026, 11, 27))
    before = weekly_from_daily(frame, now("2026-11-27", "12:59"))
    after = weekly_from_daily(frame, now("2026-11-27", "13:00"))
    assert len(after) == len(before) + 1
    assert after.volume.iloc[-1] == 4 * 2000000  # Thanksgiving closed
    with pytest.raises(BarDataError):
        weekly_from_daily(frame.drop(frame.index[-2]), now("2026-11-27", "13:00"))


def test_near_close_snapshot_cannot_use_the_eventual_daily_close():
    history = daily([100] * 10)
    intraday_bars = intraday()
    snapshot = developing_daily_from_m15(history, intraday_bars, now(time="15:45"))
    history.iloc[-1] = [500, 501, 499, 500, 99999999]
    same = developing_daily_from_m15(history, intraday_bars, now(time="15:45"))
    pd.testing.assert_frame_equal(snapshot, same)
    assert snapshot.close.iloc[-1] == 101 and snapshot.volume.iloc[-1] == 25 * 100000
    assert snapshot.attrs["developing_as_of"].startswith("2026-09-29T15:45")


@pytest.mark.parametrize("delay", [None, -1, 0.5, True, "bad"])
def test_unknown_or_bad_provider_delay_cannot_be_zero(delay):
    row = {"symbol": "SPY", "delay_minutes": delay, "result": [bar("2026-09-28T04:00:00Z", 100)]}
    with pytest.raises(BarDataError):
        client(FakeTransport([row])).bars(["SPY"], category="US_ETF", timespan="D")


def test_missing_delay_and_duplicate_provider_results_rejected():
    row = {"symbol": "SPY", "result": [bar("2026-09-28T04:00:00Z", 100)]}
    with pytest.raises(BarDataError):
        client(FakeTransport([row])).bars(["SPY"], category="US_ETF", timespan="D")
    row["delay_minutes"] = 0
    with pytest.raises(BarDataError, match="duplicate"):
        client(FakeTransport([row, row])).bars(["SPY"], category="US_ETF", timespan="D")


def test_provider_parsing_is_not_evidence_of_verified_semantics():
    row = {"symbol": "SPY", "delay_minutes": 0, "result": [bar("2026-09-28T04:00:00Z", 100)]}
    frame = client(FakeTransport([row])).bars(["SPY"], category="US_ETF", timespan="D")["SPY"]
    with pytest.raises(BarDataError, match="provenance"):
        completed_daily(frame, now())


def test_timestamp_units_timezone_and_index_are_explicit():
    timestamp = 1790596800000
    with pytest.raises(BarDataError, match="explicit timestamp unit"):
        bars_from_webull([bar(timestamp, 100)])
    frame = bars_from_webull([bar(timestamp, 100)], timestamp_unit="ms")
    assert frame.index[0] == pd.Timestamp(timestamp, unit="ms", tz="UTC")
    for value in ("2026-09-28 00:00", None, "NaT"):
        with pytest.raises(BarDataError):
            bars_from_webull([bar(value, 100)])
    with pytest.raises(BarDataError):
        validate(frame.set_axis(frame.index.tz_localize(None)))


def test_cached_forming_data_cannot_become_complete_as_time_passes():
    frame = intraday()
    frame.attrs["received_at"] = now(time="10:07").isoformat()
    assert len(completed_intraday(frame, now(time="10:07"))) == 2
    with pytest.raises(BarDataError, match="stale"):
        completed_intraday(frame, now(time="10:15"))
    history = daily([100] * 10)
    history.attrs["received_at"] = now(time="15:45").isoformat()
    with pytest.raises(BarDataError, match="Stale daily"):
        completed_daily(history, now(time="16:00"))


def test_saved_developing_snapshot_is_never_a_final_daily_bar():
    snapshot = developing_daily_from_m15(daily([100] * 10), intraday(), now(time="15:45"))
    with pytest.raises(BarDataError, match="Stale daily"):
        completed_daily(snapshot, now(time="16:10"))


def test_future_receipt_cannot_be_replayed_into_earlier_decision():
    frame = intraday()
    frame.attrs["received_at"] = now(time="16:00").isoformat()
    with pytest.raises(BarDataError, match="later"):
        completed_intraday(frame, now())


def test_live_scan_samples_decision_clock_after_data_arrives():
    frame = intraday(2)
    frame.iloc[1, frame.columns.get_loc("high")] = 103
    frame.attrs["received_at"] = now(time="10:00:02").isoformat()
    source = Fake({("LEAD", "M15"): frame})
    historical = sc.intraday_scan(source, [sig()], now())
    assert not historical.triggered and "later" in historical.skipped["LEAD"]
    live = sc.intraday_scan(source, [sig()], now(), decision_clock=lambda: now(time="10:00:03"))
    assert len(live.triggered) == 1 and live.at.startswith("2026-09-29T10:00:03")


def test_hourly_history_receipt_can_follow_historical_session_close():
    frame = stamp(pd.concat([intraday(26, date(2026, 11, 25)), intraday(14, date(2026, 11, 27))]), "M15")
    frame.attrs["received_at"] = now("2026-11-27", "13:00").isoformat()
    assert len(hourly_from_m15(frame, now("2026-11-27", "13:01"))) == 11


def test_explicit_synthetic_provider_profile_can_pass_the_data_boundary():
    from desk.bar_contract import BarProvenance
    from desk.webull import WebullData
    profile = BarProvenance.model_validate(stamp(daily([100]), "D", "SPY").attrs["bar_provenance"])
    row = {"symbol": "SPY", "instrument_id": "fixture:SPY", "delay_minutes": 0,
           "result": [bar("2026-09-28T04:00:00Z", 100)]}
    source = WebullData("fixture", "fixture", transport=FakeTransport([row]), min_interval=0,
                        clock=lambda: now(time="09:59"), bar_profile=lambda symbol, tf: profile)
    frame = source.bars(["SPY"], category="US_ETF", timespan="D")["SPY"]
    assert completed_daily(frame, now()).close.tolist() == [100]
