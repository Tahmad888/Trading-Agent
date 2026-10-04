"""G5a checkpoint 3 audit repair: F1 (lost coverage), F2 (boolean OHLCV), D1 (excluded duplicates).

Astra's audit of ``33256b3`` as relayed by Taz (2026-10-04 23:29Z). Every reply is a
labelled synthetic fixture served through the real ``WebullData`` client and parser,
``VendorBasisSource``, ``VendorHistoryStore``, scanner and ``ScanLog`` (``tests/webull_sim.py``).
No provider is called. Nothing here is setup qualification or trade approval.
"""
from __future__ import annotations

from datetime import date, datetime
import json

import pytest

from desk.bars import BarRowError, bars_from_webull
from desk.calendar import ET, sessions
from desk.history_scope import DISCOVERY_POLICY, FULL_HISTORY
from tests.test_scoped_history import BUILD, FRIDAY, HOST, MARGIN, WINDOW, Desk, world
from tests.webull_sim import label, series

MONDAY = date(2026, 10, 5)
MONDAY_BUILD = datetime(2026, 10, 5, 16, 40, tzinfo=ET)


def clip(sim, symbol, first):
    """The provider stops returning every row before ``first`` (no older rows either)."""
    sim.drop(symbol, *[d for d in list(sim.world[symbol]["rows"]) if d < first])


def watchlist(desk):
    return json.loads((desk.log.root / "watchlist.json").read_text())


# ------------------------------------------------------------------ F1 ----

def test_clipped_reply_after_a_scoped_only_ready_build_fails_in_process_after_restart_and_on_repeat(tmp_path):
    sim = world("AAA")
    desk = Desk(tmp_path, sim)
    _, build = desk.build()
    assert build["status"] == "READY" and [l["symbol"] for l in build["leaders"]] == ["AAA"]
    assert desk.store.current_snapshot(HOST, "AAA") is None              # scoped-only desk: no full pointer
    pointer = desk.store.current_snapshot(HOST, "AAA", DISCOVERY_POLICY)
    clip(sim, "AAA", WINDOW[1])                                           # the last 259 sessions only
    for attempt in ("same process", "restart", "second failed refresh"):
        if attempt != "same process":
            desk = Desk(tmp_path, sim)                                    # new stores and scan log, same files
        rec, build = desk.build()
        assert build["status"] == "FAILED", attempt                      # never a healthy EMPTY
        assert build["reasons"]["AAA"].startswith(f"SCOPED_HISTORY_INCOMPLETE: accepted evidence for this "
                                                   f"instrument starts {WINDOW[0]}; the reply starts {WINDOW[1]}")
        status = rec.discovery["watchlist_build"]
        assert status["retained"] and status["generation"] == 1 and status["retained_age_hours"] == 0.0
        assert "AAA" in watchlist(desk)                                   # the previous list stays in force
        assert desk.store.current_snapshot(HOST, "AAA", DISCOVERY_POLICY) == pointer
        assert desk.store.latest(HOST, "AAA", DISCOVERY_POLICY)["status"] == "UNAVAILABLE"
        assert desk.store.current_snapshot(HOST, "AAA") is None


def test_a_stale_reply_is_refused_and_a_fresh_complete_reply_recovers(tmp_path):
    sim = world("AAA")
    desk = Desk(tmp_path, sim)
    desk.build()
    pointer = desk.store.current_snapshot(HOST, "AAA", DISCOVERY_POLICY)
    saved = dict(sim.world["AAA"]["rows"])
    sim.drop("AAA", FRIDAY)                                               # ends Thursday: stale for a Friday build
    _, build = desk.build()
    assert build["status"] == "FAILED" and build["reasons"]["AAA"].startswith("MISSING_OR_STALE_DAILY_HISTORY")
    assert desk.store.current_snapshot(HOST, "AAA", DISCOVERY_POLICY) == pointer
    clip(sim, "AAA", WINDOW[1])
    assert desk.build()[1]["status"] == "FAILED"
    sim.world["AAA"]["rows"] = saved                                      # the provider returns everything again
    rec, build = Desk(tmp_path, sim).build()
    assert build["status"] == "READY" and [l["symbol"] for l in build["leaders"]] == ["AAA"]
    assert rec.discovery["watchlist_build"]["generation"] == 2


