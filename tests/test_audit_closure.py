"""Regressions for the 2026-10-02 audits of 6d21ddf (Astra, outside Claude, DeepSeek).

Astra A: every age limit is judged at the final clock, in approve and consume.
Astra B: the signal event is fenced through the final write (real SignalStore).
Astra C: the account guard holds in rollback-journal and WAL modes, after reopen.
Outside Claude #2 (H1), #7 (H8), #8 (H6), #15, #20 and #21.
Approvals here are automated fixtures (``fixture:automated-test``), never Taz's.
"""
from contextlib import closing
from datetime import timedelta
import sqlite3
import threading
import time

import pytest

from desk.risk_state import RiskStateError, RiskStateStore
from desk.risk_terms import EventStatus
from desk.tickets import TicketError, TicketStore
from tests.conftest import NOW
from tests.risk_support import MARKET, FixtureTerms
from tests.test_tickets import _event_path
from tests.ticket_support import FIXTURE_ACTOR, Terms, account, approve, inputs, observer, share_request

T0 = NOW + timedelta(seconds=40)      # when the check starts
LATE = NOW + timedelta(seconds=55)    # the final clock after a 15-second check


@pytest.fixture
def risk_state(tmp_path):
    state = RiskStateStore(tmp_path / "risk.sqlite")
    state.save_snapshot(account(), "obs1", now=NOW)
    return state


@pytest.fixture
def store(tmp_path):
    return TicketStore(tmp_path / "tickets.sqlite")


def aged(risk_state, case, *, clock=None):
    """Evidence fresh at T0; only ``case`` is old enough to expire by LATE."""
    risk_state.save_snapshot(account(as_of=NOW + timedelta(seconds=20) if case == "account" else T0),
                             f"snap-{case}", now=T0)
    old = NOW - timedelta(seconds=10)
    return inputs(risk_state, clock=clock,
                  terms=Terms(checked_at=old if case == "terms" else T0, quote_at=T0),
                  observe=observer(quote_as_of=old if case == "quote" else T0),
                  market=MARKET.model_copy(update={"as_of": old if case == "market" else T0}))


FRESHNESS = {"account": "account_state_fresh", "quote": "quote_fresh",
             "market": "market_context_fresh", "terms": "signal_terms_fresh"}


# ---- Astra A / outside Claude #6 --------------------------------------------------
@pytest.mark.parametrize("case", sorted(FRESHNESS))
def test_evidence_expiring_during_consumption_is_refused(store, risk_state, case):
    fresh = aged(risk_state, case)
    tid, v = store.prepare(share_request(), fresh, now=T0)
    approve(store, tid, v, fresh, now=T0)
    with pytest.raises(TicketError, match=FRESHNESS[case]):
        store.consume(tid, v, request_id="r", inputs=aged(risk_state, case, clock=lambda: LATE), now=T0)
    assert store.get(tid, v, now=T0)["state"] == "approved"
    assert store.history(tid)[-1]["event"] == "consumption_refused"
    assert store.history(tid)[-1]["at"] == LATE.isoformat()


@pytest.mark.parametrize("case", sorted(FRESHNESS))
def test_evidence_expiring_during_approval_is_refused(store, risk_state, case):
    fresh = aged(risk_state, case)
    tid, v = store.prepare(share_request(), fresh, now=T0)
    with pytest.raises(TicketError, match=FRESHNESS[case]):
        approve(store, tid, v, aged(risk_state, case, clock=lambda: LATE), now=T0)
    assert store.get(tid, v, now=T0)["state"] == "pending"


