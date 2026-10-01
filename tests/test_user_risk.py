"""Behavioral acceptance for Taz's chosen budget, independent of analyst grade."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from desk.contracts import TradeProposal
from desk.risk import _size
from tests.risk_support import evaluate
from tests.conftest import NOW


@pytest.mark.parametrize("budget", [None, 0, -1, float("nan"), float("inf"), -float("inf"), True, "150"])
def test_invalid_budget_rejected_at_contract_and_financial_boundary(proposal, account, budget):
    with pytest.raises(ValidationError):
        TradeProposal.model_validate({**proposal.model_dump(), "risk_usd": budget})
    # Internal copies bypass model construction; the engine must still fail closed.
    decision = evaluate(proposal.model_copy(update={"risk_usd": budget}), account, now=NOW)
    assert not decision.approved
    assert decision.final_qty_multiplier == 0
    assert decision.checks[0].rule == "proposal_valid"


def test_missing_budget_has_no_default(proposal):
    payload = proposal.model_dump()
    del payload["risk_usd"]
    with pytest.raises(ValidationError):
        TradeProposal.model_validate(payload)


@pytest.mark.parametrize("field", ["est_costs_usd", "max_loss_usd", "worst_case_loss_usd"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
def test_invalid_loss_and_cost_estimates_cannot_produce_size(proposal, account, field, bad):
    assert not evaluate(proposal.model_copy(update={field: bad}), account, now=NOW).approved


@pytest.mark.parametrize("grade", ["A", "B", "C", None])
def test_grade_never_changes_the_selected_budget_or_size(long_call, account, grade):
    trade = long_call.model_copy(update={
        "grade": grade, "risk_usd": 150, "max_loss_usd": 180, "est_costs_usd": 0,
    })
    result = evaluate(trade, account, now=NOW)
    assert result.approved
    assert result.risk_budget_usd == 150
    assert result.final_leg_quantities == [2]
    assert result.estimated_stop_loss_usd == 120
    assert result.estimated_total_risk_usd == 120


def test_cost_reserve_is_in_budget_even_when_quantity_shrinks(long_call, account):
    trade = long_call.model_copy(update={"max_loss_usd": 60, "risk_usd": 60})
    result = evaluate(trade, account, now=NOW)
    assert result.approved
    assert result.final_leg_quantities == [2]  # 3 would cost $60 + $3.
    assert result.estimated_stop_loss_usd == 40
    assert result.cost_reserve_usd == 3  # Retain full reserve, including any fixed fees.
    assert result.estimated_total_risk_usd == 43


@pytest.mark.parametrize("budget", [1, 2, 20, 21.99])
def test_costs_cannot_push_one_unit_over_budget(proposal, account, budget):
    result = evaluate(proposal.model_copy(update={"risk_usd": budget}), account, now=NOW)
    assert not result.approved
    assert result.final_leg_quantities == [0, 0]


def test_integer_ratio_sizing_never_produces_a_fractional_leg(proposal):
    # Arithmetic only: this does not assert these legs form an allowed vertical.
    legs = [leg.model_copy(update={"qty": q}) for leg, q in zip(proposal.legs, [2, 3])]
    trade = proposal.model_copy(update={"legs": legs, "risk_usd": 12})
    assert _size(trade)[0] == [0, 0]  # A half unit cannot become one and 1.5 contracts.
    legs = [leg.model_copy(update={"qty": q}) for leg, q in zip(proposal.legs, [4, 6])]
    assert _size(trade.model_copy(update={"legs": legs}))[0] == [2, 3]


def test_exact_decimal_budget_boundary(shares, account):
    trade = shares.model_copy(update={
        "legs": [shares.legs[0].model_copy(update={"qty": 3})],
        "worst_case_loss_usd": 750, "max_loss_usd": 0.3, "est_costs_usd": 0, "risk_usd": 0.3,
    })
    assert evaluate(trade, account, now=NOW).final_leg_quantities == [3]
    assert evaluate(trade.model_copy(update={"risk_usd": 0.29}), account, now=NOW).final_leg_quantities == [2]


def test_sizes_match_enumeration_of_affordable_whole_shares(shares, account):
    # Independent oracle: enumerate all available quantities and their decimal costs.
    for count in (1, 3, 7, 20):
        for unit_loss in (Decimal("0.10"), Decimal("1.25"), Decimal("20.01")):
            for reserve in (Decimal("0"), Decimal("2.75")):
                for budget in (Decimal("0.30"), Decimal("25"), Decimal("150"), Decimal("500")):
                    trade = shares.model_copy(update={
                        "legs": [shares.legs[0].model_copy(update={"qty": count})],
                        "max_loss_usd": float(unit_loss * count),
                        "worst_case_loss_usd": 250 * count,
                        "est_costs_usd": float(reserve), "risk_usd": float(budget),
                    })
                    affordable = [q for q in range(1, count + 1) if q * unit_loss + reserve <= budget]
                    expected = max(affordable, default=0)
                    result = evaluate(trade, account, now=NOW)
                    assert result.final_leg_quantities == [expected]
                    assert result.approved == bool(expected)
                    if result.approved:
                        assert result.estimated_total_risk_usd <= float(budget)
