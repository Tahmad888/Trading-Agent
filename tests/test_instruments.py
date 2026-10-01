"""Structure acceptance plus an independent enumerated expiration-payoff oracle."""
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from desk.contracts import Leg, TradeProposal
from desk.playbook.cards import CARDS
from desk.instruments import ContractBook, InstrumentError, loss_measures
from tests.risk_support import evaluate as fixture_evaluate, FixtureTerms
from tests.conftest import NOW
from tests.risk_support import BOOK, contract


class PayoffTerms(FixtureTerms):
    def resolve(self, event_id, now):
        return super().resolve(event_id, now).model_copy(update={"option_exit_prices": {}, "option_exit_source": None})


def evaluate(*args, **kwargs):
    kwargs.setdefault("terms_source", PayoffTerms())
    return fixture_evaluate(*args, **kwargs)


CASES = [
    # structure, (side, right, strike, premium) legs, gross maximum loss, signed premium
    ("long_call", [("buy", "call", 100, 3)], 300, 300),
    ("long_put", [("buy", "put", 100, 3)], 300, 300),
    ("debit_vertical", [("buy", "call", 100, 3), ("sell", "call", 105, 1)], 200, 200),
    ("debit_vertical", [("buy", "put", 105, 3), ("sell", "put", 100, 1)], 200, 200),
    ("credit_vertical", [("sell", "call", 100, 3), ("buy", "call", 105, 1)], 300, -200),
    ("credit_vertical", [("sell", "put", 105, 3), ("buy", "put", 100, 1)], 300, -200),
    ("iron_condor", [("buy", "put", 90, 1), ("sell", "put", 95, 2),
                      ("sell", "call", 105, 2), ("buy", "call", 110, 1)], 300, -200),
    # Unequal protected wings remain a defined-risk condor: use the wider wing.
    ("iron_condor", [("buy", "put", 90, 1), ("sell", "put", 95, 2),
                      ("sell", "call", 105, 2), ("buy", "call", 115, 1)], 800, -200),
]


def build(base, case, quantity=1):
    structure, specs, maximum, premium = case
    contracts = tuple(contract("SPY", "2026-11-20", right, strike) for _, right, strike, _ in specs)
    legs = [Leg(quantity_unit="contract", symbol=c.symbol, side=side, qty=quantity, limit_price=price, expiry=c.expiry, open_interest=2000)
            for c, (side, _, _, price) in zip(contracts, specs)]
    proposal = TradeProposal.model_validate({**base.model_dump(), "structure": structure, "legs": legs,
        "max_loss_usd": None, "sizing_mode": "selected_quantity", "worst_case_loss_usd": maximum * quantity,
        "risk_usd": 20 * quantity + 2, "max_gain_usd": None})
    if case is CASES[1] or case is CASES[3] or case is CASES[4]:
        proposal = proposal.model_copy(update={"event_id": "spy_short", "stop_price": 505,
            "setup_id": "8_raschke_holy_grail", "setup_version": CARDS["8_raschke_holy_grail"].fingerprint()})
    return proposal, ContractBook(source="independent synthetic metadata", as_of=NOW, contracts=contracts)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("quantity", [1, 3, 7])
def test_supported_structures_match_independent_payoff_grid(proposal, account, case, quantity):
    trade, book = build(proposal, case, quantity)
    result = evaluate(trade, account, contract_book=book, now=NOW)
    assert result.approved, result
    assert result.computed_max_loss_usd == case[2] * quantity
    assert result.net_premium_usd == case[3] * quantity
    assert result.final_leg_quantities == [quantity] * len(case[1])
    # Oracle reads test inputs, not production LossMeasures or parsed metadata.
    losses = []
    for spot in range(0, 401):
        profit = Decimal(0)
        for side, right, strike, price in case[1]:
            value = max(0, spot - strike) if right == "call" else max(0, strike - spot)
            profit += (Decimal(value) - Decimal(str(price))) * (1 if side == "buy" else -1) * 100 * quantity
        losses.append(-profit)
    assert max(losses) == result.computed_max_loss_usd
    assert result.broker_buying_power_required_usd is None
    assert not result.broker_requirement_verified


