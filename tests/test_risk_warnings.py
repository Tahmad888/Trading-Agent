from dataclasses import replace
from datetime import timedelta
import sqlite3

import pytest
from pydantic import ValidationError

from desk.playbook.filters import MarketSize
from desk.risk_context import Exposure
from desk.risk_state import RiskStateError, RiskStateStore
from tests.conftest import NOW
from tests.risk_support import BOOK, MARKET, REGISTRY, evaluate
from tests.test_instruments import CASES, build


@pytest.mark.parametrize("regime", list(MarketSize))
@pytest.mark.parametrize("grade", ["A", "B", "C", None])
def test_market_and_loss_warnings_never_change_user_budget(proposal, account, regime, grade):
    account = replace(account, pnl_today=-200, pnl_this_week=-400, equity=9000)
    result = evaluate(proposal.model_copy(update={"grade": grade}), account,
                      market=MARKET.model_copy(update={"regime": regime}), now=NOW)
    assert result.approved
    assert result.final_leg_quantities == [1, 1]
    assert result.risk_budget_usd == 22
    assert result.estimated_total_risk_usd == 22
    codes = {w.code for w in result.warnings}
    assert {"daily_loss", "weekly_loss", "account_drawdown"} <= codes
    assert ("mixed_market" in codes) == (regime is MarketSize.HALF)
    assert ("bearish_market" in codes) == (regime is MarketSize.NO_NEW_LONGS)
    assert result.warning_acknowledgement_required


@pytest.mark.parametrize("case", CASES)
def test_bearish_instruments_are_not_mistaken_for_longs(proposal, account, case):
    trade, book = build(proposal, case)
    result = evaluate(trade, account, contract_book=book,
                      market=MARKET.model_copy(update={"regime": MarketSize.NO_NEW_LONGS}), now=NOW)
    assert result.approved
    bearish = case is CASES[1] or case is CASES[3] or case is CASES[4]
    assert ("bearish_market" in {w.code for w in result.warnings}) != bearish


def test_warning_thresholds_and_recovery(proposal, account):
    for daily, weekly, equity, expected in [
        (-199.99, -399.99, 9000.01, set()),
        (-200, -400, 9000, {"daily_loss", "weekly_loss", "account_drawdown"}),
        (-1, -1, 10000, set()),
    ]:
        result = evaluate(proposal, replace(account, pnl_today=daily, pnl_this_week=weekly, equity=equity), now=NOW)
        assert result.approved
        assert {w.code for w in result.warnings} == expected


def test_proposal_cannot_promote_its_own_setup(proposal, account):
    result = evaluate(proposal, account, registry=None, now=NOW)
    assert not result.approved  # default registry is paper, regardless of tier=1
    assert evaluate(proposal, account, registry=None, live=False, now=NOW).approved
    for change in ({"setup_id": "made_up"}, {"setup_version": "old"}):
        assert not evaluate(proposal.model_copy(update=change), account, now=NOW).approved


@pytest.mark.parametrize("bad", [None, MARKET.model_copy(update={"as_of": NOW + timedelta(seconds=1)}),
    MARKET.model_copy(update={"as_of": NOW - timedelta(seconds=61)}),
    MARKET.model_copy(update={"source": ""}), MARKET.model_copy(update={"regime": "unknown"})])
def test_unknown_or_stale_market_is_data_failure_not_overridable_warning(proposal, account, bad):
    assert not evaluate(proposal, account, market=bad, now=NOW).approved


@pytest.mark.parametrize("change", [
    {"pnl_today": float("nan")}, {"buying_power": float("inf")}, {"equity": 0},
    {"equity_high_water_mark": 1}, {"as_of": NOW.replace(tzinfo=None)},
    {"pnl_day": NOW.date() - timedelta(days=1)}, {"pnl_basis": "realized_only"},
    {"pnl_week_start": NOW.date()}, {"source": ""},
    {"exposures": None},
])
def test_bad_account_state_cannot_be_overridden(proposal, account, change):
    assert not evaluate(proposal, replace(account, **change), now=NOW).approved


def exposure(ref, state):
    return Exposure(reference=ref, instrument="SPY", state=state, quantity=1,
                    unit="strategy", direction="long", market_value_usd=100,
                    estimated_stop_risk_usd=None, sector="index")


