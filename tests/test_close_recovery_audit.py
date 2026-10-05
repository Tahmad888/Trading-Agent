"""F1/F2 public-path regressions: temporary stores, synthetic bars, no network."""
from contextlib import contextmanager
from threading import Event, Thread

import pytest

from desk import close_jobs as cj
from desk import scanner as sc
from desk.bars import BarDataError
from desk.calendar import clock, next_trading_day
from desk.signal_state import SignalStateError, SignalStore, candidate_id
from tests.test_close_jobs import ALL, BREAKOUT, LISTS, MON, TUE, WATCH, at, charts, symbols_requested
from tests.test_scanner import Fake, m15


def waiting_job(path):
    log, source = sc.ScanLog(path), Fake(charts(MON, late=ALL), lists=LISTS)
    sc.run(source, WATCH, log, at(MON, 16, 14))
    source.frames = {**charts(MON), ("LEAD", "M15"): m15(BREAKOUT, TUE)}
    return log, source


def floors(log):
    with log.signals._db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM armed_floors")]


def prepare(log, source, started, evaluated, published):
    times = iter((evaluated, published))
    return cj.attempt(source, log, MON, started, removed=set(), kind="close_recovery",
                      decision_clock=lambda: next(times))


@pytest.mark.parametrize("started", [at(TUE, 9, 29), at(TUE, 9, 59)])
def test_delayed_publication_blocks_earlier_crossing_and_allows_a_later_one(tmp_path, started):
    log, source = waiting_job(tmp_path)
    done = at(TUE, 10, 1)
    rec = prepare(log, source, started, done, done)
    assert not rec.error
    assert floors(log) and all(clock(f["start_after"]) == done for f in floors(log))
    hit = sc.run(source, WATCH, log, done)
    assert not [h for h in hit.triggered if h["symbol"] == "LEAD"]
    source.frames[("LEAD", "M15")] = m15(
        [*BREAKOUT, (152.5, 153, 149, 150), (150, 154, 149.5, 153)], TUE,
    )
    later = sc.run(source, WATCH, log, at(TUE, 10, 31))
    lead = [h for h in later.triggered if h["symbol"] == "LEAD"]
    assert lead and all(clock(h["trigger_at"]) == at(TUE, 10, 30) for h in lead)


def test_before_open_preparation_still_allows_the_normal_crossing(tmp_path):
    log, source = waiting_job(tmp_path)
    done = at(MON, 16, 36)
    assert not prepare(log, source, done, done, done).error
    assert floors(log) == []
    hit = sc.run(source, WATCH, log, at(TUE, 10, 1))
    assert any(h["symbol"] == "LEAD" for h in hit.triggered)


def test_attempt_times_and_retry_spacing_use_the_final_publication_clock(tmp_path):
    log, source = waiting_job(tmp_path)
    source.frames = charts(MON, late=("MOVR",))
    started, evaluated, published = at(TUE, 9, 59), at(TUE, 10, 1), at(TUE, 10, 2)
    rec = prepare(log, source, started, evaluated, published)
    job = log.signals.close_job(MON)
    record = job["history"][-1]
    assert clock(rec.at) == published and clock(record["at"]) == published
    assert clock(record["started_at"]) == started and clock(record["evaluated_at"]) == evaluated
    assert clock(job["next_attempt_at"]) == at(TUE, 10, 7)
    assert all(clock(f["start_after"]) == published for f in floors(log))


