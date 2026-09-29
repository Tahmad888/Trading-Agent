"""Tests for blueprint v2.4: dollar risk per trade up to a hard ceiling, no
caps on trade or position counts, dollar loss limits, and the quote-time and
trading-status checks."""

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import ValidationError

from desk.contracts import TradeProposal
from desk.risk import GRADE_RISK_USD, RiskLimits, evaluate, suggested_risk_usd
from tests.conftest import NOW
from tests.test_risk import failed


def _lots(p, qty, max_loss, worst_case=None):
    return p.model_copy(update={
        "legs": [leg.model_copy(update={"qty": qty}) for leg in p.legs],
        "max_loss_usd": max_loss, "worst_case_loss_usd": worst_case or max_loss,
    })


# Dollar risk: the grade suggests it, Taz sets any amount up to the ceiling.

def test_grade_suggestions():
    assert GRADE_RISK_USD == {"A": 100.0, "B": 50.0, "C": 20.0}
    assert suggested_risk_usd("B") == 50.0


def test_ticket_risk_sizes_the_trade(proposal, account):
    ten = _lots(proposal, 10, 200)                    # $20 a lot at the stop
    for risk, expected in ((20, 0.1), (35, 0.1), (80, 0.4), (100, 0.5)):
        d = evaluate(ten.model_copy(update={"risk_usd": risk}), account, now=NOW)
        assert d.approved, failed(d)
        assert d.final_qty_multiplier == pytest.approx(expected)


def test_ceiling_holds_whatever_the_ticket_says(proposal, account):
    ten = _lots(proposal, 10, 200).model_copy(update={"risk_usd": 500})
    d = evaluate(ten, account, now=NOW)
    assert d.approved, failed(d)
    assert d.final_qty_multiplier == 0.5              # $100, not $500


def test_ceiling_is_configurable(proposal, account):
    ten = _lots(proposal, 10, 200).model_copy(update={"risk_usd": 200})
    d = evaluate(ten, account, limits=RiskLimits(max_risk_per_trade_usd=150), now=NOW)
    assert d.final_qty_multiplier == pytest.approx(0.7)


def test_risk_must_be_positive(proposal):
    with pytest.raises(ValidationError):
        TradeProposal(**{**proposal.model_dump(), "risk_usd": 0})


def test_small_account_spread_now_fits(proposal, account):
    # The v2.3 problem: a $1-wide credit spread for $0.30 on a $5,000 account
    # (stop at 2x credit = $30, max $70) was rejected at 0.25%. At $50 it fits.
    five_k = replace(account, equity=5_000, equity_high_water_mark=5_000,
                     buying_power=5_000, margin_excess=5_000)
    spread = _lots(proposal, 1, 30, 70).model_copy(update={"risk_usd": 50})
    d = evaluate(spread, five_k, now=NOW)
    assert d.approved, failed(d)


def test_bought_premium_is_limited_by_buying_power_not_a_cap(long_call, account):
    # 3 calls, $450 of premium, $60 at the stop on a $100 ticket: approved.
    d = evaluate(long_call, account, now=NOW)
    assert d.approved, failed(d)
    assert d.final_qty_multiplier == 1.0
    assert "buying_power" in failed(evaluate(long_call, replace(account, buying_power=400), now=NOW))


def test_paper_is_sized_like_live(proposal, account):
    paper = _lots(proposal, 10, 200).model_copy(update={"tier": 0, "risk_usd": 60})
    assert "tier_allows_live" in failed(evaluate(paper, account, now=NOW))
    d = evaluate(paper, account, now=NOW, live=False)
    assert d.approved, failed(d)
    assert d.final_qty_multiplier == pytest.approx(0.3)


# No count caps.

def test_no_position_count_or_sector_rules(proposal, account):
    rules = {c.rule for c in evaluate(proposal, account, now=NOW).checks}
    assert not rules & {"max_open_positions", "max_per_sector", "total_open_risk", "worst_case_cap"}


# Loss limits in dollars.

def test_loss_limits_in_dollars(proposal, account):
    assert evaluate(proposal, replace(account, pnl_today=-199), now=NOW).approved
    assert "daily_loss_limit" in failed(evaluate(proposal, replace(account, pnl_today=-200), now=NOW))
    assert "weekly_loss_limit" in failed(
        evaluate(proposal, replace(account, pnl_this_week=-400), now=NOW))


# Quote time and trading status.

def test_stale_quote_rejected(proposal, account):
    old = proposal.model_copy(update={"quote_as_of": NOW - timedelta(seconds=61)})
    assert "quote_fresh" in failed(evaluate(old, account, now=NOW))
    future = proposal.model_copy(update={"quote_as_of": NOW + timedelta(seconds=5)})
    assert "quote_fresh" in failed(evaluate(future, account, now=NOW))


def test_halted_or_unknown_status_rejected(proposal, account):
    for status in (False, None):
        p = proposal.model_copy(update={"security_tradable": status})
        assert "security_tradable" in failed(evaluate(p, account, now=NOW))


def test_naive_quote_time_refused(proposal):
    # A quote time without a timezone can't be compared with the clock, so
    # the contract refuses it instead of letting evaluate() crash.
    with pytest.raises(ValidationError):
        TradeProposal(**{**proposal.model_dump(), "quote_as_of": NOW.replace(tzinfo=None)})
