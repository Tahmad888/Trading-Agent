"""Tests for the blueprint v2.3 step 2 changes to the risk engine and contracts."""

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import ValidationError

from desk.contracts import Leg, TradeProposal
from desk.risk import OpenPosition, RiskLimits, evaluate
from tests.conftest import NOW, TODAY
from tests.test_risk import failed


# (1) Perps are gone.

def test_perp_structures_no_longer_accepted(proposal):
    data = proposal.model_dump()
    for structure in ("perp_long", "perp_short"):
        with pytest.raises(ValidationError):
            TradeProposal(**{**data, "structure": structure})


# (2) Worst-case loss field and the 2% cap.

def test_worst_case_below_stop_loss_is_invalid(proposal):
    with pytest.raises(ValidationError):
        TradeProposal(**{**proposal.model_dump(), "worst_case_loss_usd": 10})


def test_bought_premium_over_cap_rejected_even_with_tight_stop(long_call, account):
    # One call with $300 of premium: the stop risks only $20, but the whole
    # premium counts against the 2% cap ($200 on $10k).
    one = long_call.model_copy(update={
        "legs": [long_call.legs[0].model_copy(update={"qty": 1, "limit_price": 3.00})],
        "max_loss_usd": 20, "worst_case_loss_usd": 300,
    })
    d = evaluate(one, account, now=NOW)
    assert not d.approved
    assert "worst_case_cap" in failed(d)


def test_worst_case_cap_shrinks_below_stop_budget(long_call, account):
    # Stop budget at tier 2 ($50) allows 2 of 3 calls; the 2% cap allows 1.
    d = evaluate(long_call, account, now=NOW)
    assert d.approved, failed(d)
    assert d.final_qty_multiplier == pytest.approx(1 / 3)


def test_shares_are_sized_by_stop_not_the_option_cap(shares, account):
    d = evaluate(shares, account, now=NOW)
    assert d.approved, failed(d)
    assert "worst_case_cap" not in {c.rule for c in d.checks}


def test_share_cost_counts_against_buying_power(shares, account):
    assert "buying_power" in failed(evaluate(shares, replace(account, buying_power=4_000), now=NOW))


# (3) Sizing by tier, 0.25% default.

def test_default_risk_is_quarter_percent():
    assert RiskLimits().tier1_risk_pct == 0.0025
    assert RiskLimits().tier2_risk_pct == 0.005


def test_tier_zero_is_paper_only(proposal, account):
    paper = proposal.model_copy(update={"tier": 0})
    live = evaluate(paper, account, now=NOW)
    assert not live.approved
    assert "tier_allows_live" in failed(live)
    d = evaluate(paper, account, now=NOW, live=False)
    assert d.approved, failed(d)
    assert d.final_qty_multiplier == 1.0          # sized like tier 1


def test_tier_one_budget_rejects_what_tier_two_allows(proposal, account):
    thirty = proposal.model_copy(update={"max_loss_usd": 30, "worst_case_loss_usd": 30})
    assert "risk_per_trade_sizable" in failed(evaluate(thirty, account, now=NOW))
    assert evaluate(thirty.model_copy(update={"tier": 2}), account, now=NOW).approved


def test_tier_two_sizes_at_half_percent(proposal, account):
    big = proposal.model_copy(update={
        "legs": [leg.model_copy(update={"qty": 10}) for leg in proposal.legs],
        "max_loss_usd": 200, "worst_case_loss_usd": 200,
    })
    assert evaluate(big, account, now=NOW).final_qty_multiplier == 0.1
    assert evaluate(big.model_copy(update={"tier": 2}), account, now=NOW).final_qty_multiplier == 0.2


# (4) Open interest, the 14-day rule and leg expiries.

def _with_legs(p, **update):
    return p.model_copy(update={"legs": [leg.model_copy(update=update) for leg in p.legs]})


def test_low_open_interest_rejected(proposal, account):
    assert "open_interest" in failed(evaluate(_with_legs(proposal, open_interest=499), account, now=NOW))
    assert evaluate(_with_legs(proposal, open_interest=500), account, now=NOW).approved