@pytest.mark.parametrize("peers_already_published", [False, True])
def test_target_expiring_at_final_publication_discards_new_results_and_keeps_peers(
    tmp_path, peers_already_published,
):
    log, source = sc.ScanLog(tmp_path), Fake(
        charts(MON, late=("MOVR",) if peers_already_published else ALL), lists=LISTS,
    )
    sc.run(source, WATCH, log, at(MON, 16, 14))
    prior = log.signals.load_armed(TUE)
    source.frames = charts(MON)
    rec = prepare(log, source, at(TUE, 15, 59), at(TUE, 15, 59), at(TUE, 16, 0))
    job = log.signals.close_job(MON)
    assert rec.error == "CLOSE_JOB_EXPIRED: TARGET_SESSION_ENDED" and rec.armed == []
    assert job["status"] == "EXPIRED" and clock(job["finished_at"]) == at(TUE, 16, 0)
    assert job["history"][-1]["prepared"] == []
    assert log.signals.load_armed(TUE) == prior and floors(log) == []


def test_backwards_final_clock_refuses_publication(tmp_path):
    log, source = waiting_job(tmp_path)
    with pytest.raises(SignalStateError, match="publication clock precedes"):
        prepare(log, source, at(TUE, 9, 59), at(TUE, 10, 1), at(TUE, 10, 0))
    assert log.signals.load_armed(TUE) is None and floors(log) == []
    assert log.signals.close_job(MON)["claim_token"] is None


@pytest.mark.parametrize("lock_kind,journal", [("writer", "delete"), ("reader", "delete"), ("writer", "wal")])
def test_sqlite_wait_happens_before_the_final_clock(tmp_path, monkeypatch, lock_kind, journal):
    log, source = waiting_job(tmp_path)
    blocker = SignalStore(log.signals.path)
    with blocker._db() as db:
        assert db.execute(f"PRAGMA journal_mode={journal}").fetchone()[0] == journal
    committing, allow_commit, requesting_lock, final_clock = Event(), Event(), Event(), Event()
    original_commit, original_db = log.signals.commit_close_job, log.signals._db
    times, results, errors = [], [], []

    def decision_clock():
        stamp = at(TUE, 9, 59) if not times else at(TUE, 10, 1)
        if times:
            final_clock.set()
        times.append(stamp)
        return stamp

    @contextmanager
    def traced_db():
        with original_db() as db:
            db.set_trace_callback(lambda sql: requesting_lock.set()
                                  if committing.is_set() and sql.startswith("BEGIN ") else None)
            yield db

    def commit(*args, **kwargs):
        committing.set()
        assert allow_commit.wait(5)
        return original_commit(*args, **kwargs)

    def worker():
        try:
            results.append(cj.attempt(source, log, MON, at(TUE, 9, 59), removed=set(),
                                      kind="close_recovery", decision_clock=decision_clock))
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(log.signals, "_db", traced_db)
    monkeypatch.setattr(log.signals, "commit_close_job", commit)
    thread = Thread(target=worker)
    thread.start()
    try:
        assert committing.wait(5)
        with blocker._db() as db:
            if lock_kind == "writer":
                db.execute("BEGIN IMMEDIATE")
            else:
                db.execute("BEGIN")
                db.execute("SELECT * FROM close_jobs").fetchall()
            allow_commit.set()
            assert requesting_lock.wait(5)
            assert not final_clock.wait(0.1)
            assert times == [at(TUE, 9, 59)]  # provider evaluation only; writer still holds the lock
    finally:
        allow_commit.set()
        thread.join(5)
    assert not thread.is_alive() and not errors, errors
    assert times == [at(TUE, 9, 59), at(TUE, 10, 1)]
    assert clock(results[0].at) == at(TUE, 10, 1)
    assert all(clock(f["start_after"]) == at(TUE, 10, 1) for f in floors(log))


def test_already_published_candidates_keep_their_original_floor(tmp_path):
    log, source = sc.ScanLog(tmp_path), Fake(charts(MON, late=("MOVR",)), lists=LISTS)
    sc.run(source, WATCH, log, at(MON, 16, 14))
    before = log.load_armed(TUE)[1]
    assert any(s.symbol == "LEAD" for s in before)
    source.frames = charts(MON)
    prepare(log, source, at(TUE, 9, 59), at(TUE, 10, 1), at(TUE, 10, 1))
    lead_ids = {candidate_id(s, TUE) for s in before if s.symbol == "LEAD"}
    assert not lead_ids & {f["candidate_id"] for f in floors(log)}


