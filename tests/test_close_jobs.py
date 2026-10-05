"""Live-run package 2: recoverable unfinished close preparation (offline, synthetic).

Synthetic Webull-shaped fixtures only (``tests.test_scanner.Fake``); no network, no order.
The 2026-10-05 shape is replayed: completed daily bars missing at 16:14 and 16:26,
present at 16:36, for a Monday close whose target is Tuesday.
"""
from datetime import date, datetime, timedelta
import json
from threading import Barrier, Thread

import numpy as np
import pytest

from desk import close_jobs as cj
from desk import scanner as sc
from desk.calendar import previous_trading_day
from desk.signal_state import SignalStore
from desk.vendor_basis import VendorBasisSource, VendorHistoryStore
from tests.test_scanner import ET, UP, Fake, daily, frames, m15
from tests.test_triggers import QULL

MON, TUE = date(2026, 10, 5), date(2026, 10, 6)
LISTS = {"DAY_1": [{"symbol": "MOVR", "price": 50, "change_ratio": 0.25}],
         ("losers", "DAY_1"): [{"symbol": "DOWN", "price": 40, "change_ratio": -0.1}]}
WATCH = ["LEAD", "IWM"]
UNIVERSE = {"LEAD", "IWM", "MOVR", "DOWN"}
BREAKOUT = [(147, 148.5, 146.5, 148), (148, 153, 148, 152.5)]


def at(day, hour, minute):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def charts(day, late=(), extra=None):
    """Daily charts through ``day``; names in ``late`` end one trading session earlier."""
    fr = {**frames(day), ("MOVR", "D"): daily(QULL, spread=0.02, end=day),
          ("DOWN", "D"): daily(np.full(len(QULL), 100.0), end=day)}
    for (symbol, span) in list(fr):
        if symbol in late and span == "D":
            fr[(symbol, span)] = daily(QULL if symbol in ("LEAD", "MOVR") else UP, spread=0.02,
                                       end=previous_trading_day(day))
    fr.update(extra or {})
    return fr


ALL = ("SPY", "QQQ", "IWM", "LEAD", "MOVR", "DOWN")


def symbols_requested(src, start=0):
    return set().union(*(set(c[0]) for c in src.calls[start:] if c[2] == "D")) if src.calls[start:] else set()


def kinds(log):
    return [(r["kind"], bool(r.get("error"))) for r in log.records()]


# ---- the 2026-10-05 shape ----------------------------------------------------------------------
def test_bars_late_until_after_the_slot_window_then_the_full_universe_prepares_for_tuesday(tmp_path):
    log, src = sc.ScanLog(tmp_path), Fake(charts(MON, late=ALL), lists=LISTS)
    first = sc.run(src, WATCH, log, at(MON, 16, 14))
    assert first.kind == "close" and first.error.startswith("close preparation pending")
    job = log.signals.close_job(MON)
    assert (job["source_session"], job["target_session"], job["status"]) == ("2026-10-05", "2026-10-06", "PENDING")
    assert UNIVERSE <= set(job["universe"]) and job["universe"]["MOVR"] == ["mover"]
    assert {o["status"] for o in job["outcomes"].values()} == {"WAITING_LATEST_SESSION"}
    assert log.signals.load_armed(TUE) is None                      # nothing published, nothing overwritten
    again = sc.run(src, WATCH, log, at(MON, 16, 26))                # slot window over: recovery
    assert again.kind == "close_recovery" and again.error.startswith("close preparation pending")
    src.frames = charts(MON)
    done = sc.run(src, WATCH, log, at(MON, 16, 36))
    assert done.kind == "close_recovery" and not done.error, done.error
    job = log.signals.close_job(MON)
    assert (job["status"], job["coverage"], job["attempts"]) == ("FINISHED", "FULL", 3)
    assert {s for s, o in job["outcomes"].items() if o["status"] == "PREPARED"} >= UNIVERSE
    market, armed = log.load_armed(TUE)
    assert market.value == "full" and "1_qullamaggie_breakout" in {s.setup_id for s in armed if s.symbol == "LEAD"}
    assert log.signals.load_armed(MON) is None                      # never saved under the source date
    assert kinds(log) == [("close", True), ("close_recovery", True), ("close_recovery", False)]
    assert sc.run(src, WATCH, log, at(MON, 16, 45)) is None         # finished: nothing more to do
    fun = sc.funnel(log.records(), MON, MON)
    assert fun["scans_failed"] == [first.slot] and fun["close_recovery_attempts"] == 2 and fun["setups_armed"] >= 1