def test_restart_preserves_losses_highwater_and_unlimited_exposure(tmp_path, account, proposal):
    path = tmp_path / "risk.sqlite"
    store = RiskStateStore(path)
    snapshot = replace(account, equity=9000, pnl_today=-200, pnl_this_week=-400,
                       exposures=tuple(exposure(f"position-{i}", "open" if i % 2 else "pending") for i in range(40)))
    assert store.save_snapshot(snapshot, "obs1", now=NOW) == 1
    revision, loaded = RiskStateStore(path).load(account.account_id)
    assert revision == 1 and loaded == snapshot
    result = evaluate(proposal, loaded, now=NOW)
    assert result.approved and len(result.exposure_summary) == 40
    assert {w.code for w in result.warnings} == {"daily_loss", "weekly_loss", "account_drawdown"}
    # Rejected/cancelled pending orders disappear only with a reconciled snapshot.
    next_snapshot = replace(snapshot, as_of=NOW, exposures=snapshot.exposures[1:], equity_high_water_mark=9500)
    assert store.save_snapshot(next_snapshot, "obs2", now=NOW) == 2
    _, loaded = store.load(account.account_id)
    assert loaded.equity_high_water_mark == 10000 and len(loaded.exposures) == 39
    assert not loaded.halted  # no persisted automatic loss halt


def test_snapshot_replay_and_ordering_are_safe(tmp_path, account):
    store = RiskStateStore(tmp_path / "risk.sqlite")
    assert store.save_snapshot(account, "one", now=NOW) == 1
    assert store.save_snapshot(account, "one", now=NOW) == 1
    for snapshot, key in [(replace(account, pnl_today=-1), "one"), (account, "two")]:
        with pytest.raises(RiskStateError):
            store.save_snapshot(snapshot, key, now=NOW)
    assert store.load(account.account_id) == (1, account)


def test_explicit_manual_stop_survives_refresh_and_requires_audited_reset(tmp_path, account, proposal):
    store = RiskStateStore(tmp_path / "risk.sqlite")
    store.save_snapshot(account, "one", now=NOW)
    store.set_manual_halt(account.account_id, True, expected_revision=1, actor="Taz", reason="manual pause", now=NOW)
    store.save_snapshot(replace(account, as_of=NOW), "two", now=NOW)
    revision, loaded = store.load(account.account_id)
    assert loaded.halted and not evaluate(proposal, loaded, now=NOW).approved
    with pytest.raises(RiskStateError):
        store.set_manual_halt(account.account_id, False, expected_revision=revision, actor="", reason="", now=NOW)
    with pytest.raises(RiskStateError):
        store.set_manual_halt(account.account_id, False, expected_revision=1, actor="Taz", reason="resume", now=NOW)
    store.set_manual_halt(account.account_id, False, expected_revision=revision, actor="Taz", reason="resume", now=NOW)
    assert not store.load(account.account_id)[1].halted
    assert [e["event"] for e in store.audit(account.account_id)] == ["manual_halt", "manual_resume"]


def test_cash_flow_requires_explicit_reconciliation(tmp_path, account):
    store = RiskStateStore(tmp_path / "risk.sqlite")
    store.save_snapshot(account, "one", now=NOW)
    after = replace(account, as_of=NOW, equity=9000, equity_high_water_mark=9000)
    with pytest.raises(RiskStateError):
        store.save_snapshot(after, "two", now=NOW, cash_flow_usd=-1000)
    store.save_snapshot(after, "two", now=NOW, cash_flow_usd=-1000, actor="broker adapter", reason="confirmed withdrawal")
    assert store.load(account.account_id)[1].equity_high_water_mark == 9000
    assert store.audit(account.account_id)[0]["event"] == "cash_flow_reconciliation"


