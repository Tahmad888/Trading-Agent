"""Regression tests for the five G4 defects Astra reproduced on d1e4514.

Each test failed on 5b63b8a (G4 code unchanged since d1e4514) before the fix.
Approvals here are automated fixtures (``fixture:automated-test``), never Taz's.
"""
from datetime import timedelta
from decimal import Decimal
import sqlite3

import pytest

from desk import tickets as tk
from desk.risk_state import RiskStateStore
from desk.tickets import TicketError, TicketLeg, TicketStore
from tests.conftest import NOW
from tests.ticket_support import FIXTURE_ACTOR, Terms, account, approve, inputs, observer, share_request


@pytest.fixture
def risk_state(tmp_path):
    state = RiskStateStore(tmp_path / "risk.sqlite")
    state.save_snapshot(account(), "obs1", now=NOW)
    return state


@pytest.fixture
def store(tmp_path):
    return TicketStore(tmp_path / "tickets.sqlite")


@pytest.fixture
def adapters(risk_state):
    return inputs(risk_state)


def halting_observer(risk_state):
    """Switch the manual stop on once, after recheck has already read the account."""
    base, fired = observer(), []

    def observe(symbol, legs, now):
        if not fired:
            fired.append(now)
            revision, _ = risk_state.load("fixture-account")
            risk_state.set_manual_halt("fixture-account", True, expected_revision=revision,
                                       actor=FIXTURE_ACTOR, reason="fixture manual stop mid-check", now=now)
        return base(symbol, legs, now)
    return observe


class Revising(Terms):
    """First read returns the approved revision; every later read a newer one."""
    def __init__(self, **later):
        super().__init__()
        self.later, self.calls = later, 0

    def resolve(self, event_id, at):
        self.calls += 1
        terms = super().resolve(event_id, at)
        return terms if self.calls == 1 else terms.model_copy(update=self.later)


# Finding 1: manual stop switched on after the account read must still block.
def test_manual_stop_during_consumption_check_blocks(store, risk_state, adapters):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    with pytest.raises(TicketError, match="Account changed"):
        store.consume(tid, v, request_id="r", inputs=inputs(risk_state, observe=halting_observer(risk_state)),
                      now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "approved"
    assert store.history(tid)[-1]["event"] == "consumption_refused"
    # The halt now shows in the account read itself and keeps blocking.
    with pytest.raises(TicketError, match="not_halted"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=NOW)


def test_manual_stop_during_approval_check_blocks(store, risk_state, adapters):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    with pytest.raises(TicketError, match="Account changed"):
        approve(store, tid, v, inputs(risk_state, observe=halting_observer(risk_state)))
    assert store.get(tid, v, now=NOW)["state"] == "pending"


# Finding 2: one terms snapshot feeds both the risk check and the approval binding.
def test_risk_and_binding_use_one_terms_snapshot(risk_state):
    source = Revising(event_digest="fixture-v2", valid_until=NOW + timedelta(minutes=5))
    check = tk.recheck("T-snapshot", 1, share_request(), inputs(risk_state, terms=source), NOW)
    assert source.calls == 1
    assert check.binding["signal"]["event_digest"] == "fixture-v1"
    assert check.decision.signal_event_valid_until == check.terms.valid_until == NOW + timedelta(minutes=15)


def test_newer_terms_revision_blocks_consumption(store, risk_state, adapters):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    # Approved under fixture-v1; the single read at consumption now sees fixture-v2.
    with pytest.raises(TicketError, match="differ from the approved ticket"):
        store.consume(tid, v, request_id="r", inputs=inputs(risk_state, terms=Terms(event_digest="fixture-v2")),
                      now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "approved"


# Finding 3: expiry is judged by a fresh clock reading at the final transaction.
def test_slow_check_crossing_expiry_blocks_consumption(store, risk_state, adapters):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    record = approve(store, tid, v, adapters)
    assert record.expires_at == NOW + timedelta(seconds=120)
    slow = inputs(risk_state, clock=lambda: NOW + timedelta(seconds=130))
    with pytest.raises(TicketError, match="expired"):
        store.consume(tid, v, request_id="r", inputs=slow, now=NOW + timedelta(seconds=10))
    assert store.get(tid, v, now=NOW + timedelta(seconds=10))["state"] == "approved"


def test_final_clock_never_runs_backwards(store, risk_state, adapters):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    behind = inputs(risk_state, clock=lambda: NOW - timedelta(hours=1))
    with pytest.raises(TicketError, match="expired"):
        store.consume(tid, v, request_id="r", inputs=behind, now=NOW + timedelta(seconds=120))


# Finding 4: a ticket whose time stop has passed cannot be entered.
def test_passed_time_stop_blocks_consumption(store, adapters):
    request = share_request(time_stop=NOW + timedelta(seconds=15))
    tid, v = store.prepare(request, adapters, now=NOW)
    record = approve(store, tid, v, adapters)
    # Four seconds after the required exit, inside the 120-second approval lifetime.
    # The approval itself now ends at the time stop, so either refusal is correct.
    with pytest.raises(TicketError, match="time stop|expired"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=NOW + timedelta(seconds=19))
    assert store.get(tid, v, now=NOW + timedelta(seconds=19))["state"] != "consumed"
    assert record.expires_at <= request.time_stop


def test_time_stop_crossed_during_check_blocks_consumption(store, risk_state, adapters):
    tid, v = store.prepare(share_request(time_stop=NOW + timedelta(seconds=60)), adapters, now=NOW)
    approve(store, tid, v, adapters)
    slow = inputs(risk_state, clock=lambda: NOW + timedelta(seconds=64))
    with pytest.raises(TicketError, match="time stop"):
        store.consume(tid, v, request_id="r", inputs=slow, now=NOW + timedelta(seconds=10))


def test_ticket_prepared_after_its_time_stop_is_blocked(store, adapters):
    tid, v = store.prepare(share_request(time_stop=NOW - timedelta(seconds=1)), adapters, now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "blocked"
    with pytest.raises(TicketError, match="cannot be approved"):
        approve(store, tid, v, adapters)


# Finding 5: the visible ticket shows the exact accepted prices.
def test_display_shows_exact_limit_and_stop(store, risk_state):
    adapters = inputs(risk_state, terms=Terms(stop=248.7512))
    leg = TicketLeg(symbol="MSFT", limit_price=Decimal("250.0049"))
    tid, v = store.prepare(share_request(legs=(leg,)), adapters, now=NOW)
    text = store.display(tid, v, now=NOW)
    assert "limit $250.0049;" in text
    assert "$250.00;" not in text
    assert "Stop (structural, from the signal event): $248.7512" in text
    assert store.get(tid, v, now=NOW)["binding"]["legs"][0]["limit_price"] == "250.0049"


@pytest.mark.parametrize("value,shown", [
    (Decimal("250"), "$250.00"), (Decimal("250.5"), "$250.50"), (Decimal("1250.0049"), "$1,250.0049"),
    (248.75, "$248.75"), (0.0001, "$0.0001"), ("250.0100", "$250.01"),
])
def test_price_formatting_is_exact(value, shown):
    assert tk.price(value) == shown


def test_held_revision_makes_a_manual_stop_wait_for_the_final_write(risk_state):
    with risk_state.held_revision("fixture-account") as revision:
        writer = sqlite3.connect(risk_state.path, timeout=0.1, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE accounts SET revision=revision+1 WHERE account_id='fixture-account'")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            writer.execute("COMMIT")  # cannot land while the final check holds the revision
        writer.execute("ROLLBACK")
        writer.close()
    assert risk_state.load("fixture-account")[0] == revision