def test_next_session_window_roll_with_a_formerly_seen_required_session_omitted(tmp_path):
    sim = world("AAA", end=MONDAY)
    _, build = Desk(tmp_path, sim).build()                                # Friday build sees WINDOW[0..259]
    assert build["status"] == "READY"
    monday_window = sessions(date(2024, 9, 1), MONDAY)[-260:]
    assert monday_window[0] == WINDOW[1]
    assert Desk(tmp_path / "control", world("AAA", end=MONDAY), MONDAY_BUILD).build()[1]["status"] == "READY"
    clip(sim, "AAA", monday_window[1])                                    # omits WINDOW[1], seen on Friday
    _, build = Desk(tmp_path, sim, MONDAY_BUILD).build()
    assert build["status"] == "FAILED"
    assert build["reasons"]["AAA"] == (f"SCOPED_HISTORY_INCOMPLETE: accepted evidence for this instrument starts "
                                       f"{WINDOW[0]}; the reply starts {monday_window[1]} (known sessions missing: 1)")


def test_a_healthy_peer_publishes_partial_without_the_clipped_name(tmp_path):
    sim = world("AAA", "BBB")
    desk = Desk(tmp_path, sim)
    assert {l["symbol"] for l in desk.build()[1]["leaders"]} == {"AAA", "BBB"}
    clip(sim, "AAA", WINDOW[1])
    _, build = Desk(tmp_path, sim).build()
    assert build["status"] == "PARTIAL" and [l["symbol"] for l in build["leaders"]] == ["BBB"]
    assert build["outcomes"]["AAA"]["outcome"] == "source_failure"
    assert "AAA" not in watchlist(desk) and "BBB" in watchlist(desk)


def test_some_candidates_evaluated_without_a_leader_stays_incomplete(tmp_path):
    sim = world("AAA", "DOWN")
    desk = Desk(tmp_path, sim)
    desk.build()
    clip(sim, "AAA", WINDOW[1])
    _, build = desk.build()
    assert build["status"] == "INCOMPLETE" and build["stage_counts"] == {
        "criterion:trend_template": 1, "source_failure:bars": 1}


def test_first_time_short_or_stopped_short_reply_is_unverified_coverage_not_a_young_listing(tmp_path):
    sim = world("AAA")
    sim.add("YOUNG", series(FRIDAY, 259, start=60))                       # really young, but nothing proves it
    sim.add("CUT", series(FRIDAY, 1100, start=70))
    sim.cap["CUT"] = 200                                                  # the provider stops short, first time
    sim.universe("AAA", "YOUNG", "CUT")
    desk = Desk(tmp_path, sim)
    _, build = desk.build()
    assert build["status"] == "PARTIAL" and [l["symbol"] for l in build["leaders"]] == ["AAA"]
    for name, first in (("YOUNG", WINDOW[1]), ("CUT", WINDOW[60])):
        outcome = build["outcomes"][name]
        assert outcome["outcome"] == "source_failure" and outcome["stage"] == "bars", name
        assert outcome["reason"].startswith(f"SCOPED_COVERAGE_UNVERIFIED: the reply starts {first}"), name


def test_capped_full_history_does_not_establish_a_listing_start(tmp_path):
    sim = world("AAA")
    sim.add("OLD", series(FRIDAY, 1100, start=70))
    sim.universe("AAA", "OLD")
    desk = Desk(tmp_path, sim)
    assert len(desk.full("OLD")["OLD"]) == 1000                           # capped: older rows may exist
    assert desk.store.coverage(HOST, "OLD", "wb:OLD")["established"] is None
    sim.add("YOUNG", series(FRIDAY, 259, start=60))
    assert len(desk.full("YOUNG")["YOUNG"]) == 259                        # uncapped: the history starts here
    assert desk.store.coverage(HOST, "YOUNG", "wb:YOUNG") == {"earliest": WINDOW[1], "established": WINDOW[1]}
    assert desk.store.coverage(HOST, "YOUNG", "wb:OTHER") == {"earliest": None, "established": None}


# ------------------------------------------------------------------ F2 ----

def rows(n=3, **override):
    days = sessions(date(2026, 9, 1), FRIDAY)[-n:]
    out = [{"time": label(d), "open": "10", "high": "11", "low": "9", "close": "10.5", "volume": "1000"} for d in days]
    out[1].update(override)
    return list(reversed(out))


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
@pytest.mark.parametrize("value", [True, False])
def test_raw_parser_refuses_boolean_ohlcv_before_conversion(field, value):
    with pytest.raises(BarRowError) as caught:
        bars_from_webull(rows(**{field: value}))
    assert caught.value.defect["field"] == field and caught.value.defect["index"] == 1
    assert caught.value.defect["reason"] == "non-numeric value" and "non-numeric value" in str(caught.value)


