"""Tests for the blueprint v2.3 step 2 changes that v2.4 keeps. v2.4 dropped
the 2% worst-case cap, the percentage tiers and the 3% total-risk backstop;
their tests are gone and test_risk_v24.py covers what replaced them."""

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import ValidationError

from desk.contracts import Leg, TradeProposal
from desk.risk import evaluate
from tests.conftest import NOW, TODAY
from tests.test_risk import failed


# (1) Perps are gone.

def test_perp_structures_no_longer_accepted(proposal):
    data = proposal.model_dump()
    for structure in ("perp_long", "perp_short"):
        with pytest.raises(ValidationError):
            TradeProposal(**{**data, "structure": structure})


# (2) Worst-case loss field.

def test_worst_case_below_stop_loss_is_invalid(proposal):
    with pytest.raises(ValidationError):
        TradeProposal(**{**proposal.model_dump(), "worst_case_loss_usd": 10})


def test_shares_are_sized_by_stop(shares, account):
    d = evaluate(shares, account, now=NOW)
    assert d.approved, failed(d)
    assert d.final_qty_multiplier == 1.0          # $25 at the stop inside the ticket's $50


def test_share_cost_counts_against_buying_power(shares, account):
    assert "buying_power" in failed(evaluate(shares, replace(account, buying_power=4_000), now=NOW))


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
        TradeProposal(**{**data, "tier": 2})


def test_single_long_put_allowed(long_call, account):
    put = long_call.model_copy(update={"structure": "long_put", "tier": 1})
    d = evaluate(put, account, now=NOW)
    assert d.approved, failed(d)


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