def test_missing_option_leg_data_rejected(proposal, account):
    assert "option_leg_data_known" in failed(
        evaluate(_with_legs(proposal, open_interest=None), account, now=NOW))
    assert "option_leg_data_known" in failed(
        evaluate(_with_legs(proposal, expiry=None), account, now=NOW))


def test_no_opening_buys_under_14_days(long_call, account):
    ten = _with_legs(long_call, expiry=TODAY + timedelta(days=10))
    assert "days_to_expiry_for_buys" in failed(evaluate(ten, account, now=NOW))
    fourteen = _with_legs(long_call, expiry=TODAY + timedelta(days=14))
    assert evaluate(fourteen, account, now=NOW).approved


def test_calendar_checks_its_bought_back_month(proposal, account):
    front = Leg(symbol="SPY 261002C00500000", side="sell", qty=1, limit_price=2.00,
                expiry=TODAY + timedelta(days=8), open_interest=3_000)
    back = Leg(symbol="SPY 261016C00500000", side="buy", qty=1, limit_price=3.00,
               expiry=TODAY + timedelta(days=22), open_interest=3_000)
    cal = proposal.model_copy(update={"structure": "calendar", "legs": [front, back]})
    assert evaluate(cal, account, now=NOW).approved
    short_back = cal.model_copy(update={"legs": [front, back.model_copy(
        update={"expiry": TODAY + timedelta(days=12)})]})
    assert "days_to_expiry_for_buys" in failed(evaluate(short_back, account, now=NOW))


def test_credit_spread_wing_is_exempt_from_14_day_rule(proposal, account):
    # The fixture's bought wing expires in 6 days; it protects a credit spread.
    d = evaluate(proposal, account, now=NOW)
    assert d.approved, failed(d)
    assert "days_to_expiry_for_buys" not in {c.rule for c in d.checks}


def test_share_leg_with_an_expiry_rejected(shares, account):
    odd = _with_legs(shares, expiry=TODAY + timedelta(days=30))
    assert "legs_match_structure" in failed(evaluate(odd, account, now=NOW))


# (5) Setup and tier on every proposal; single long calls and puts.

def test_proposal_requires_setup_and_tier(proposal):
    data = proposal.model_dump()
    for missing in ("setup_id", "tier"):
        with pytest.raises(ValidationError):
            TradeProposal(**{k: v for k, v in data.items() if k != missing})
    with pytest.raises(ValidationError):
        TradeProposal(**{**data, "tier": 3})


def test_single_long_put_allowed(long_call, account):
    put = long_call.model_copy(update={"structure": "long_put", "tier": 1})
    d = evaluate(put, account, now=NOW)
    assert d.approved, failed(d)


# (6) 3% total open risk backstop.

def test_total_open_risk_backstop(proposal, account):
    near_cap = replace(account, open_positions=[OpenPosition("QQQ", "tech", 150),
                                                OpenPosition("XLE", "energy", 140)])
    d = evaluate(proposal, near_cap, now=NOW)          # 290 + 20 > 300
    assert "total_open_risk" in failed(d)
    ok = replace(near_cap, open_positions=[OpenPosition("QQQ", "tech", 150),
                                           OpenPosition("XLE", "energy", 130)])
    assert evaluate(proposal, ok, now=NOW).approved   # 280 + 20 = 300


# Leg shapes: a structure's legs must match its name.

def test_long_call_with_a_sold_leg_rejected(long_call, account):
    naked = _with_legs(long_call, side="sell")
    assert "legs_match_structure" in failed(evaluate(naked, account, now=NOW))


def test_vertical_with_two_sold_legs_rejected(proposal, account):
    both_sold = _with_legs(proposal, side="sell")
    assert "legs_match_structure" in failed(evaluate(both_sold, account, now=NOW))


def test_shares_with_two_legs_rejected(shares, account):
    two = shares.model_copy(update={"legs": shares.legs * 2})
    assert "legs_match_structure" in failed(evaluate(two, account, now=NOW))
