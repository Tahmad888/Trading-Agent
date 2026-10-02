"""G4 acceptance: real ticket store, real risk/signal components, fixture adapters.

Provider transport and clocks are synthetic at their boundaries. Approvals here are
automated fixtures (``fixture:automated-test``), never Taz's approval.
"""
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import io
import json
import sqlite3
import threading

import pytest
from pydantic import ValidationError

from desk import scanner as sc
from desk import tickets as tk
from desk.contracts import ApprovalRecord, TradeProposal
from desk.risk_state import RiskStateStore
from desk.risk_terms import EventRiskSource
from desk.tickets import TicketError, TicketLeg, TicketStore
from tests.conftest import NOW
from tests.risk_support import BOOK, MARKET
from tests.risk_support import evaluate as fixture_evaluate
from tests.test_scanner import Fake, m15, sig
from tests.test_signal_lifecycle import BASE, now as session_now
from tests.ticket_support import (CALL, FIXTURE_ACTOR, Terms, account, approve, call_request, inputs,
                                  observer, share_request)

RICH = dict(buying_power=10_000_000, margin_excess=10_000_000, equity=10_000_000, equity_high_water_mark=10_000_000)


@pytest.fixture
def risk_state(tmp_path):
    store = RiskStateStore(tmp_path / "risk.sqlite")
    store.save_snapshot(account(), "obs1", now=NOW)
    store.save_snapshot(account(account_id="second-account"), "obs1b", now=NOW)
    return store


@pytest.fixture
def rich_state(tmp_path):
    store = RiskStateStore(tmp_path / "rich-risk.sqlite")
    store.save_snapshot(account(**RICH), "obs1", now=NOW)
    return store


@pytest.fixture
def store(tmp_path):
    return TicketStore(tmp_path / "tickets.sqlite")


@pytest.fixture
def adapters(risk_state):
    return inputs(risk_state)


def legs_view(store, tid, v):
    return store.get(tid, v, now=NOW)["binding"]["legs"]


# 1
def test_share_ticket_defaults_to_stop_budget_and_shows_plain_terms(store, adapters):
    request = share_request()
    assert request.sizing_mode == "stop_budget"
    tid, v = store.prepare(request, adapters, now=NOW)
    view = store.get(tid, v, now=NOW)
    assert view["state"] == "pending" and view["proposal"]["sizing_mode"] == "stop_budget"
    leg = view["binding"]["legs"][0]
    assert leg["requested_qty"] is None and leg["final_qty"] == 39  # floor((50 - 1) / 1.25)
    disc = view["binding"]["disclosures"]
    assert (disc["estimated_stop_loss_usd"], disc["estimated_total_risk_usd"], disc["position_value_usd"]) == (48.75, 49.75, 9750)
    text = store.display(tid, v, now=NOW)
    for expected in ["Stop budget:", "Budget entered: $50.00", "requested quantity none (sized from budget)",
                     "final calculated quantity 39", "limit $250.00", "Stop (structural, from the signal event): $248.75",
                     "Target: none set by the setup", "Estimated stop loss: $48.75", "Reserved costs: $1.00",
                     "Estimated stop loss including costs: $49.75", "Position value: $9,750.00",
                     "Funding estimate: $9,751.00 local estimate; NOT a broker-verified", "Warnings: none"]:
        assert expected in text, expected
    with pytest.raises(ValidationError, match="explicit selected-quantity"):
        share_request(legs=(TicketLeg(symbol="MSFT", limit_price=Decimal("250"), qty=20),))


# 2
def test_selected_quantity_needs_an_explicit_choice(store, adapters):
    with pytest.raises(ValidationError, match="quantity you chose"):
        share_request(sizing_mode="selected_quantity")
    with pytest.raises(ValidationError, match="explicit sizing mode"):
        call_request(sizing_mode=None)
    request = share_request(sizing_mode="selected_quantity",
                            legs=(TicketLeg(symbol="MSFT", limit_price=Decimal("250"), qty=20),))
    tid, v = store.prepare(request, adapters, now=NOW)
    view = store.get(tid, v, now=NOW)
    assert view["proposal"]["sizing_mode"] == "selected_quantity"
    assert view["binding"]["legs"][0]["requested_qty"] == view["binding"]["legs"][0]["final_qty"] == 20
    assert "Selected quantity: you chose the quantity" in store.display(tid, v, now=NOW)


