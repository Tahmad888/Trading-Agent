"""Regressions for Astra's re-audit of ee4dc90 (2026-10-02).

P1a: a manual stop is never vetoed by the as-of time of a routine snapshot that
committed while the stop waited for the lock (two real stores, one database).
P1b: after the final clock sample no reader can delay the ticket commit, in
rollback-journal and WAL modes alike.
Approvals here are automated fixtures (``fixture:automated-test``), never Taz's.
"""
from contextlib import closing
from datetime import datetime, timedelta
import sqlite3
import threading
import time

import pytest

from desk.risk_state import RiskStateError, RiskStateStore
from desk.tickets import TicketError, TicketStore
from tests.conftest import NOW
from tests.risk_support import MARKET
from tests.ticket_support import FIXTURE_ACTOR, Terms, account, approve, inputs, observer, share_request

REQUESTED = NOW + timedelta(seconds=30)


class PausedWriter(RiskStateStore):
    """A routine snapshot writer that pauses right after taking its write lock."""
    def __init__(self, path):
        super().__init__(path)
        self.locked, self.release = threading.Event(), threading.Event()

    def _read(self, db, account_id):
        self.locked.set()
        assert self.release.wait(10)
        return RiskStateStore._read(db, account_id)


def _race(tmp_path, snapshot_offset):
    """Writer holds the lock; the stop request (time REQUESTED) starts and waits;
    the writer then commits a snapshot ``snapshot_offset`` from REQUESTED."""
    path = tmp_path / "risk.sqlite"
    RiskStateStore(path).save_snapshot(account(), "obs1", now=NOW)
    writer, controller = PausedWriter(path), RiskStateStore(path)
    snapshot = account(as_of=REQUESTED + snapshot_offset)
    results = {}

    def write():
        results["snapshot"] = writer.save_snapshot(snapshot, "routine", now=REQUESTED + timedelta(seconds=5))

    def stop():
        try:
            results["stop"] = controller.set_manual_halt(
                "fixture-account", True, actor=FIXTURE_ACTOR, reason="stop", now=REQUESTED,
                clock=lambda: REQUESTED + timedelta(seconds=2))
        except RiskStateError as exc:
            results["stop"] = exc
    writing = threading.Thread(target=write)
    writing.start()
    assert writer.locked.wait(5)
    stopping = threading.Thread(target=stop)
    stopping.start()
    time.sleep(0.3)
    assert stopping.is_alive(), "the stop request must be waiting on the writer's lock"
    writer.release.set()
    writing.join(10)
    stopping.join(35)
    return controller, results


@pytest.mark.parametrize("offset", [timedelta(seconds=1), timedelta(seconds=-1)],
                         ids=["snapshot-newer-than-request", "snapshot-older-control"])
def test_manual_stop_waiting_on_a_routine_snapshot_still_engages(tmp_path, offset):
    controller, results = _race(tmp_path, offset)
    assert results["snapshot"] == 2
    assert results["stop"] == 3, results["stop"]
    revision, state = controller.load("fixture-account")
    assert state.halted and revision == 3
    entry = controller.audit("fixture-account")[-1]
    assert entry["event"] == "manual_halt" and entry["at"] == REQUESTED.isoformat()
    assert entry["payload"] == {"revision": 3, "requested_at": REQUESTED.isoformat(),
                                "committed_at": (REQUESTED + timedelta(seconds=2)).isoformat(),
                                "snapshot_as_of": (REQUESTED + offset).isoformat()}


def test_resume_keeps_the_revision_and_chronology_checks(tmp_path):
    controller, _ = _race(tmp_path, timedelta(seconds=1))
    with pytest.raises(RiskStateError, match="predates the account snapshot"):
        controller.set_manual_halt("fixture-account", False, expected_revision=3, actor=FIXTURE_ACTOR,
                                   reason="resume", now=REQUESTED)
    with pytest.raises(RiskStateError, match="current revision"):
        controller.set_manual_halt("fixture-account", False, expected_revision=2, actor=FIXTURE_ACTOR,
                                   reason="resume", now=REQUESTED + timedelta(seconds=2))
    assert controller.load("fixture-account")[1].halted
    assert controller.set_manual_halt("fixture-account", False, expected_revision=3, actor=FIXTURE_ACTOR,
                                      reason="resume", now=REQUESTED + timedelta(seconds=2)) == 4


def test_manual_control_timestamps_must_be_timezone_aware(tmp_path):
    controller, _ = _race(tmp_path, timedelta(seconds=-1))
    with pytest.raises(RiskStateError, match="timezone"):
        controller.set_manual_halt("fixture-account", True, actor=FIXTURE_ACTOR, reason="stop",
                                   now=REQUESTED.replace(tzinfo=None))
    with pytest.raises(RiskStateError, match="timezone aware"):
        controller.set_manual_halt("fixture-account", True, actor=FIXTURE_ACTOR, reason="stop",
                                   now=REQUESTED, clock=lambda: REQUESTED.replace(tzinfo=None))


# ---- P1b: no reader-dependent wait after the final clock sample ---------------------
HOLD = 1.0      # seconds a reader keeps its read transaction open during the recheck
MARGIN = 0.5    # evidence starts this far inside its limit (account 30 s, others 60 s)
PROMPT = 0.5    # the commit must follow the recorded final time within this