def test_raw_parser_keeps_numeric_strings_numbers_and_zero_volume():
    frame = bars_from_webull(rows(open=10, high=11.5, low="9.25", close=10.0, volume=0))
    assert frame["low"].tolist()[1] == 9.25 and frame["volume"].tolist()[1] == 0.0
    assert bars_from_webull(rows(volume="0"))["volume"].tolist()[1] == 0.0


def slow_world():
    """AAA plus SLOW: a steady riser that fails the Trend Template only by being less than
    30% above its 52-week low (labelled fixture). A low of 1.0 would flip that test."""
    sim = world("AAA")
    sim.add("SLOW", series(FRIDAY, 1100, start=80, growth=0.0008, wave=0.005))
    sim.universe("AAA", "SLOW")
    return sim


def test_a_boolean_required_low_cannot_make_a_trend_template_failure_a_leader(tmp_path):
    control = Desk(tmp_path / "control", slow_world())
    _, build = control.build()
    assert build["outcomes"]["SLOW"] == {"outcome": "criterion", "stage": "trend_template",
                                         "reason": "failed the Trend Template"}
    # A JSON ``true`` low inside the window read as 1.0 before the repair and made SLOW a leader.
    sim = slow_world()
    sim.row("SLOW", WINDOW[100])["low"] = True
    desk = Desk(tmp_path / "bool", sim)
    _, build = desk.build()
    assert build["status"] == "PARTIAL" and [l["symbol"] for l in build["leaders"]] == ["AAA"]
    reason = build["outcomes"]["SLOW"]["reason"]
    assert reason.startswith(f"DAILY_ROW_INVALID: non-numeric value at session {WINDOW[100]}") and "field low" in reason
    assert desk.full("SLOW") == {} and "field low" in desk.vendor.last_errors["SLOW"]
    (rejected,) = [d for d in desk.store.defects(HOST, "SLOW") if d["status"] == "REJECTED_REQUIRED_DATA"
                   and d["scope"] == DISCOVERY_POLICY]
    assert rejected["field"] == "low" and json.loads(rejected["row_json"])["low"] is True


def test_an_outside_scope_boolean_is_excluded_and_recorded_while_full_history_rejects(tmp_path):
    sim = world("AAA")
    sim.row("AAA", MARGIN[2])["volume"] = False
    desk = Desk(tmp_path, sim)
    _, build = desk.build()
    assert build["status"] == "READY" and [l["symbol"] for l in build["leaders"]] == ["AAA"]
    (kept,) = desk.store.defects(HOST, "AAA", DISCOVERY_POLICY)
    assert (kept["status"], kept["field"], kept["reason"]) == ("EXCLUDED_OUTSIDE_SCOPE", "volume", "non-numeric value")
    assert desk.full("AAA") == {}
    assert desk.vendor.last_errors["AAA"].startswith(f"DAILY_ROW_INVALID: non-numeric value at session {MARGIN[2]}")
    assert [d["field"] for d in desk.store.defects(HOST, "AAA", FULL_HISTORY)] == ["volume"]


# ------------------------------------------------------------------ D1 ----

def test_an_old_duplicate_is_recorded_and_the_ticker_still_ranks(tmp_path):
    sim = world("AAA", mode="excess")
    old = sessions(date(2023, 1, 1), FRIDAY)[100]
    sim.extra["AAA"] = [dict(sim.row("AAA", old))]
    desk = Desk(tmp_path, sim)
    _, build = desk.build()
    assert build["status"] == "READY" and [l["symbol"] for l in build["leaders"]] == ["AAA"]
    report = build["history_scope"]["excluded_outside_scope"]["AAA"]
    assert report["excluded_defect_count"] == 1
    (kept,) = desk.store.defects(HOST, "AAA", DISCOVERY_POLICY)
    assert kept["status"] == "EXCLUDED_OUTSIDE_SCOPE" and kept["field"] == "time"
    assert kept["reason"].startswith("duplicate bar times (also row ") and kept["row_time"].startswith(str(old)[:4])


def test_a_required_duplicate_still_rejects_the_ticker(tmp_path):
    sim = world("AAA", "BBB")
    sim.extra["AAA"] = [dict(sim.row("AAA", WINDOW[100]))]
    _, build = Desk(tmp_path, sim).build()
    assert build["status"] == "PARTIAL" and [l["symbol"] for l in build["leaders"]] == ["BBB"]
    assert build["outcomes"]["AAA"]["reason"].startswith(f"DAILY_ROW_INVALID: duplicate bar times")
    assert str(WINDOW[100]) in build["outcomes"]["AAA"]["reason"]