def test_only_an_otherwise_contiguous_history_one_session_behind_waits():
    gap = daily(QULL, spread=0.02, end=MON)
    gap = gap.drop(gap.index[-5])
    fr = charts(MON, late=("MOVR",), extra={("LEAD", "D"): daily(QULL, spread=0.02, end=date(2026, 10, 1)),
                                            ("IWM", "D"): gap})
    rec, _ = sc.close_scan(Fake(fr), ["LEAD", "IWM", "MOVR"], at(MON, 16, 14), preparing=True)
    assert cj.classify(rec.skipped["MOVR"]) == "WAITING_LATEST_SESSION"
    assert cj.classify(rec.skipped["LEAD"]) == "INVALID_HISTORY"        # two sessions behind: plain stale
    assert cj.classify(rec.skipped["IWM"]) == "INVALID_HISTORY"         # an older gap
    assert "LATEST_SESSION_NOT_YET_AVAILABLE" not in rec.skipped["LEAD"] + rec.skipped["IWM"]


def test_the_vendor_path_emits_the_same_readiness_code(tmp_path):
    from tests.test_vendor_basis import Native, source
    native = Native()
    native.now = datetime(2026, 9, 29, 16, 30, tzinfo=ET)            # its daily rows end 2026-09-28
    out = source(tmp_path, native).bars(["LEAD"], timespan="D", category="US_STOCK")
    vendor = source(tmp_path, native)
    vendor.bars(["LEAD"], timespan="D", category="US_STOCK")
    assert not out and cj.classify(vendor.last_errors["LEAD"]) == "WAITING_LATEST_SESSION"
    native.now = datetime(2026, 9, 30, 16, 30, tzinfo=ET)            # two sessions behind
    vendor = source(tmp_path / "b", native)
    vendor.bars(["LEAD"], timespan="D", category="US_STOCK")
    assert "LATEST_SESSION_NOT_YET_AVAILABLE" not in vendor.last_errors["LEAD"]

    class Gapped(Native):                                              # one session behind, older gap
        def bars(self, symbols, *, timespan, **kwargs):
            out = super().bars(symbols, timespan=timespan, **kwargs)
            return {s: f.drop(f.index[1]) for s, f in out.items()} if timespan == "D" else out
    gapped = Gapped()
    gapped.now = datetime(2026, 9, 29, 16, 30, tzinfo=ET)
    vendor = source(tmp_path / "c", gapped)
    vendor.bars(["LEAD"], timespan="D", category="US_STOCK")
    assert "LATEST_SESSION_NOT_YET_AVAILABLE" not in vendor.last_errors["LEAD"]
    assert cj.classify(vendor.last_errors["LEAD"]) != "WAITING_LATEST_SESSION"


def test_classification_uses_structured_codes():
    assert cj.classify("DAILY_PROVIDER_UNAVAILABLE: PROVIDER_STOP_HTTP_429") == "PROVIDER_STOP"
    assert cj.classify("metadata unavailable: Webull HTTP 403 api.webull.com/x: access denied") == "PROVIDER_STOP"
    assert cj.classify("DAILY_PROVIDER_UNAVAILABLE") == "TRANSIENT_UNAVAILABLE"
    assert cj.classify("MINUTE_PROVIDER_UNAVAILABLE") == "TRANSIENT_UNAVAILABLE"
    assert cj.classify("IDENTITY_STORE_UNAVAILABLE: nothing requested") == "TRANSIENT_UNAVAILABLE"
    assert cj.classify("SECURITY_IDENTITY_CHANGED") == "IDENTITY_REFUSED"
    assert cj.classify("bar identity disagrees with security metadata") == "IDENTITY_REFUSED"
    assert cj.classify("DAILY_ROW_INVALID: high/low don't contain open and close") == "INVALID_HISTORY"
    assert cj.classify("MISSING_OR_STALE_DAILY_HISTORY: last session 2026-10-02, required 2026-10-05") \
        == "INVALID_HISTORY"                                           # the broad prefix alone never waits
    assert cj.classify("unsupported security type: WARRANT") == "REFUSED"