@pytest.mark.parametrize("right", ["call", "put"])
def test_long_calendar_uses_covered_debit_bound(proposal, account, right):
    near = contract("SPY", "2026-10-02", right, 100)
    far = contract("SPY", "2026-11-20", right, 100)
    book = ContractBook(source="fixture", as_of=NOW, contracts=(near, far))
    legs = [Leg(quantity_unit="contract", symbol=c.symbol, side=side, qty=2, limit_price=price, expiry=c.expiry, open_interest=1000)
            for c, side, price in [(near, "sell", 2), (far, "buy", 3)]]
    trade = proposal.model_copy(update={"structure": "calendar", "legs": legs,
        "max_loss_usd": None, "sizing_mode": "selected_quantity", "worst_case_loss_usd": 200, "risk_usd": 42})
    result = evaluate(trade, account, contract_book=book, now=NOW)
    assert result.approved
    assert result.computed_max_loss_usd == result.net_premium_usd == 200
    assert result.loss_basis == "covered_calendar_debit_before_costs"
    for bad_legs in (
        [leg.model_copy(update={"side": "buy" if leg.side == "sell" else "sell"}) for leg in legs],
        [legs[0], legs[1].model_copy(update={"qty": 1})],
        [legs[0], legs[1].model_copy(update={"limit_price": 1})],
    ):
        assert not evaluate(trade.model_copy(update={"legs": bad_legs}), account, contract_book=book, now=NOW).approved


def test_diagonal_is_not_mislabeled_calendar(proposal, account):
    trade, book = build(proposal, CASES[2])
    far = contract("SPY", "2026-12-18", "call", 100)
    book = book.model_copy(update={"contracts": (far, book.contracts[1])})
    legs = [trade.legs[0].model_copy(update={"symbol": far.symbol, "expiry": far.expiry}), trade.legs[1]]
    assert not evaluate(trade.model_copy(update={"structure": "calendar", "legs": legs}),
                        account, contract_book=book, now=NOW).approved


@pytest.mark.parametrize("claim", [21, 199, 201, 2000])
def test_caller_loss_cannot_override_computed_loss(proposal, account, claim):
    trade, book = build(proposal, CASES[2])
    result = evaluate(trade.model_copy(update={"worst_case_loss_usd": claim}), account, contract_book=book, now=NOW)
    assert not result.approved
    assert any(c.rule == "declared_max_loss_matches" and not c.passed for c in result.checks)
    assert result.final_leg_quantities == [0, 0]


def test_option_metadata_cannot_come_from_proposal(proposal, account):
    assert not evaluate(proposal, account, contract_book=None, now=NOW).approved
    with pytest.raises(ValidationError):
        TradeProposal.model_validate({**proposal.model_dump(), "contract_book": BOOK.model_dump()})


@pytest.mark.parametrize("change", [
    {"multiplier": 10}, {"deliverable_shares": 10}, {"deliverable_cash_usd": 1},
    {"deliverable_symbol": "QQQ"}, {"adjusted": True}, {"currency": "EUR"},
    {"exercise_style": "european"}, {"settlement": "cash"}, {"tradable": False},
    {"strike": 101}, {"right": "put"}, {"underlying": "QQQ"},
    {"expiry": NOW.date()}, {"symbol": "SPY261332C00100000"},
    {"multiplier": float("nan")}, {"strike": float("inf")},
])
def test_bad_contract_metadata_fails_closed(proposal, account, change):
    trade, book = build(proposal, CASES[0])
    broken = book.model_copy(update={"contracts": (book.contracts[0].model_copy(update=change),)})
    result = evaluate(trade, account, contract_book=broken, now=NOW)
    assert not result.approved
    assert result.diagnostics


@pytest.mark.parametrize("change", [
    {"as_of": NOW + timedelta(seconds=1)},
    {"as_of": NOW - timedelta(hours=24, seconds=1)},
    {"as_of": NOW.replace(tzinfo=None)}, {"contracts": ()}, {"source": ""},
])
def test_bad_book_evidence_fails_closed(proposal, account, change):
    trade, book = build(proposal, CASES[0])
    assert not evaluate(trade, account, contract_book=book.model_copy(update=change), now=NOW).approved


def test_duplicate_metadata_and_order_legs_rejected(proposal, account):
    trade, book = build(proposal, CASES[2])
    for duplicate in (book.contracts + (book.contracts[0],),
                      (book.contracts[0], book.contracts[1].model_copy(update={"broker_contract_id": book.contracts[0].broker_contract_id}))):
        assert not evaluate(trade, account, contract_book=book.model_copy(update={"contracts": duplicate}), now=NOW).approved
    trade = trade.model_copy(update={"legs": [trade.legs[0], trade.legs[0].model_copy(update={"side": "sell"})]})
    assert not evaluate(trade, account, contract_book=book, now=NOW).approved


