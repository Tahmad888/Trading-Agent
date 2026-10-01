"""Risk layer tests carried from the paused ai-trading-desk code, updated for
dollar risk per trade (blueprint v2.4) and with the perp case removed. They include the
adversarial cases the first gate names: oversized qty, stale buying power,
spread > 10%. (The missing-token case belongs to the execution adapter.)"""

from dataclasses import replace
from datetime import timedelta

from tests.risk_support import evaluate
from tests.conftest import NOW


def failed(decision):
    return {c.rule for c in decision.checks if not c.passed}


def test_clean_proposal_approved_at_full_size(proposal, account):
    d = evaluate(proposal, account, now=NOW)
    assert d.approved, failed(d)
    assert d.final_qty_multiplier == 1.0


def test_oversized_qty_is_shrunk_never_grown(proposal, account):
    big = proposal.model_copy(update={
        "legs": [leg.model_copy(update={"qty": 10}) for leg in proposal.legs],
        "max_loss_usd": 200, "worst_case_loss_usd": 200,
    })
    d = evaluate(big, account, now=NOW)
    assert d.approved, failed(d)
    assert d.final_qty_multiplier == 0.1          # 10 lots -> 1 lot, the ticket's $22 including $2 costs
    assert d.final_qty_multiplier <= 1.0


def test_unsizable_trade_rejected(proposal, account):
    # One lot risking $400 cannot be shrunk below one contract.
    d = evaluate(proposal.model_copy(update={"max_loss_usd": 400, "worst_case_loss_usd": 400}),
                 account, now=NOW)
    assert not d.approved
    assert "declared_stop_loss_matches" in failed(d)
    assert d.final_qty_multiplier == 0.0


def test_stale_account_state_rejected(proposal, account):
    stale = replace(account, as_of=NOW - timedelta(minutes=5))
    assert "account_state_fresh" in failed(evaluate(proposal, stale, now=NOW))


def test_future_dated_account_state_rejected(proposal, account):
    future = replace(account, as_of=NOW + timedelta(minutes=1))
    assert "account_state_fresh" in failed(evaluate(proposal, future, now=NOW))


def test_wide_option_spread_rejected(proposal, account):
    wide = proposal.model_copy(update={"option_spread_pct_mid": 0.15})
    assert "option_spread" in failed(evaluate(wide, account, now=NOW))


def test_option_structure_without_spread_data_rejected(proposal, account):
    unknown = proposal.model_copy(update={"option_spread_pct_mid": None})
    assert "option_spread_known" in failed(evaluate(unknown, account, now=NOW))


def test_insufficient_buying_power_rejected(proposal, account):
    poor = replace(account, buying_power=20)
    assert "buying_power" in failed(evaluate(proposal, poor, now=NOW))


def test_negative_margin_excess_rejected(proposal, account):
    deficit = replace(account, margin_excess=-1)
    assert "margin_excess_positive" in failed(evaluate(proposal, deficit, now=NOW))


def test_daily_loss_is_a_warning(proposal, account):
    down = replace(account, pnl_today=-200)
    d = evaluate(proposal, down, now=NOW)
    assert d.approved and "daily_loss" in {w.code for w in d.warnings}


def test_weekly_loss_is_a_warning(proposal, account):
    down = replace(account, pnl_this_week=-400)
    d = evaluate(proposal, down, now=NOW)
    assert d.approved and "weekly_loss" in {w.code for w in d.warnings}


def test_manual_halt_respected(proposal, account):
    assert "not_halted" in failed(evaluate(proposal, replace(account, halted=True), now=NOW))


def test_drawdown_is_a_warning(proposal, account):
    dd = replace(account, equity=9_000, equity_high_water_mark=10_000)
    d = evaluate(proposal, dd, now=NOW)
    assert d.approved and "account_drawdown" in {w.code for w in d.warnings}


def test_already_moved_rejected(proposal, account):
    chased = proposal.model_copy(update={"already_moved_pct": -0.06})
    assert "already_moved" in failed(evaluate(chased, account, now=NOW))