def test_shorter_current_event_deadline_expiring_during_consumption_is_refused(store, risk_state):
    adapters = inputs(risk_state)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)  # stored approval lasts 120 s
    shortened = inputs(risk_state, terms=Terms(valid_until=NOW + timedelta(seconds=1)),
                       clock=lambda: NOW + timedelta(seconds=2))
    with pytest.raises(TicketError, match="signal_terms_fresh"):
        store.consume(tid, v, request_id="r", inputs=shortened, now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "approved"


def test_boundary_ages_still_pass_and_record_the_final_time(store, risk_state):
    # Account exactly 30 s old at the final clock: the existing <= boundary is kept.
    risk_state.save_snapshot(account(as_of=NOW + timedelta(seconds=25)), "edge", now=T0)
    fresh = inputs(risk_state, terms=Terms(checked_at=T0, quote_at=T0), observe=observer(quote_as_of=T0),
                   market=MARKET.model_copy(update={"as_of": T0}))
    tid, v = store.prepare(share_request(), fresh, now=T0)
    slow = inputs(risk_state, terms=Terms(checked_at=T0, quote_at=T0), observe=observer(quote_as_of=T0),
                  market=MARKET.model_copy(update={"as_of": T0}), clock=lambda: LATE)
    record = approve(store, tid, v, slow, now=T0)
    assert record.decided_at == LATE
    outcome = store.consume(tid, v, request_id="r", inputs=slow, now=T0)
    assert outcome["consumed_at"] == LATE.isoformat() and outcome["order_submitted"] is False


# ---- Astra B / outside Claude #5 --------------------------------------------------
def _mid_check(adapters, action):
    """Run ``action`` once inside the recheck, after the terms were resolved."""
    base, fired = adapters.observe, []

    def observe(symbol, legs, now):
        if not fired:
            fired.append(now)
            action()
        return base(symbol, legs, now)
    adapters.observe = observe
    return adapters


@pytest.mark.parametrize("change", ["invalidate", "suspend", "withdraw"])
def test_real_signal_change_after_resolve_blocks_consumption(store, tmp_path, change):
    at, log, event, quote, adapters, request = _event_path(tmp_path)
    tid, v = store.prepare(request, adapters, now=at)
    approve(store, tid, v, adapters, now=at)
    act = {"invalidate": lambda: log.signals.invalidate(event["id"], at, "stop touched mid-check"),
           "suspend": lambda: log.signals.suspend_all("provider outage mid-check"),
           "withdraw": lambda: log.signals.close(event["id"], at, "withdrawn mid-check")}[change]
    with pytest.raises(TicketError, match="no longer eligible"):
        store.consume(tid, v, request_id="r", inputs=_mid_check(adapters, act), now=at)
    assert not log.signals.get(event["id"], at)["eligible"]
    assert store.get(tid, v, now=at)["state"] == "approved"


def test_real_signal_invalidated_during_approval_is_refused(store, tmp_path):
    at, log, event, quote, adapters, request = _event_path(tmp_path)
    tid, v = store.prepare(request, adapters, now=at)
    act = lambda: log.signals.invalidate(event["id"], at, "stop touched mid-check")
    with pytest.raises(TicketError, match="no longer eligible"):
        approve(store, tid, v, _mid_check(adapters, act), now=at)
    assert store.get(tid, v, now=at)["state"] == "pending"


def test_unchanged_real_signal_passes_the_fence(store, tmp_path):
    at, log, event, quote, adapters, request = _event_path(tmp_path)
    tid, v = store.prepare(request, adapters, now=at)
    approve(store, tid, v, adapters, now=at)
    assert store.consume(tid, v, request_id="r", inputs=adapters, now=at)["replay"] is False
    assert log.signals.get(event["id"], at)["eligible"]


def test_changed_event_generation_at_commit_is_refused(store, risk_state):
    adapters = inputs(risk_state)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    revised = inputs(risk_state, terms=Terms(status=lambda event_id, at: EventStatus(True, "fixture-v2")))
    with pytest.raises(TicketError, match="no longer eligible"):
        store.consume(tid, v, request_id="r", inputs=revised, now=NOW)


def test_signal_source_without_a_fence_cannot_consume(store, risk_state):
    adapters = inputs(risk_state)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    with pytest.raises(TicketError, match="cannot hold the event"):
        store.consume(tid, v, request_id="r", inputs=inputs(risk_state, terms=FixtureTerms()), now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "approved"


def test_held_event_blocks_signal_writers_until_released(tmp_path):
    at, log, event, *_ = _event_path(tmp_path)
    with log.signals.held_event(event["id"]) as view:
        assert view(at)["eligible"]
        with closing(sqlite3.connect(log.signals.path, timeout=0.1, isolation_level=None)) as writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute("BEGIN IMMEDIATE")
    log.signals.invalidate(event["id"], at, "after the fence")
    assert log.signals.get(event["id"], at)["state"] == "invalidated"


# ---- Astra C / outside Claude #11 and #15 -----------------------------------------
@pytest.mark.parametrize("mode", ["delete", "wal"])
def test_account_guard_blocks_writers_in_both_journal_modes_after_reopen(tmp_path, mode):
    path = tmp_path / "risk.sqlite"
    RiskStateStore(path).save_snapshot(account(), "obs1", now=NOW)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(f"PRAGMA journal_mode={mode}").fetchone()[0] == mode
    reopened = RiskStateStore(path)  # the persisted mode is kept, not rewritten
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == mode
    with reopened.held_account("fixture-account") as (revision, held):
        with closing(sqlite3.connect(path, timeout=0.1, isolation_level=None)) as writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute("BEGIN IMMEDIATE")
        assert not held.halted
    assert reopened.set_manual_halt("fixture-account", True, actor=FIXTURE_ACTOR, reason="after", now=NOW) \
        == revision + 1


@pytest.mark.parametrize("mode", ["delete", "wal"])
def test_manual_stop_mid_check_blocks_in_both_journal_modes(store, tmp_path, mode):
    path = tmp_path / "risk-mode.sqlite"
    RiskStateStore(path).save_snapshot(account(), "obs1", now=NOW)
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"PRAGMA journal_mode={mode}")
    state = RiskStateStore(path)
    adapters = inputs(state)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    halt = lambda: state.set_manual_halt("fixture-account", True, actor=FIXTURE_ACTOR,
                                         reason="fixture stop mid-check", now=NOW)
    with pytest.raises(TicketError, match="not_halted"):
        store.consume(tid, v, request_id="r", inputs=_mid_check(inputs(state), halt), now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "approved"


def test_manual_stop_issued_while_the_guard_is_held_waits_and_then_lands(risk_state):
    """Renamed evidence for #15: the stop really waits (production timeout) and lands after."""
    result = {}
    with risk_state.held_account("fixture-account") as (revision, _):
        worker = threading.Thread(target=lambda: result.setdefault("rev", risk_state.set_manual_halt(
            "fixture-account", True, actor=FIXTURE_ACTOR, reason="stop pressed during final write", now=NOW)))
        worker.start()
        time.sleep(0.3)
        assert worker.is_alive()                                   # waiting, not failed
        assert not risk_state.load("fixture-account")[1].halted     # nothing landed inside the guard
    worker.join(5)
    assert result["rev"] == revision + 1 and risk_state.load("fixture-account")[1].halted


# ---- Outside Claude #2 (H1): a manual stop never waits behind a waiting consumer ----
def test_manual_stop_engages_while_a_consumer_waits_for_the_ticket_lock(store, risk_state, tmp_path):
    adapters = inputs(risk_state)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    blocker = sqlite3.connect(tmp_path / "tickets.sqlite", isolation_level=None, check_same_thread=False)
    timing = {}

    def press_stop_then_release():
        time.sleep(0.3)  # the consumer is now waiting for the ticket lock
        started = time.monotonic()
        risk_state.set_manual_halt("fixture-account", True, actor=FIXTURE_ACTOR,
                                   reason="Taz presses manual stop", now=NOW)
        timing["halt_seconds"] = time.monotonic() - started
        blocker.execute("ROLLBACK")

    def take_ticket_lock():
        blocker.execute("BEGIN IMMEDIATE")  # another ticket writer, mid-check
        threading.Thread(target=press_stop_then_release).start()
    with pytest.raises(TicketError, match="not_halted"):
        store.consume(tid, v, request_id="r", inputs=_mid_check(inputs(risk_state), take_ticket_lock), now=NOW)
    blocker.close()
    assert timing["halt_seconds"] < 2
    assert risk_state.load("fixture-account")[1].halted
    assert store.get(tid, v, now=NOW)["state"] == "approved"


# ---- Outside Claude #7 (H8) and #20 ------------------------------------------------
def test_routine_unchanged_snapshot_mid_check_does_not_refuse(store, risk_state):
    adapters = inputs(risk_state)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    tick = lambda: risk_state.save_snapshot(account(as_of=NOW - timedelta(seconds=1)), "routine-tick", now=NOW)
    assert store.consume(tid, v, request_id="r", inputs=_mid_check(inputs(risk_state), tick), now=NOW)["replay"] is False


def test_manual_stop_is_never_refused_by_a_newer_snapshot(risk_state):
    stale_revision, _ = risk_state.load("fixture-account")
    risk_state.save_snapshot(account(as_of=NOW - timedelta(seconds=1)), "routine-tick", now=NOW)
    risk_state.set_manual_halt("fixture-account", True, expected_revision=stale_revision,
                               actor=FIXTURE_ACTOR, reason="stop", now=NOW)
    revision, state = risk_state.load("fixture-account")
    assert state.halted
    with pytest.raises(RiskStateError, match="current revision"):  # a resume still needs the current state
        risk_state.set_manual_halt("fixture-account", False, expected_revision=stale_revision,
                                   actor=FIXTURE_ACTOR, reason="resume", now=NOW)
    with pytest.raises(RiskStateError, match="current revision"):
        risk_state.set_manual_halt("fixture-account", False, actor=FIXTURE_ACTOR, reason="resume", now=NOW)


def test_busy_store_reports_that_the_manual_stop_was_not_recorded(risk_state, monkeypatch):
    def locked(self, timeout=5):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(RiskStateStore, "_connect", locked)
    with pytest.raises(RiskStateError, match="Manual stop was NOT recorded"):
        risk_state.set_manual_halt("fixture-account", True, actor=FIXTURE_ACTOR, reason="stop", now=NOW)


# ---- Outside Claude #8 (H6) and #21 ------------------------------------------------
def test_editing_two_rows_cannot_reopen_a_consumed_approval(store, risk_state, tmp_path):
    adapters = inputs(risk_state)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    first = store.consume(tid, v, request_id="r1", inputs=adapters, now=NOW)
    with closing(sqlite3.connect(tmp_path / "tickets.sqlite")) as db, db:
        db.execute("UPDATE tickets SET state='approved'")
        db.execute("UPDATE approvals SET consumed_request=NULL, consumed_at=NULL, consumption=NULL")
    with pytest.raises(TicketError, match="already consumed"):
        store.consume(tid, v, request_id="r2", inputs=adapters, now=NOW)
    assert store.consume(tid, v, request_id="r1", inputs=adapters, now=NOW) == {**first, "replay": True}
    with closing(sqlite3.connect(tmp_path / "tickets.sqlite")) as db:
        for statement in ("DELETE FROM consumptions", "UPDATE consumptions SET request_id='r2'"):
            with pytest.raises(sqlite3.DatabaseError, match="insert-only"):
                db.execute(statement)


def test_audit_trail_alone_still_marks_an_approval_used(store, risk_state, tmp_path):
    """Even with the consumption row gone (triggers dropped), the audit row refuses reuse."""
    adapters = inputs(risk_state)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    store.consume(tid, v, request_id="r1", inputs=adapters, now=NOW)
    with closing(sqlite3.connect(tmp_path / "tickets.sqlite")) as db, db:
        db.execute("DROP TRIGGER consumptions_insert_only_delete")
        db.execute("DELETE FROM consumptions")
        db.execute("UPDATE tickets SET state='approved'")
    with pytest.raises(TicketError, match="already used"):
        store.consume(tid, v, request_id="r2", inputs=adapters, now=NOW)


def test_v1_database_migrates_consumptions_into_the_insert_only_table(store, risk_state, tmp_path):
    adapters = inputs(risk_state)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    first = store.consume(tid, v, request_id="r1", inputs=adapters, now=NOW)
    with closing(sqlite3.connect(tmp_path / "tickets.sqlite")) as db, db:
        db.execute("DROP TABLE consumptions")
        db.execute("UPDATE meta SET value='desk-tickets-v1' WHERE key='schema'")
    migrated = TicketStore(tmp_path / "tickets.sqlite")
    assert migrated.get(tid, v, now=NOW)["consumption"]["permission_id"] == first["permission_id"]
    assert [e["event"] for e in migrated.history(tid)] == ["prepared", "approved", "consumed"]
    with closing(sqlite3.connect(tmp_path / "tickets.sqlite")) as db, db:
        db.execute("UPDATE tickets SET state='approved'")
    with pytest.raises(TicketError, match="already consumed"):
        migrated.consume(tid, v, request_id="r2", inputs=adapters, now=NOW)


def test_consumption_permission_names_the_bound_terms(store, risk_state):
    adapters = inputs(risk_state)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    outcome = store.consume(tid, v, request_id="r", inputs=adapters, now=NOW)
    assert outcome["binding_sha256"] == store.get(tid, v, now=NOW)["binding_sha256"]
    assert store.history(tid)[-1]["payload"]["binding_sha256"] == outcome["binding_sha256"]


def test_unknown_schema_is_refused_without_changing_the_file(tmp_path):
    path = tmp_path / "tickets.sqlite"
    TicketStore(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE consumptions")
        db.execute("UPDATE meta SET value='desk-tickets-v9' WHERE key='schema'")
    with pytest.raises(TicketError, match="Unsupported"):
        TicketStore(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='consumptions'").fetchone() is None
