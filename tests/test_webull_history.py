"""Offline adapter checks using explicitly attributed, user-supplied market rows."""
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from desk.bar_contract import BarProvenance, completed_intraday, hourly_from_m15, provenance
from desk.bars import BarDataError
from desk.calendar import session
from desk.webull import WebullData, WebullError
from tests.test_webull import FakeTransport, bar, client
from tests.basis_support import price_evidence


def fixture():
    return json.loads((Path(__file__).parent / "fixtures/webull-spy-2025-07-03.json").read_text())


def reply(rows):
    return {"result": [{"symbol": "SPY", "delay_minutes": 0, "result": rows}]}


def epoch_ms(value):
    return int(pd.Timestamp(value).value // 1_000_000)


def test_supplied_early_close_rows_through_public_adapter_and_calendar():
    evidence = fixture()
    t = FakeTransport(reply(list(reversed(evidence["rows"]))))
    now = pd.Timestamp(evidence["observed_at"])
    source = WebullData("fixture", "fixture", transport=t, min_interval=0, clock=lambda: now)
    frame = source.bars(["SPY"], category="US_ETF", timespan="M15", count=40,
                        start_time=epoch_ms("2025-07-03T12:00Z"),
                        end_time=epoch_ms("2025-07-03T20:00Z"))["SPY"]
    payload = json.loads(t.requests[0].data)
    assert payload["trading_sessions"] == "RTH" and payload["real_time_required"] is True
    assert frame.attrs["webull_request"] == payload
    assert frame.attrs["provider_sessions"] == ["RTH"]
    opened, closed = session(date(2025, 7, 3))
    assert closed.hour == 13
    assert frame.index.equals(pd.date_range(opened, closed - pd.Timedelta(minutes=15), freq="15min").tz_convert("UTC"))
    assert len(frame) == 14
    # These observations do not establish real-time entitlement or an adjustment epoch.
    with pytest.raises(BarDataError, match="provenance"):
        completed_intraday(frame, now, session_day=opened.date())

    # Explicit TEST-ONLY profile to check calendar/aggregation on supplied price rows.
    frame.attrs["bar_provenance"] = BarProvenance(
        source="test-only historical normalization", evidence_ref=evidence["source_sha256"],
        timeframe="M15", timestamp_semantics="start", session="regular", delay_minutes=0,
        adjustment="unadjusted", price_scale_id="TEST-ONLY-single-historical-session",
        price_basis=price_evidence("2025-07-03", "SPY", "unadjusted")).model_dump(mode="json")
    assert len(completed_intraday(frame, now, session_day=opened.date())) == 14
    # Use a historical decision clock only for this offline aggregation, removing the
    # later capture clock explicitly. This is not a current trading decision.
    frame.attrs.pop("received_at")
    hours = hourly_from_m15(frame, closed)
    assert len(hours) == 4 and hours.index[-1] == pd.Timestamp("2025-07-03T16:30Z")
    assert hours.iloc[-1].volume == 3942917 + 6928816


@pytest.mark.parametrize("tag", [None, "", "ATH", "regular", 123, ["RTH"]])
def test_default_rth_rejects_missing_or_unexpected_session(tag):
    row = {**bar("2025-07-03T13:30Z", 100), "trading_session": tag}
    with pytest.raises(WebullError, match="trading_session"):
        client(FakeTransport(reply([row]))).bars(["SPY"], category="US_ETF", timespan="M15")


def test_explicit_extended_sessions_cannot_claim_regular_profile():
    rows = [{**bar("2025-07-03T13:15Z", 100), "trading_session": "PRE"},
            {**bar("2025-07-03T13:30Z", 100), "trading_session": "RTH"}]
    profile = BarProvenance(source="fixture", evidence_ref="fixture", timeframe="M15",
                           timestamp_semantics="start", session="regular", delay_minutes=0,
                           adjustment="unadjusted", price_scale_id="fixture")
    t = FakeTransport(reply(rows))
    source = WebullData("fixture", "fixture", transport=t, min_interval=0,
                        bar_profile=lambda symbol, tf: profile)
    frame = source.bars(["SPY"], category="US_ETF", timespan="M15", sessions="PRE,RTH")["SPY"]
    assert json.loads(t.requests[0].data)["trading_sessions"] == "PRE,RTH"
    assert frame.attrs["provider_sessions"] == ["PRE", "RTH"]
    with pytest.raises(BarDataError, match="provenance"):
        provenance(frame, "M15")


@pytest.mark.parametrize("params", [
    {"start_time": True}, {"start_time": "1751549400000"}, {"end_time": -1},
    {"end_time": 1.5}, {"start_time": 10**30}, {"start_time": 2, "end_time": 1},
    {"real_time_required": "false"}, {"sessions": ""}, {"sessions": "REGULAR"},
    {"sessions": "RTH,RTH"}, {"sessions": ["RTH"]},
])
def test_invalid_window_or_session_is_rejected_before_request(params):
    t = FakeTransport()
    with pytest.raises(WebullError):
        client(t).bars(["SPY"], category="US_ETF", timespan="M15", **params)
    assert not t.requests


def test_window_clips_provider_overreturn_inclusively_and_forwards_false_flag():
    rows = fixture()["rows"]
    start, end = epoch_ms(rows[1]["time"]), epoch_ms(rows[2]["time"])
    t = FakeTransport(reply(rows))
    source = client(t)
    frame = source.bars(["SPY"], category="US_ETF", timespan="M15", start_time=start,
                        end_time=end, real_time_required=False)["SPY"]
    assert len(frame) == 2
    assert frame.index[0] == pd.Timestamp(rows[1]["time"])
    assert frame.index[-1] == pd.Timestamp(rows[2]["time"])
    payload = json.loads(t.requests[0].data)
    assert payload["start_time"] == start and payload["end_time"] == end
    assert payload["real_time_required"] is False
    with pytest.raises(WebullError, match="requested window"):
        source.bars(["SPY"], category="US_ETF", timespan="M15",
                    start_time=epoch_ms("2025-07-03T20:00Z"))


def test_daily_empty_session_tag_does_not_claim_intraday_volume_equivalence():
    row = {**bar("2025-07-03T04:00Z", 100), "trading_session": ""}
    t = FakeTransport(reply([row]))
    frame = client(t).bars(["SPY"], category="US_ETF", timespan="D")["SPY"]
    assert "trading_sessions" not in json.loads(t.requests[0].data)
    assert "provider_sessions" not in frame.attrs
    assert frame.attrs["bar_provenance"]["timestamp_semantics"] == "unknown"