@pytest.mark.parametrize("edit", ["right", "expiry", "underlying", "ratio", "reversed", "zero_credit"])
def test_vertical_cannot_hide_uncovered_or_mislabeled_structure(proposal, account, edit):
    trade, book = build(proposal, CASES[4])
    legs, contracts = list(trade.legs), list(book.contracts)
    if edit in {"right", "expiry", "underlying"}:
        contracts[1] = contract("QQQ" if edit == "underlying" else "SPY",
                               "2026-12-18" if edit == "expiry" else "2026-11-20",
                               "put" if edit == "right" else "call", 105)
        legs[1] = legs[1].model_copy(update={"symbol": contracts[1].symbol, "expiry": contracts[1].expiry})
    elif edit == "ratio":
        legs[0] = legs[0].model_copy(update={"qty": 2})
    elif edit == "reversed":
        legs = [leg.model_copy(update={"side": "buy" if leg.side == "sell" else "sell"}) for leg in legs]
    else:
        legs[1] = legs[1].model_copy(update={"limit_price": 3})
    result = evaluate(trade.model_copy(update={"legs": legs}), account,
                      contract_book=book.model_copy(update={"contracts": tuple(contracts)}), now=NOW)
    assert not result.approved


def test_condor_wrong_wing_sides_rejected(proposal, account):
    trade, book = build(proposal, CASES[6])
    legs = [leg.model_copy(update={"side": "buy" if leg.side == "sell" else "sell"}) for leg in trade.legs]
    assert not evaluate(trade.model_copy(update={"legs": legs}), account, contract_book=book, now=NOW).approved


def test_contracts_already_expired_are_rejected(proposal):
    trade, book = build(proposal, CASES[0])
    later = NOW.replace(month=11, day=21)
    with pytest.raises(InstrumentError, match="expired"):
        loss_measures(trade, book.model_copy(update={"as_of": later}), later)


def test_shares_cannot_hide_a_short_or_different_symbol(shares, account):
    for update in ({"side": "sell"}, {"symbol": "SPY"}):
        bad = shares.model_copy(update={"legs": [shares.legs[0].model_copy(update=update)]})
        assert not evaluate(bad, account, now=NOW).approved


def test_downsizing_recalculates_premium_loss_and_funding(proposal, account):
    trade, book = build(proposal, CASES[6], quantity=7)
    result = evaluate(trade.model_copy(update={"risk_usd": 902, "sizing_mode": "maximum_loss_budget"}), account, contract_book=book, now=NOW)
    assert result.approved
    assert result.final_leg_quantities == [3, 3, 3, 3]
    assert result.computed_max_loss_usd == 900
    assert result.net_premium_usd == -600
    assert result.estimated_stop_loss_usd is None
    assert result.estimated_total_risk_usd is None
    assert result.estimated_funding_usd == 902


@pytest.mark.parametrize("change", [
    {"qty": 1.5}, {"qty": True}, {"qty": 0}, {"quantity_unit": "share"},
    {"limit_price": float("inf")}, {"limit_price": float("nan")}, {"limit_price": 0},
])
def test_invalid_option_quantity_or_price_rejected(proposal, account, change):
    trade, book = build(proposal, CASES[0])
    trade = trade.model_copy(update={"legs": [trade.legs[0].model_copy(update=change)]})
    assert not evaluate(trade, account, contract_book=book, now=NOW).approved


def test_explicit_identity_fields_cannot_be_defaulted(proposal):
    for missing in ("multiplier", "deliverable_shares", "deliverable_cash_usd", "broker_contract_id", "adjusted"):
        payload = BOOK.model_dump()
        del payload["contracts"][0][missing]
        with pytest.raises(ValidationError):
            ContractBook.model_validate(payload)
    for missing in ("setup_version", "quote_source", "stop_estimate_source"):
        payload = proposal.model_dump()
        del payload[missing]
        with pytest.raises(ValidationError):
            TradeProposal.model_validate(payload)


def test_metadata_age_boundary_and_provenance(proposal, account):
    trade, book = build(proposal, CASES[0])
    book = book.model_copy(update={"as_of": NOW - timedelta(hours=24)})
    result = evaluate(trade, account, contract_book=book, now=NOW)
    assert result.approved
    assert result.contract_metadata_source == book.source
    assert result.contract_metadata_as_of == book.as_of


def test_long_call_cannot_be_renamed_to_long_put(proposal, account):
    trade, book = build(proposal, CASES[0])
    assert not evaluate(trade.model_copy(update={"structure": "long_put"}), account,
                        contract_book=book, now=NOW).approved


def test_whitespace_inside_occ_identity_cannot_alias_a_real_contract(proposal, account):
    trade, book = build(proposal, CASES[0])
    leg = trade.legs[0].model_copy(update={"symbol": "SP Y261120C00100000"})
    assert not evaluate(trade.model_copy(update={"legs": [leg]}), account, contract_book=book, now=NOW).approved