# ---- isolation, partial publication and the market reference --------------------------------------
def test_market_late_then_usable_while_one_name_stays_late_and_refusals_stay_isolated(tmp_path):
    bad = daily(QULL, spread=0.02, end=MON)
    bad.iloc[10, bad.columns.get_loc("high")] = float(bad["open"].iloc[10]) * 0.5
    idnt = daily(QULL, spread=0.02, end=MON)
    idnt.attrs["provider_identity"] = {"symbol": "IDNT", "instrument_id": "someone-else"}
    extra = {("BADO", "D"): bad, ("IDNT", "D"): idnt}
    watch = [*WATCH, "BADO", "IDNT"]
    log, src = sc.ScanLog(tmp_path), Fake(charts(MON, late=ALL, extra=extra), lists=LISTS)
    sc.run(src, watch, log, at(MON, 16, 14))
    src.frames = charts(MON, late=("MOVR",), extra=extra)
    partial = sc.run(src, watch, log, at(MON, 16, 26))
    job = log.signals.close_job(MON)
    assert job["status"] == "PARTIAL" and not partial.error
    outcome = {s: o["status"] for s, o in job["outcomes"].items()}
    assert outcome["MOVR"] == "WAITING_LATEST_SESSION" and outcome["LEAD"] == "PREPARED"
    assert outcome["BADO"] == "INVALID_HISTORY" and outcome["IDNT"] == "IDENTITY_REFUSED"
    assert any(s.symbol == "LEAD" for s in log.load_armed(TUE)[1])     # healthy peers published
    src.frames = charts(MON, extra=extra)
    start = len(src.calls)
    sc.run(src, watch, log, at(MON, 16, 36))
    assert symbols_requested(src, start) == {"MOVR", "SPY", "QQQ"}       # only unresolved + market
    job = log.signals.close_job(MON)
    last = job["history"][-1]
    assert (last["requested"], last["symbols_requested"], last["alpaca_requests"]) == (["MOVR"], 3, 0)
    assert (job["status"], job["coverage"]) == ("FINISHED", "PARTIAL")  # refusals: not fully complete


def test_a_committed_preparation_survives_a_later_failed_attempt(tmp_path):
    log, src = sc.ScanLog(tmp_path), Fake(charts(MON, late=("MOVR",)), lists=LISTS)
    sc.run(src, WATCH, log, at(MON, 16, 14))
    before = log.signals.load_armed(TUE)
    assert before is not None and before[0] == "full"
    src.frames = {k: v for k, v in charts(MON).items() if k[0] not in ("SPY", "QQQ")}  # market reference gone
    failed = sc.run(src, WATCH, log, at(MON, 16, 26))
    assert failed.error.startswith("close preparation pending")
    assert log.signals.load_armed(TUE) == before                      # nothing replaced with empty/null
    assert log.signals.close_job(MON)["status"] == "PARTIAL"


def test_a_genuinely_empty_preparation_differs_from_pending(tmp_path):
    flat = {("LEAD", "D"): daily(np.full(len(QULL), 100.0), end=MON)}
    log = sc.ScanLog(tmp_path)
    sc.run(Fake(charts(MON, extra=flat), lists={}), ["LEAD"], log, at(MON, 16, 14))
    market, payload = log.signals.load_armed(TUE)
    assert market == "full" and not [p for p in payload if p["symbol"] == "LEAD"]
    assert log.signals.close_job(MON)["outcomes"]["LEAD"]["status"] == "PREPARED"   # evaluated, no setup
    pending = sc.ScanLog(tmp_path / "pending")
    sc.run(Fake(charts(MON, late=ALL), lists={}), ["LEAD"], pending, at(MON, 16, 14))
    assert pending.signals.load_armed(TUE) is None                    # not evaluated: no row at all