# 3
def test_ten_dollar_budget_with_1000_selected_shares_needs_exact_acknowledgement(store, risk_state, tmp_path):
    request = share_request(budget_usd=Decimal("10"), sizing_mode="selected_quantity",
                            legs=(TicketLeg(symbol="MSFT", limit_price=Decimal("250"), qty=1000),))
    # Buying power is still enforced: the default $10,000 account cannot fund it.
    tid, v = store.prepare(request, inputs(risk_state), now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "blocked"
    assert "buying_power" in store.get(tid, v, now=NOW)["binding"]["failed_checks"]
    rich = RiskStateStore(tmp_path / "rich.sqlite")
    rich.save_snapshot(account(buying_power=300_000, margin_excess=300_000), "o", now=NOW)
    adapters = inputs(rich)
    tid, v = store.prepare(request, adapters, now=NOW)
    view = store.get(tid, v, now=NOW)
    assert view["state"] == "pending"
    assert [(w["code"], w["value"], w["threshold"]) for w in view["binding"]["warnings"]] == \
        [("stop_estimate_above_budget", 1251.0, 10.0)]
    assert view["binding"]["disclosures"]["estimated_total_risk_usd"] == 1251
    text = store.display(tid, v, now=NOW)
    assert "Value $1,251.00 against $10.00" in text and "acknowledgement code: stop_estimate_above_budget:" in text
    with pytest.raises(TicketError, match="Acknowledge exactly"):
        approve(store, tid, v, adapters, acks=[])
    with pytest.raises(TicketError, match="Acknowledge exactly"):
        approve(store, tid, v, adapters, acks=["stop_estimate_above_budget"])
    assert store.get(tid, v, now=NOW)["state"] == "pending"
    record = approve(store, tid, v, adapters)
    assert record.acknowledgements == tuple(view["acknowledgement_tokens"])
    assert store.get(tid, v, now=NOW)["state"] == "approved"


# 4
def test_share_position_value_without_noisy_exposure_warning(store, adapters):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    assert store.get(tid, v, now=NOW)["binding"]["warnings"] == []
    proposal = TradeProposal.model_validate(store.get(tid, v, now=NOW)["proposal"])
    acct = account()
    decision = fixture_evaluate(proposal, acct, now=NOW, live=False)
    assert decision.approved and decision.position_value_usd == 9750
    assert "exposure_above_budget" not in {w.code for w in decision.warnings}
    # maximum_loss_budget arithmetic is unchanged: 20 shares need $5,000 + $1.
    full = proposal.model_copy(update={"sizing_mode": "maximum_loss_budget", "risk_usd": 5001.0,
        "legs": [proposal.legs[0].model_copy(update={"qty": 20})], "worst_case_loss_usd": 5000.0})
    assert fixture_evaluate(full, acct, now=NOW, live=False).final_leg_quantities == [20]
    assert fixture_evaluate(full.model_copy(update={"risk_usd": 5000.99}), acct, now=NOW, live=False).final_leg_quantities == [19]


# 5
def test_option_exposure_and_unavailable_stop_estimate_stay_distinct(store, risk_state):
    unavailable = inputs(risk_state, terms=Terms(option_exit_prices={}, option_exit_source=None))
    tid, v = store.prepare(call_request(budget_usd=Decimal("10")), unavailable, now=NOW)
    view = store.get(tid, v, now=NOW)
    disc = view["binding"]["disclosures"]
    assert view["state"] == "pending"
    assert disc["computed_max_loss_usd"] == 450 and disc["estimated_stop_loss_usd"] is None
    assert disc["estimated_total_risk_usd"] is None and disc["position_value_usd"] is None
    assert view["binding"]["legs"][0]["contract_id"] == "fixture:AAPL261120C00230000"
    assert {w["code"] for w in view["binding"]["warnings"]} == {"exposure_above_budget"}
    text = store.display(tid, v, now=NOW)
    assert "Option strategy exposure: $450.00" in text and "Net premium: $450.00 debit" in text
    assert "Estimated stop loss: unavailable (unavailable: underlying stop does not determine an option exit price)" in text
    assert "Position value" not in text
    tid, v = store.prepare(call_request(), inputs(risk_state), now=NOW)
    disc = store.get(tid, v, now=NOW)["binding"]["disclosures"]
    assert (disc["estimated_stop_loss_usd"], disc["computed_max_loss_usd"]) == (60, 450)
    assert "Estimated stop loss: $60.00" in store.display(tid, v, now=NOW)


# 6
def test_exact_budget_confirmation_for_25000(store, rich_state):
    adapters = inputs(rich_state)
    tid, v = store.prepare(share_request(budget_usd=Decimal("25000")), adapters, now=NOW)
    text = store.display(tid, v, now=NOW)
    assert "Budget entered: $25,000.00" in text and "final calculated quantity 19999" in text
    for wrong in ["250", "2,500", "2500.00", "", "25000.01", "25,00", "twenty five thousand", None]:
        with pytest.raises(TicketError, match=r"Budget confirmation does not match the ticket budget of \$25,000.00"):
            store.approve(tid, v, budget_confirmation=wrong, acknowledgements=[], actor=FIXTURE_ACTOR,
                          channel="automated_fixture", inputs=adapters, now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "pending"
    assert approve(store, tid, v, adapters, budget="$25,000").budget_confirmed_usd == "25000.00"
    # A changed budget is a new version: the earlier confirmation cannot carry over.
    tid2, v2 = store.revise(tid, adapters, now=NOW, budget_usd=Decimal("2500"))
    assert (tid2, v2) == (tid, 2) and store.get(tid, 1, now=NOW)["state"] == "superseded"
    with pytest.raises(TicketError, match="Budget confirmation"):
        approve(store, tid, 2, adapters, budget="25,000")
    assert approve(store, tid, 2, adapters, budget="2,500").budget_confirmed_usd == "2500.00"


# 7
EDITS = [
    {"budget_usd": Decimal("60")},
    {"sizing_mode": "selected_quantity", "legs": (TicketLeg(symbol="MSFT", limit_price=Decimal("250"), qty=20),)},
    {"account_id": "second-account"},
    {"environment": "live", "tier": 1},
    {"legs": (TicketLeg(symbol="MSFT", limit_price=Decimal("250.5")),)},
    {"est_costs_usd": Decimal("2")},
    {"exit_rules": ("sell half at the first target",)},
    {"time_stop": NOW + timedelta(days=10)},
    {"event_id": "aapl", "legs": (TicketLeg(symbol="AAPL", limit_price=Decimal("230")),)},
]


@pytest.mark.parametrize("change", EDITS, ids=lambda c: "+".join(c))
def test_every_bound_request_edit_needs_a_new_version_and_approval(store, adapters, change):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    first = store.get(tid, v, now=NOW)["binding_sha256"]
    _, v2 = store.revise(tid, adapters, now=NOW, **change)
    assert store.get(tid, v, now=NOW)["state"] == "superseded"
    with pytest.raises(TicketError, match="superseded"):
        store.consume(tid, v, request_id="r1", inputs=adapters, now=NOW)
    second = store.get(tid, v2, now=NOW)
    assert second["binding_sha256"] != first and second["state"] in {"pending", "blocked"}
    with pytest.raises(TicketError, match="no usable approval"):
        store.consume(tid, v2, request_id="r2", inputs=adapters, now=NOW)


@pytest.mark.parametrize("change", [{"legs": (TicketLeg(symbol=CALL, limit_price=Decimal("1.50"), qty=2),)},
    {"legs": (TicketLeg(symbol=CALL, limit_price=Decimal("1.40"), qty=3),)},
    {"legs": (TicketLeg(symbol="AAPL 261120P00230000", limit_price=Decimal("1.50"), qty=3),), "structure": "long_put"}],
    ids=["contracts", "premium", "leg"])
def test_option_leg_edits_need_a_new_version(store, adapters, change):
    tid, v = store.prepare(call_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    store.revise(tid, adapters, now=NOW, **change)
    with pytest.raises(TicketError, match="superseded"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=NOW)


EVIDENCE = {
    "stop": dict(terms=Terms(stop=248.5)),
    "target": dict(terms=Terms(target=270)),
    "event_revision": dict(terms=Terms(event_digest="revision2")),
    "setup_version_or_symbol": dict(terms=Terms(symbol="MSFX")),
}


@pytest.mark.parametrize("name", EVIDENCE)
def test_changed_independent_terms_after_approval_block_consumption(store, risk_state, adapters, name):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    with pytest.raises(TicketError, match="differ from the approved ticket|Recheck failed"):
        store.consume(tid, v, request_id="r", inputs=inputs(risk_state, **EVIDENCE[name]), now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "approved"  # still unused, and still bound to old terms


def test_changed_option_contract_identity_blocks_consumption(store, risk_state, adapters):
    tid, v = store.prepare(call_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    changed = BOOK.model_copy(update={"contracts": tuple(
        c.model_copy(update={"broker_contract_id": "fixture:replaced"}) if c.symbol == "AAPL261120C00230000" else c
        for c in BOOK.contracts)})
    with pytest.raises(TicketError, match="differ from the approved ticket"):
        store.consume(tid, v, request_id="r", inputs=inputs(risk_state, book=changed), now=NOW)


@pytest.mark.parametrize("column,edit", [
    ("request", lambda r: {**r, "budget_usd": "500"}),
    ("request", lambda r: {**r, "est_costs_usd": "0"}),
    ("binding", lambda b: {**b, "budget_usd": "500.00"}),
])
def test_tampered_stored_ticket_cannot_be_consumed(store, adapters, tmp_path, column, edit):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    with closing(sqlite3.connect(tmp_path / "tickets.sqlite")) as db, db:
        value = edit(json.loads(db.execute(f"SELECT {column} FROM tickets").fetchone()[0]))
        db.execute(f"UPDATE tickets SET {column}=?", (tk._json(value),))
        if column == "binding":
            db.execute("UPDATE tickets SET binding_sha256=?", (tk._sha(value),))
    with pytest.raises(TicketError):
        store.consume(tid, v, request_id="r", inputs=adapters, now=NOW)


# 8
def test_warning_acknowledgements_are_exact_per_version_and_value(store, risk_state, tmp_path):
    warn = RiskStateStore(tmp_path / "warn.sqlite")
    warn.save_snapshot(account(pnl_today=-200), "o1", now=NOW)
    adapters = inputs(warn)
    a, va = store.prepare(share_request(), adapters, now=NOW)
    b, vb = store.prepare(share_request(), adapters, now=NOW)
    token_a = store.get(a, va, now=NOW)["acknowledgement_tokens"]
    token_b = store.get(b, vb, now=NOW)["acknowledgement_tokens"]
    assert [t.split(":")[0] for t in token_a] == ["daily_loss"] and token_a != token_b
    assert "Today's account loss is at or above your daily warning level. Value $200.00 against $200.00." \
        in store.display(a, va, now=NOW)
    for acks in ([], token_b, token_a + token_a, token_a + ["approved=true"]):
        with pytest.raises(TicketError, match="Acknowledge exactly"):
            approve(store, a, va, adapters, acks=acks)
    approve(store, a, va, adapters, acks=token_a)
    # The warning value changes after preparation: the old acknowledgement cannot approve.
    c, vc = store.prepare(share_request(), adapters, now=NOW)
    warn.save_snapshot(account(pnl_today=-250, as_of=NOW - timedelta(seconds=1)), "o2", now=NOW)
    with pytest.raises(TicketError, match="changed since this version"):
        approve(store, c, vc, adapters)
    # ...and a value change after approval prevents consumption of the approved ticket.
    with pytest.raises(TicketError, match="differ from the approved ticket"):
        store.consume(a, va, request_id="r", inputs=adapters, now=NOW)


def test_acknowledgement_cannot_override_a_blocking_check(store, tmp_path):
    warn = RiskStateStore(tmp_path / "warn.sqlite")
    warn.save_snapshot(account(pnl_today=-200), "o1", now=NOW)
    adapters = inputs(warn, observe=observer(security_tradable=None))
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    view = store.get(tid, v, now=NOW)
    assert view["state"] == "blocked" and "security_tradable" in view["binding"]["failed_checks"]
    assert "no warning acknowledgement can override" in store.display(tid, v, now=NOW)
    with pytest.raises(TicketError, match="failed blocking checks"):
        approve(store, tid, v, adapters)


# 9
def test_forged_unknown_and_legacy_approvals_fail(store, adapters, tmp_path):
    with pytest.raises(TicketError, match="Unknown ticket"):
        store.consume("T-made-up", 1, request_id="r", inputs=adapters, now=NOW)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    with pytest.raises(TicketError, match="no usable approval"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=NOW)
    other, ov = store.prepare(share_request(budget_usd=Decimal("40")), adapters, now=NOW)
    genuine = approve(store, other, ov, adapters)
    db_path = tmp_path / "tickets.sqlite"
    binding = store.get(tid, v, now=NOW)["binding"]
    forged_snapshot = {**binding, "budget_usd": "5000.00"}
    forged = ApprovalRecord(schema_version=2, approval_id="A-forged", ticket_id=tid, ticket_version=v,
        decision="approve", actor="Taz", channel="terminal", decided_at=NOW, expires_at=NOW + timedelta(minutes=2),
        budget_confirmed_usd="5000.00", acknowledgements=(), snapshot=forged_snapshot,
        snapshot_sha256=tk._sha(forged_snapshot)).model_dump(mode="json")
    copied = {**genuine.model_dump(mode="json")}
    legacy = {"proposal_id": f"{tid}:v{v}", "decision": "approve", "decided_by": "Taz", "decided_at": NOW.isoformat(),
              "order_args_sha256": "0" * 64, "token_expires_at": (NOW + timedelta(minutes=2)).isoformat(), "reason": "old"}
    with pytest.raises(ValidationError):
        ApprovalRecord.model_validate(legacy)
    for approval_id, record in [("A-forged", forged), (genuine.approval_id + "-copy", {**copied, "approval_id": genuine.approval_id + "-copy"}),
                                ("A-legacy", legacy)]:
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("INSERT INTO approvals(approval_id,ticket_id,version,record,record_sha256,expires_at) VALUES (?,?,?,?,?,?)",
                       (approval_id, tid, v, tk._json(record), tk._sha(record), (NOW + timedelta(minutes=2)).isoformat()))
            db.execute("UPDATE tickets SET state='approved', approval_id=? WHERE ticket_id=? AND version=?", (approval_id, tid, v))
        with pytest.raises(TicketError, match="unknown, legacy or does not match"):
            store.consume(tid, v, request_id="r", inputs=adapters, now=NOW)


def test_copied_risk_result_and_actor_claims_are_not_approval(store, adapters, tmp_path):
    blocked = inputs(adapters.risk_state, observe=observer(security_tradable=False))
    tid, v = store.prepare(share_request(), blocked, now=NOW)
    good, gv = store.prepare(share_request(), adapters, now=NOW)
    with closing(sqlite3.connect(tmp_path / "tickets.sqlite")) as db, db:
        decision, binding, digest = db.execute("SELECT decision,binding,binding_sha256 FROM tickets WHERE ticket_id=?", (good,)).fetchone()
        db.execute("UPDATE tickets SET state='pending', decision=?, binding=?, binding_sha256=? WHERE ticket_id=?",
                   (decision, binding, digest, tid))
    with pytest.raises(TicketError):
        approve(store, tid, v, blocked)  # stored copy says eligible; independent rerun says no
    with pytest.raises(TicketError, match="labelled"):
        store.approve(good, gv, budget_confirmation="50", acknowledgements=[], actor="Taz",
                      channel="automated_fixture", inputs=adapters, now=NOW)
    with pytest.raises(TicketError, match="labelled"):
        store.approve(good, gv, budget_confirmation="50", acknowledgements=[], actor="fixture:x",
                      channel="terminal", inputs=adapters, now=NOW)
    with pytest.raises(TypeError):
        store.approve(good, gv, budget_confirmation="50", acknowledgements=[], actor=FIXTURE_ACTOR,
                      channel="automated_fixture", inputs=adapters, now=NOW, approved=True)


# 10
def test_rejection_revocation_and_expiry(store, adapters, tmp_path):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    store.reject(tid, v, actor=FIXTURE_ACTOR, channel="automated_fixture", reason="not this one", now=NOW)
    with pytest.raises(TicketError, match="rejected"):
        approve(store, tid, v, adapters)
    with pytest.raises(TicketError, match="rejected"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=NOW)
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    store.revoke(tid, v, actor=FIXTURE_ACTOR, channel="automated_fixture", reason="changed my mind", now=NOW)
    with pytest.raises(TicketError, match="revoked"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=NOW)
    with pytest.raises(TicketError):
        store.revoke(tid, v, actor=FIXTURE_ACTOR, channel="automated_fixture", reason="again", now=NOW)
    tid, v = expiring = store.prepare(share_request(), adapters, now=NOW)
    record = approve(store, tid, v, adapters)
    assert record.expires_at == NOW + timedelta(seconds=120)
    with pytest.raises(TicketError, match="expired"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=NOW + timedelta(seconds=120))
    assert store.get(tid, v, now=NOW + timedelta(seconds=121))["state"] == "expired"
    # A long configured lifetime still cannot outlive the signal event's validity.
    long = TicketStore(tmp_path / "long.sqlite", approval_lifetime=timedelta(hours=1))
    tid, v = long.prepare(share_request(), adapters, now=NOW)
    assert approve(long, tid, v, adapters).expires_at == NOW + timedelta(minutes=15)
    with pytest.raises(TicketError):
        TicketStore(tmp_path / "bad.sqlite", approval_lifetime=timedelta(hours=2))
    events = [e["event"] for e in store.history(expiring[0])]
    assert events == ["prepared", "approved", "expired", "consumption_refused"]


# 11
def test_restart_preserves_state_and_history(store, adapters, tmp_path):
    path = tmp_path / "tickets.sqlite"
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    record = approve(store, tid, v, adapters)
    reopened = TicketStore(path)
    assert reopened.get(tid, v, now=NOW)["state"] == "approved"
    assert reopened.get(tid, v, now=NOW)["approval"]["approval_id"] == record.approval_id
    result = reopened.consume(tid, v, request_id="first", inputs=adapters, now=NOW + timedelta(seconds=5))
    again = TicketStore(path)
    assert again.get(tid, v, now=NOW)["state"] == "consumed"
    assert [e["event"] for e in again.history(tid)] == ["prepared", "approved", "consumed"]
    assert again.consume(tid, v, request_id="first", inputs=adapters, now=NOW)["permission_id"] == result["permission_id"]
    with pytest.raises(TicketError, match="already consumed"):
        again.consume(tid, v, request_id="second", inputs=adapters, now=NOW)
    with closing(sqlite3.connect(path)) as db, pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM audit")


# 12
def test_concurrent_and_repeated_consumption_yield_one_permission(store, adapters):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    barrier, results, errors = threading.Barrier(8), [], []

    def consume(request_id):
        barrier.wait()
        try:
            results.append(store.consume(tid, v, request_id=request_id, inputs=adapters, now=NOW))
        except TicketError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=consume, args=(f"r{i}",)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 1 and len(errors) == 7 and not results[0]["replay"]
    assert results[0]["order_submitted"] is False and results[0]["broker_action"] == "none"

    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    barrier, results, errors = threading.Barrier(8), [], []
    threads = [threading.Thread(target=consume, args=("same",)) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(results) == 8
    assert sum(not r["replay"] for r in results) == 1
    assert len({r["permission_id"] for r in results}) == 1
    assert [e["event"] for e in store.history(tid)].count("consumed") == 1


# 13
def test_stale_or_revised_evidence_blocks_but_fresh_unchanged_evidence_passes(store, risk_state, adapters):
    later = NOW + timedelta(seconds=10)
    for stale in (Terms(checked_at=NOW - timedelta(seconds=120)), Terms(event_digest="revision2")):
        tid, v = store.prepare(share_request(), adapters, now=NOW)
        approve(store, tid, v, adapters)
        with pytest.raises(TicketError):
            store.consume(tid, v, request_id="r", inputs=inputs(risk_state, terms=stale), now=later)
    tid, v = store.prepare(call_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    old_book = BOOK.model_copy(update={"as_of": NOW - timedelta(hours=25)})
    with pytest.raises(TicketError, match="Recheck failed"):
        store.consume(tid, v, request_id="r", inputs=inputs(risk_state, book=old_book), now=later)
    # Same terms, new receipt times: revalidates without a new executable version.
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    fresh = inputs(risk_state, terms=Terms(checked_at=later, quote_at=later),
                   observe=observer(quote_as_of=later - timedelta(seconds=1)),
                   market=MARKET.model_copy(update={"as_of": later}))
    assert store.consume(tid, v, request_id="r", inputs=fresh, now=later)["final_leg_quantities"] == [39]


def _event_path(tmp_path):
    at = session_now()
    candidate = replace(sig(), stop=None, stop_basis="session_low", adr_pct=4)
    log = sc.ScanLog(tmp_path / "scan")
    frame = m15(BASE)
    event = sc.observe_signal(log.signals, candidate, frame, at)[0]
    quote = {"price": 101.5}
    terms = EventRiskSource(Fake({("LEAD", "M15"): frame}), log, lambda s: (s, quote["price"], at))
    state = RiskStateStore(tmp_path / "event-risk.sqlite")
    state.save_snapshot(account(as_of=at - timedelta(seconds=5), pnl_day=at.date(),
                                pnl_week_start=at.date() - timedelta(days=at.weekday())), "o1", now=at)
    adapters = inputs(state, terms=terms, market=MARKET.model_copy(update={"as_of": at}),
                      observe=observer(quote_as_of=at - timedelta(seconds=1)))
    request = share_request(event_id=event["id"], legs=(TicketLeg(symbol="LEAD", limit_price=Decimal("101.5")),),
                            budget_usd=Decimal("36"), time_stop=at + timedelta(days=15))
    return at, log, event, quote, adapters, request


# 14
def test_touched_stop_or_invalidated_event_blocks_consumption(store, tmp_path):
    at, log, event, quote, adapters, request = _event_path(tmp_path)
    tid, v = store.prepare(request, adapters, now=at)
    approve(store, tid, v, adapters, now=at)
    quote["price"] = 97  # the fresh trusted quote is through the frozen stop at 98
    with pytest.raises(TicketError, match="Recheck failed"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=at)
    assert log.signals.get(event["id"], at)["state"] == "invalidated"
    quote["price"] = 101.5
    with pytest.raises(TicketError, match="Recheck failed"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=at)


def test_explicitly_invalidated_event_blocks_consumption(store, tmp_path):
    at, log, event, quote, adapters, request = _event_path(tmp_path)
    tid, v = store.prepare(request, adapters, now=at)
    approve(store, tid, v, adapters, now=at)
    log.signals.invalidate(event["id"], at, "fixture invalidation")
    with pytest.raises(TicketError, match="Recheck failed"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=at)


# 15
def test_manual_stop_after_approval_blocks_consumption(store, risk_state, adapters):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    revision, _ = risk_state.load("fixture-account")
    risk_state.set_manual_halt("fixture-account", True, expected_revision=revision, actor="fixture:automated-test",
                               reason="fixture manual stop", now=NOW)
    with pytest.raises(TicketError, match="not_halted"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=NOW)
    assert store.history(tid)[-1]["event"] == "consumption_refused"


# 16
def test_new_blocking_risk_failure_after_approval(store, risk_state, adapters):
    tid, v = store.prepare(share_request(), adapters, now=NOW)
    approve(store, tid, v, adapters)
    with pytest.raises(TicketError, match="security_tradable"):
        store.consume(tid, v, request_id="r", inputs=inputs(risk_state, observe=observer(security_tradable=False)), now=NOW)
    risk_state.save_snapshot(account(buying_power=100, margin_excess=100, as_of=NOW - timedelta(seconds=1)), "o2", now=NOW)
    with pytest.raises(TicketError, match="buying_power"):
        store.consume(tid, v, request_id="r", inputs=adapters, now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "approved"


# 17
def test_complete_positive_path_from_persisted_signal_to_single_use(store, tmp_path):
    at, log, event, quote, adapters, request = _event_path(tmp_path)
    tid, v = store.prepare(request, adapters, now=at)
    view = store.get(tid, v, now=at)
    assert view["state"] == "pending" and view["terms"]["stop"] == 98 and view["terms"]["event_id"] == event["id"]
    assert view["binding"]["legs"][0]["final_qty"] == 10  # floor((36 - 1) / (101.5 - 98))
    text = store.display(tid, v, now=at)
    assert "Qullamaggie breakout on LEAD (long)" in text and "Estimated stop loss including costs: $36.00" in text
    record = approve(store, tid, v, adapters, now=at)
    assert record.channel == "automated_fixture" and record.actor == FIXTURE_ACTOR and not record.order_authorized
    result = store.consume(tid, v, request_id="paper-test-1", inputs=adapters, now=at)
    assert result["final_leg_quantities"] == [10] and result["order_submitted"] is False
    assert "No order was submitted" in result["note"]
    assert store.get(tid, v, now=at)["state"] == "consumed"
    assert [e["event"] for e in store.history(tid)] == ["prepared", "approved", "consumed"]
    assert len(log.signals.events(at)) == 1


def test_terminal_interface_round_trip(tmp_path, monkeypatch):
    """The CLI path Taz would use, driven here with fixture adapters and fixture input."""
    monkeypatch.setenv("DESK_DEMO_RISK_STATE", str(tmp_path / "demo-risk.sqlite"))
    request = tmp_path / "request.json"
    request.write_text(share_request(budget_usd=Decimal("10"), sizing_mode="selected_quantity",
        legs=(TicketLeg(symbol="MSFT", limit_price=Decimal("250"), qty=1000),)).model_dump_json())
    base = ["--db", str(tmp_path / "t.sqlite"), "--adapters", "tests.ticket_support:demo_inputs"]
    out = io.StringIO()
    assert tk.main(base + ["prepare", str(request)], stdout=out) == 0
    text = out.getvalue()
    tid = text.split()[1]
    token = next(l.split(": ")[1] for l in text.splitlines() if "acknowledgement code:" in l)
    out = io.StringIO()
    assert tk.main(base + ["approve", tid, "--version", "1", "--actor", "fixture:cli"],
                   stdin=io.StringIO("10\n" + token + "\n"), stdout=out) == 2
    assert "labelled" in out.getvalue()  # the terminal channel refuses fixture actors
    out = io.StringIO()
    assert tk.main(base + ["approve", tid, "--version", "1", "--actor", "cli-fixture-operator"],
                   stdin=io.StringIO("1000\n" + token + "\n"), stdout=out) == 2
    assert "Budget confirmation does not match" in out.getvalue()
    out = io.StringIO()
    assert tk.main(base + ["approve", tid, "--version", "1", "--actor", "cli-fixture-operator"],
                   stdin=io.StringIO("10\n" + token + "\n"), stdout=out) == 0
    assert "APPROVED" in out.getvalue() and "Not an order" in out.getvalue()
    out = io.StringIO()
    assert tk.main(base + ["consume", tid, "--version", "1", "--request-id", "cli-1"], stdout=out) == 0
    assert json.loads(out.getvalue())["order_submitted"] is False
    out = io.StringIO()
    assert tk.main(base + ["history", tid], stdout=out) == 0
    assert [json.loads(l)["event"] for l in out.getvalue().splitlines()] == \
        ["prepared", "approval_refused", "approved", "consumed"]
    monkeypatch.delenv("DESK_TICKET_ADAPTERS", raising=False)
    out = io.StringIO()
    assert tk.main(["--db", str(tmp_path / "t.sqlite"), "prepare", str(request)], stdout=out) == 2
    assert "No trusted adapters configured" in out.getvalue()