class Wall:
    """Test clock that advances with real time from NOW."""
    def __init__(self):
        self.t0 = time.monotonic()

    def __call__(self):
        return NOW + timedelta(seconds=time.monotonic() - self.t0)


class Reader:
    """Opens a real read transaction on the ticket database when armed and the
    recheck calls the market observer; releases it after HOLD seconds."""
    def __init__(self, path, clock):
        self.path, self.clock, self.armed, self.released, self.thread = path, clock, False, None, None

    def wrap(self, observe):
        def hooked(symbol, legs, now):
            if self.armed:
                self.armed = False
                self.start()
            return observe(symbol, legs, now)
        return hooked

    def start(self):
        held = threading.Event()

        def run():
            with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
                db.execute("BEGIN")
                db.execute("SELECT count(*) FROM tickets").fetchone()
                held.set()
                time.sleep(HOLD)
                self.released = self.clock()
                db.execute("COMMIT")
        self.thread = threading.Thread(target=run)
        self.thread.start()
        assert held.wait(5)


CASES = {  # case -> refusal, or (approve refusal, consume refusal)
    "account": "account_state_fresh",
    "quote": "quote_fresh",
    "market": "market_context_fresh",
    "terms": "signal_terms_fresh",
    "event_deadline": ("Evidence validity ended during the approval check", "Approval has expired"),
    "approval_deadline": ("Evidence validity ended during the approval check", "Approval has expired"),
}


def _setup(tmp_path, case, journal):
    clock = Wall()
    risk_state = RiskStateStore(tmp_path / "risk.sqlite")
    near = lambda limit: NOW - timedelta(seconds=limit - MARGIN)  # noqa: E731
    risk_state.save_snapshot(account(as_of=near(30) if case == "account" else NOW), "obs1", now=NOW)
    lifetime = timedelta(seconds=1 if case == "approval_deadline" else 120)
    store = TicketStore(tmp_path / "tickets.sqlite", approval_lifetime=lifetime)
    if journal == "wal":
        with closing(sqlite3.connect(store.path)) as db:
            assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    reader = Reader(store.path, clock)
    terms = {"terms": Terms(checked_at=near(60), quote_at=NOW),
             "event_deadline": Terms(checked_at=NOW, quote_at=NOW,
                                     valid_until=NOW + timedelta(seconds=MARGIN))}.get(case, Terms())
    adapters = inputs(risk_state, clock=clock, terms=terms,
                      observe=reader.wrap(observer(quote_as_of=near(60) if case == "quote" else NOW)),
                      market=MARKET.model_copy(update={"as_of": near(60) if case == "market" else NOW}))
    tid, v = store.prepare(share_request(), adapters, now=clock())
    return clock, store, reader, adapters, tid, v


def _refusal(case, step):
    expected = CASES[case]
    return expected if isinstance(expected, str) else expected[step == "consume"]


def _run(store, tid, v, adapters, clock, step):
    if step == "approve":
        record = approve(store, tid, v, adapters, now=clock())
        return record.decided_at
    return datetime.fromisoformat(store.consume(tid, v, request_id="r", inputs=adapters, now=clock())["consumed_at"])


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("step", ["approve", "consume"])
def test_reader_wait_happens_before_the_final_clock_in_rollback_mode(tmp_path, case, step):
    """Rollback journal: the reader is cleared before the clock is read, so the
    evidence that expires during the wait is refused, and the refusal's recorded
    time is after the reader let go."""
    clock, store, reader, adapters, tid, v = _setup(tmp_path, case, "delete")
    if step == "consume":
        approve(store, tid, v, adapters, now=clock())
    reader.armed = True
    with pytest.raises(TicketError, match=_refusal(case, step)):
        _run(store, tid, v, adapters, clock, step)
    reader.thread.join(5)
    refused = store.history(tid)[-1]
    assert refused["event"] == f"{'approval' if step == 'approve' else 'consumption'}_refused"
    assert refused["at"] >= reader.released.isoformat()
    assert store.get(tid, v, now=clock())["state"] != ("approved" if step == "approve" else "consumed")


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("step", ["approve", "consume"])
def test_wal_readers_never_delay_the_final_commit(tmp_path, case, step):
    """WAL: the same reader does not block, so the same evidence is still fresh."""
    clock, store, reader, adapters, tid, v = _setup(tmp_path, case, "wal")
    if step == "consume":
        approve(store, tid, v, adapters, now=clock())
    reader.armed = True
    at = _run(store, tid, v, adapters, clock, step)
    assert clock() - at < timedelta(seconds=PROMPT)
    assert reader.released is None or at < reader.released
    reader.thread.join(5)


@pytest.mark.parametrize("journal", ["delete", "wal"])
@pytest.mark.parametrize("step", ["approve", "consume"])
def test_commit_follows_the_recorded_final_time_promptly(tmp_path, journal, step):
    """Evidence with room to spare: the write succeeds in both modes and nothing
    waits between the recorded final time and the commit."""
    clock, store, reader, adapters, tid, v = _setup(tmp_path, "fresh", journal)
    if step == "consume":
        approve(store, tid, v, adapters, now=clock())
    reader.armed = True
    at = _run(store, tid, v, adapters, clock, step)
    assert clock() - at < timedelta(seconds=PROMPT)
    if journal == "delete":
        assert at >= reader.released
    reader.thread.join(5)