# ---- exactly once: duplicates, reopening, crashes, two writers, reversed completion ----------------
def test_a_duplicate_invocation_reports_pending_without_requests(tmp_path):
    log, src = sc.ScanLog(tmp_path), Fake(charts(MON, late=ALL), lists=LISTS)
    sc.run(src, WATCH, log, at(MON, 16, 14))
    calls = len(src.calls)
    dup = sc.run(src, WATCH, log, at(MON, 16, 15))
    assert dup.error.startswith("CLOSE_JOB_NOT_DUE") and len(src.calls) == calls
    later = sc.run(src, WATCH, log, at(MON, 16, 50))                 # spacing elapsed: one new attempt
    assert later.kind == "close_recovery" and len(src.calls) > calls
    assert log.signals.close_job(MON)["attempts"] == 2
    reopened = sc.ScanLog(tmp_path)
    assert reopened.signals.close_job(MON)["attempts"] == log.signals.close_job(MON)["attempts"]


def test_a_crashed_attempt_holds_its_lease_then_the_job_resumes(tmp_path):
    log, src = sc.ScanLog(tmp_path), Fake(charts(MON, late=ALL), lists=LISTS)
    sc.run(src, WATCH, log, at(MON, 16, 14))
    assert log.signals.claim_close_job(MON, at(MON, 16, 26), "crashed", at(MON, 16, 36))[0]
    calls = len(src.calls)
    held = sc.run(src, WATCH, log, at(MON, 16, 30))
    assert held.error == "CLOSE_JOB_CLAIMED_BY_ANOTHER_ATTEMPT" and len(src.calls) == calls
    src.frames = charts(MON)
    resumed = sc.run(src, WATCH, log, at(MON, 16, 37))
    assert not resumed.error and log.signals.close_job(MON)["status"] == "FINISHED"