def test_acknowledgements_are_exact_audited_and_not_reused(tmp_path, account, proposal):
    path = tmp_path / "risk.sqlite"
    store = RiskStateStore(path)
    account = replace(account, pnl_today=-200)
    store.save_snapshot(account, "one", now=NOW)
    kwargs = dict(now=NOW, contract_book=BOOK, market=MARKET, registry=REGISTRY)
    decision, review = store.review(account.account_id, proposal, **kwargs)
    assert decision.approved and review
    with pytest.raises(RiskStateError):
        store.acknowledge(review, set(), actor="Taz", reason="reviewed", now=NOW)
    RiskStateStore(path).acknowledge(review, {"daily_loss"}, actor="Taz", reason="accept this ticket's risk", now=NOW)
    with pytest.raises(RiskStateError):
        store.acknowledge(review, {"daily_loss"}, actor="Taz", reason="again", now=NOW)
    _, changed = store.review(account.account_id, proposal.model_copy(update={"risk_usd": 100}), **kwargs)
    assert changed != review
    store.save_snapshot(replace(account, as_of=NOW), "two", now=NOW)
    with pytest.raises(RiskStateError):
        store.acknowledge(changed, {"daily_loss"}, actor="Taz", reason="stale", now=NOW)
    audit = store.audit(account.account_id)
    assert len(audit) == 1 and audit[0]["payload"]["review_id"] == review


def test_time_alone_expires_warning_review(tmp_path, account, proposal):
    store = RiskStateStore(tmp_path / "risk.sqlite")
    store.save_snapshot(replace(account, pnl_today=-200), "one", now=NOW)
    _, review = store.review(account.account_id, proposal, now=NOW, contract_book=BOOK, market=MARKET, registry=REGISTRY)
    with pytest.raises(RiskStateError):
        store.acknowledge(review, {"daily_loss"}, actor="Taz", reason="too late", now=NOW + timedelta(seconds=26))


def test_corrupt_or_missing_state_does_not_become_empty_healthy_account(tmp_path, account):
    path = tmp_path / "risk.sqlite"
    store = RiskStateStore(path)
    with pytest.raises(RiskStateError):
        store.load(account.account_id)
    store.save_snapshot(account, "one", now=NOW)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE accounts SET payload='{}'")
    with pytest.raises(ValidationError):
        RiskStateStore(path).load(account.account_id)


def test_restart_does_not_invent_next_day_or_week_pnl(tmp_path, account, proposal):
    store = RiskStateStore(tmp_path / "risk.sqlite")
    store.save_snapshot(account, "one", now=NOW)
    _, loaded = store.load(account.account_id)
    next_week = NOW + timedelta(days=4)
    result = evaluate(proposal, loaded, now=next_week)
    assert not result.approved
    assert any(c.rule == "account_periods_current" and not c.passed for c in result.checks)


def test_stricter_market_freshness_expires_review(tmp_path, account, proposal):
    from desk.risk import RiskLimits
    store = RiskStateStore(tmp_path / "risk.sqlite")
    store.save_snapshot(replace(account, pnl_today=-200), "one", now=NOW)
    _, review = store.review(account.account_id, proposal, now=NOW, contract_book=BOOK,
                            market=MARKET, registry=REGISTRY,
                            limits=RiskLimits(max_market_context_age=timedelta(seconds=2)))
    with pytest.raises(RiskStateError):
        store.acknowledge(review, {"daily_loss"}, actor="Taz", reason="reviewed", now=NOW + timedelta(seconds=3))


def test_data_failure_never_creates_overridable_review(tmp_path, account, proposal):
    store = RiskStateStore(tmp_path / "risk.sqlite")
    store.save_snapshot(replace(account, pnl_today=-200), "one", now=NOW)
    decision, review = store.review(account.account_id, proposal, now=NOW, contract_book=None,
                                   market=MARKET, registry=REGISTRY)
    assert not decision.approved and review is None


def test_new_pnl_period_requires_explicit_reconciled_snapshot(tmp_path, account, proposal):
    store = RiskStateStore(tmp_path / "risk.sqlite")
    store.save_snapshot(replace(account, pnl_today=-200, pnl_this_week=-400), "one", now=NOW)
    monday = NOW + timedelta(days=4)
    fresh = replace(account, as_of=monday, pnl_day=monday.date(), pnl_week_start=monday.date())
    store.save_snapshot(fresh, "monday", now=monday)
    _, loaded = store.load(account.account_id)
    assert loaded.pnl_today == loaded.pnl_this_week == 0


def test_state_schema_mismatch_is_not_reinitialized(tmp_path):
    path = tmp_path / "risk.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(RiskStateError):
        RiskStateStore(path)