def test_repeated_payload_does_not_backdate_or_replace_a_committed_candidate_floor(tmp_path):
    log, source = sc.ScanLog(tmp_path), Fake(charts(MON, late=("MOVR",)), lists=LISTS)
    sc.run(source, WATCH, log, at(MON, 16, 14))
    existing = log.signals.load_armed(TUE)[1]
    source.frames = charts(MON)
    evaluated, _ = sc.close_scan(source, ["MOVR"], at(TUE, 10, 1), preparing=True)
    assert evaluated.armed
    assert log.signals.claim_close_job(MON, at(TUE, 10, 1), "audit", at(TUE, 10, 11))[0]
    assert log.signals.commit_close_job(
        MON, "audit", at(TUE, 10, 1), outcomes={}, market="full",
        payload=existing + evaluated.armed, volume_pending=[], status="FINISHED", coverage="FULL",
        next_attempt_at=None, blocked_reason=None, floor_after=at(TUE, 10, 1), record={},
    )
    old_ids = {candidate_id(sc.restore_signal(p), TUE) for p in existing}
    new_ids = {candidate_id(sc.restore_signal(p), TUE) for p in evaluated.armed} - old_ids
    assert {f["candidate_id"] for f in floors(log)} == new_ids


class StoppingSource(Fake):
    def __init__(self, code, symbol="MOVR", **kwargs):
        super().__init__(charts(MON), fail={symbol}, lists=LISTS, **kwargs)
        self.code = code

    def bars(self, symbols, **kwargs):
        if self.fail & set(symbols):
            self.calls.append((tuple(symbols), kwargs["category"], kwargs["timespan"]))
            raise BarDataError(f"Webull HTTP {self.code} provider stop")
        return super().bars(symbols, **kwargs)


@pytest.mark.parametrize("code", [401, 403, 429])
def test_explicit_resume_requeues_the_stopped_name_across_restart(tmp_path, code):
    log, source = sc.ScanLog(tmp_path), StoppingSource(code)
    sc.run(source, WATCH, log, at(MON, 16, 14))
    blocked = log.signals.close_job(MON)
    assert blocked["status"] == "BLOCKED"
    before, calls = log.signals.load_armed(TUE), len(source.calls)
    resumed = log.signals.resume_close_job(MON, at(MON, 16, 37), "audit:operator")
    assert len(source.calls) == calls
    assert resumed["outcomes"]["MOVR"]["status"] == "RETRY_AFTER_OPERATOR_RESUME"
    assert resumed["history"][-1]["previous_provider_stops"]["MOVR"] == blocked["outcomes"]["MOVR"]
    assert {s: o for s, o in resumed["outcomes"].items() if s != "MOVR"} \
        == {s: o for s, o in blocked["outcomes"].items() if s != "MOVR"}
    source.fail.clear()
    log = sc.ScanLog(tmp_path)
    rec = sc.run(source, WATCH, log, at(MON, 16, 40))
    job = log.signals.close_job(MON)
    assert not rec.error and (job["status"], job["coverage"]) == ("FINISHED", "FULL")
    assert symbols_requested(source, calls) == {"MOVR", "SPY", "QQQ"}
    assert job["outcomes"]["MOVR"] == {"status": "PREPARED", "attempt": 2}
    assert all(p in log.signals.load_armed(TUE)[1] for p in before[1])