def test_two_writers_claim_once_and_an_older_attempt_cannot_commit(tmp_path):
    log = sc.ScanLog(tmp_path)
    sc.run(Fake(charts(MON, late=ALL), lists=LISTS), WATCH, log, at(MON, 16, 14))
    stores = [SignalStore(tmp_path / "signals.sqlite") for _ in range(2)]
    barrier, results = Barrier(2), []

    def claim(store, token):
        barrier.wait()
        results.append(store.claim_close_job(MON, at(MON, 16, 26), token, at(MON, 16, 36))[0])
    threads = [Thread(target=claim, args=(s, f"t{i}")) for i, s in enumerate(stores)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    # Reversed completion: the lease expires, a newer attempt commits, the older one cannot.
    winner = "t0" if log.signals.close_job(MON)["claim_token"] == "t0" else "t1"
    assert log.signals.claim_close_job(MON, at(MON, 16, 37), "newer", at(MON, 16, 47))[0]
    common = dict(outcomes={}, market="full", payload=[], volume_pending=[], status="PARTIAL", coverage="PARTIAL",
                  next_attempt_at=at(MON, 16, 42), blocked_reason=None, floor_after=None, record={})
    assert log.signals.commit_close_job(MON, "newer", at(MON, 16, 38), **common)
    assert not log.signals.commit_close_job(MON, winner, at(MON, 16, 39), **{**common, "market": "half"})
    assert log.signals.load_armed(TUE)[0] == "full"


# ---- calendar boundaries ---------------------------------------------------------------------------
@pytest.mark.parametrize("source,slot,target,recover_at", [
    (date(2026, 10, 2), (16, 10), date(2026, 10, 5), datetime(2026, 10, 3, 12, 0, tzinfo=ET)),     # Fri→Mon
    (date(2026, 11, 25), (16, 10), date(2026, 11, 27), datetime(2026, 11, 26, 9, 0, tzinfo=ET)),   # holiday
    (date(2026, 11, 27), (13, 10), date(2026, 11, 30), datetime(2026, 11, 28, 10, 0, tzinfo=ET)),  # early close
    (date(2026, 10, 30), (16, 10), date(2026, 11, 2), datetime(2026, 11, 1, 23, 30, tzinfo=ET)),   # DST ends
    (MON, (16, 10), TUE, datetime(2026, 10, 6, 0, 30, tzinfo=ET)),                                  # after midnight
])
def test_frozen_sessions_across_calendar_boundaries(tmp_path, source, slot, target, recover_at):
    log, src = sc.ScanLog(tmp_path), Fake(charts(source, late=ALL), lists=LISTS)
    first = sc.run(src, WATCH, log, at(source, *slot))
    assert first.kind == "close"
    job = log.signals.close_job(source)
    assert (job["source_session"], job["target_session"]) == (source.isoformat(), target.isoformat())
    src.frames = charts(source)
    rec = sc.run(src, WATCH, log, recover_at)
    assert rec.kind == "close_recovery" and not rec.error, rec.error
    assert log.signals.close_job(source)["target_session"] == target.isoformat()
    assert log.signals.load_armed(target)[0] == "full"


def test_an_expired_target_session_receives_nothing(tmp_path):
    log, src = sc.ScanLog(tmp_path), Fake(charts(MON, late=ALL), lists=LISTS)
    sc.run(src, WATCH, log, at(MON, 16, 14))
    src.frames = charts(MON)
    calls = len(src.calls)
    rec = sc.run(src, WATCH, log, at(TUE, 16, 5))                     # Tuesday has closed
    assert rec.error == "CLOSE_JOB_EXPIRED: TARGET_SESSION_ENDED" and len(src.calls) == calls
    assert log.signals.close_job(MON)["status"] == "EXPIRED" and log.signals.load_armed(TUE) is None


# ---- removals, provider stops, normal jobs, no replay ----------------------------------------------
def test_a_user_removal_after_capture_is_applied(tmp_path):
    log, src = sc.ScanLog(tmp_path), Fake(charts(MON, late=ALL), lists=LISTS)
    sc.run(src, WATCH, log, at(MON, 16, 14))
    (tmp_path / "taz-picks.json").write_text(json.dumps({"add": [], "remove": ["MOVR"]}))
    src.frames = charts(MON)
    start = len(src.calls)
    sc.run(src, WATCH, log, at(MON, 16, 36))
    assert "MOVR" not in symbols_requested(src, start)
    assert log.signals.close_job(MON)["outcomes"]["MOVR"]["status"] == "REMOVED"
    assert not [s for s in log.load_armed(TUE)[1] if s.symbol == "MOVR"]


def test_a_rate_limit_blocks_until_an_operator_resumes(tmp_path, capsys):
    log, src = sc.ScanLog(tmp_path), Fake(charts(MON), fail={"MOVR"}, lists=LISTS)
    sc.run(src, WATCH, log, at(MON, 16, 14))
    job = log.signals.close_job(MON)
    assert job["status"] == "BLOCKED" and "429" in job["blocked_reason"]
    assert any(s.symbol == "LEAD" for s in log.load_armed(TUE)[1])     # healthy peers still published
    calls = len(src.calls)
    assert sc.run(src, WATCH, log, at(MON, 16, 36)) is None and len(src.calls) == calls  # no blind retry
    assert cj.main(["status", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "BLOCKED"
    assert cj.main(["resume", "--data-dir", str(tmp_path), "--source-session", "2026-10-05"]) == 0
    capsys.readouterr()
    src.fail = set()
    rec = sc.run(src, WATCH, log, at(MON, 16, 40))
    assert not rec.error and log.signals.close_job(MON)["status"] == "FINISHED"


def test_intraday_and_leader_jobs_still_run_beside_a_pending_recovery(tmp_path):
    fri = date(2026, 10, 2)
    log, src = sc.ScanLog(tmp_path), Fake(charts(fri, late=("MOVR",)), lists=LISTS)
    sc.run(src, WATCH, log, at(fri, 16, 14))
    assert log.signals.close_job(fri)["status"] == "PARTIAL"
    leader = sc.run(src, WATCH, log, at(fri, 16, 41))
    assert leader.kind == "leader"
    assert [r["kind"] for r in log.records()][-2:] == ["close_recovery", "leader"]
    intraday = sc.run(src, WATCH, log, at(date(2026, 10, 5), 10, 1))
    assert intraday.kind == "intraday" and intraday.slot.startswith("2026-10-05T10:00")


def test_a_mid_session_publication_never_replays_earlier_crossings(tmp_path):
    def prepared_then_intraday(recover_at):
        log = sc.ScanLog(tmp_path / recover_at.isoformat()[:13])
        src = Fake(charts(MON, late=ALL), lists=LISTS)
        sc.run(src, WATCH, log, at(MON, 16, 14))
        src.frames = {**charts(MON), ("LEAD", "M15"): m15(BREAKOUT, TUE)}
        rec = sc.run(src, WATCH, log, recover_at)
        if recover_at != at(TUE, 10, 1):
            rec = sc.run(src, WATCH, log, at(TUE, 10, 1))
        assert rec.kind == "intraday"   # at 10:01 the recovery and the 10:00 slot share one invocation
        return rec, log
    control, _ = prepared_then_intraday(at(MON, 16, 36))              # published before Tuesday's open
    assert any(t["setup_id"] == "1_qullamaggie_breakout" for t in control.triggered)
    late, log = prepared_then_intraday(at(TUE, 10, 1))                # published at 10:01 Tuesday
    assert not [t for t in late.triggered if t["symbol"] == "LEAD"]  # the 09:45 crossing is not replayed
    floors = log.signals._db
    with floors() as db:
        assert db.execute("SELECT COUNT(*) FROM armed_floors").fetchone()[0] >= 1


def test_spacing_configuration_is_bounded():
    assert cj.spacing({}) == timedelta(minutes=5)
    assert cj.spacing({"DESK_CLOSE_RECOVERY_SPACING_MINUTES": "10"}) == timedelta(minutes=10)
    assert cj.spacing({"DESK_CLOSE_RECOVERY_SPACING_MINUTES": "0"}) == timedelta(minutes=5)
    assert cj.spacing({"DESK_CLOSE_RECOVERY_SPACING_MINUTES": "soon"}) == timedelta(minutes=5)


def test_volume_pending_from_a_recovered_preparation_waits_for_the_next_session(tmp_path, monkeypatch):
    """Price recovery at 16:36 does not make today's Alpaca daily final: VCP/cup stay
    pending in the job and are prepared next session under the existing rule."""
    from desk.playbook.filters import GateResult
    from tests.alpaca_support import AlpacaSim, Clock, dry_daily, with_volume
    from tests.test_triggers import CUP
    monkeypatch.setattr(sc, "trend_template", lambda f, spy: GateResult({"fixture template": True}))
    day0, entry = date(2026, 9, 29), date(2026, 9, 30)
    late = {**frames(previous_trading_day(day0)), ("LEAD", "D"): daily(CUP, end=previous_trading_day(day0))}
    fresh = {**frames(day0), ("LEAD", "D"): daily(CUP, end=day0)}
    sim = AlpacaSim(symbols=["LEAD", "SPY", "QQQ", "IWM"], daily=dry_daily(day0, days=12))
    clock, log = Clock(at(day0, 16, 12)), sc.ScanLog(tmp_path / "scan")
    watch = {"LEAD": ["leader scan"]}
    sc.run(with_volume(Fake(late), tmp_path / "volume.sqlite", sim, clock), watch, log, clock.at)
    clock.at = at(day0, 16, 36)
    recovered = sc.run(with_volume(Fake(fresh), tmp_path / "volume.sqlite", sim, clock), watch, log, clock.at)
    assert recovered.kind == "close_recovery" and not recovered.error, recovered.error
    assert log.signals.close_job(day0)["volume_pending"] == ["LEAD"]
    assert log.signals.close_job_volume_pending(entry) == {"LEAD"} and log.pending(entry, "volume") == set()
    assert not {a["setup_id"] for a in recovered.armed} & {"2_minervini_vcp", "3_oneil_cup_with_handle"}
    clock.at = at(entry, 9, 47)
    morning = sc.run(with_volume(Fake(fresh), tmp_path / "volume.sqlite", sim, clock), watch, log, clock.at)
    prep = morning.discovery["volume_preparation"]
    assert {a["setup_id"] for a in prep["armed"]} == {"2_minervini_vcp", "3_oneil_cup_with_handle"}


def test_the_status_command_never_creates_a_store(tmp_path, capsys):
    assert cj.main(["status", "--data-dir", str(tmp_path / "absent")]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "NO_SIGNAL_STORE"
    assert not (tmp_path / "absent").exists()