@pytest.mark.parametrize("code", [401, 403, 429])
def test_continuing_provider_stop_blocks_again_and_never_retries_automatically(tmp_path, code):
    log, source = sc.ScanLog(tmp_path), StoppingSource(code)
    sc.run(source, WATCH, log, at(MON, 16, 14))
    calls = len(source.calls)
    assert sc.run(source, WATCH, log, at(MON, 16, 36)) is None
    assert len(source.calls) == calls
    log.signals.resume_close_job(MON, at(MON, 16, 37), "audit:operator")
    sc.run(source, WATCH, log, at(MON, 16, 40))
    job = log.signals.close_job(MON)
    assert "MOVR" in symbols_requested(source, calls)
    assert job["status"] == "BLOCKED" and job["outcomes"]["MOVR"]["attempt"] == 2
    calls = len(source.calls)
    assert sc.run(source, WATCH, log, at(MON, 16, 50)) is None
    assert len(source.calls) == calls


@pytest.mark.parametrize("symbol", ["SPY", "QQQ"])
def test_market_reference_stop_recovers_after_explicit_resume(tmp_path, symbol):
    log, source = sc.ScanLog(tmp_path), StoppingSource(403, symbol)
    sc.run(source, WATCH, log, at(MON, 16, 14))
    assert log.signals.close_job(MON)["status"] == "BLOCKED"
    assert log.signals.load_armed(TUE) is None
    source.fail.clear()
    log.signals.resume_close_job(MON, at(MON, 16, 37), "audit:operator")
    rec = sc.run(source, WATCH, log, at(MON, 16, 40))
    assert not rec.error and log.signals.close_job(MON)["coverage"] == "FULL"
    assert log.signals.load_armed(TUE)[0] == "full"


def test_a_removed_resumed_ticker_is_not_fetched_or_armed(tmp_path):
    log, source = sc.ScanLog(tmp_path), StoppingSource(429)
    sc.run(source, WATCH, log, at(MON, 16, 14))
    log.signals.resume_close_job(MON, at(MON, 16, 37), "audit:operator")
    source.fail.clear()
    calls = len(source.calls)
    rec = cj.attempt(source, log, MON, at(MON, 16, 40), removed={"MOVR"}, kind="close_recovery")
    assert not rec.error and "MOVR" not in symbols_requested(source, calls)
    assert log.signals.close_job(MON)["outcomes"]["MOVR"]["status"] == "REMOVED"
    assert not [p for p in log.signals.load_armed(TUE)[1] if p["symbol"] == "MOVR"]


def test_resume_preserves_terminal_outcomes_and_another_source_job(tmp_path):
    log, source = sc.ScanLog(tmp_path), StoppingSource(429)
    sc.run(source, WATCH, log, at(MON, 16, 14))
    # Persist legitimate terminal refusals through a claimed production store commit.
    log.signals.resume_close_job(MON, at(MON, 16, 37), "audit:operator")
    assert log.signals.claim_close_job(MON, at(MON, 16, 38), "audit", at(MON, 16, 48))[0]
    terminal = {"LEAD": {"status": "IDENTITY_REFUSED", "reason": "identity changed"},
                "DOWN": {"status": "INVALID_HISTORY", "reason": "invalid OHLC"},
                "IWM": {"status": "REMOVED"},
                "MOVR": {"status": "PROVIDER_STOP", "reason": "Webull HTTP 429"}}
    assert log.signals.commit_close_job(
        MON, "audit", at(MON, 16, 39), outcomes=terminal, market=None, payload=[],
        volume_pending=[], status="BLOCKED", coverage="PARTIAL", next_attempt_at=None,
        blocked_reason="MOVR", floor_after=None, record={},
    )
    other = log.signals.create_close_job(TUE, next_trading_day(TUE), {}, {}, at(MON, 16, 39))
    resumed = log.signals.resume_close_job(MON, at(MON, 16, 40), "audit:operator")
    assert all(resumed["outcomes"][s] == terminal[s] for s in ("LEAD", "DOWN", "IWM"))
    assert log.signals.close_job(TUE) == other
    count = len(resumed["history"])
    again = log.signals.resume_close_job(MON, at(MON, 16, 41), "audit:operator")
    assert len(again["history"]) == count
