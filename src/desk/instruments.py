"""Contract identity and gross strategy loss, separate from stop and broker risk.

ContractBook comes from the adapter, never from analyst proposal JSON. This is a
trust boundary, not authentication: adapters must verify source/deliverable data.
No broker/network calls or account buying-power formula are implemented here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from desk.contracts import LEG_SHAPES, TradeProposal

PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Text = Annotated[str, Field(min_length=1)]
ET = ZoneInfo("America/New_York")


class OptionContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    broker_contract_id: Text
    symbol: Text
    underlying: Text
    right: Literal["call", "put"]
    strike: PositiveDecimal
    expiry: date
    multiplier: PositiveDecimal
    deliverable_symbol: Text
    deliverable_shares: PositiveDecimal
    deliverable_cash_usd: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    adjusted: bool
    exercise_style: Literal["american", "european"]
    settlement: Literal["physical", "cash"]
    currency: Text
    tradable: bool


class ContractBook(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: Text
    as_of: AwareDatetime
    contracts: tuple[OptionContract, ...]


class InstrumentError(ValueError):
    """A proposal cannot be interpreted as the claimed supported structure."""


@dataclass(frozen=True)
class LossMeasures:
    maximum_loss: Decimal
    net_premium: Decimal
    funding_estimate: Decimal
    basis: str


def money(value: float) -> Decimal:
    return Decimal(str(value))


def canonical_symbol(symbol: str) -> str:
    # OCC permits a space-padded root, not spaces inside the root/date/strike.
    match = re.fullmatch(r"([A-Z]{1,6}) *([0-9]{6}[CP][0-9]{8})", symbol)
    if match is None:
        raise InstrumentError("Unsupported or malformed OCC symbol")
    return "".join(match.groups())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InstrumentError(message)


def _identity(contract: OptionContract) -> None:
    match = re.fullmatch(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})", canonical_symbol(contract.symbol))
    _require(match is not None, "Unsupported or malformed OCC symbol")
    root, expiry, right, strike = match.groups()
    try:
        decoded_date = date(2000 + int(expiry[:2]), int(expiry[2:4]), int(expiry[4:]))
    except ValueError as exc:
        raise InstrumentError("Invalid OCC expiry") from exc
    _require((root, decoded_date, right, Decimal(strike) / 1000) ==
             (contract.underlying, contract.expiry, "C" if contract.right == "call" else "P", contract.strike),
             "OCC identity contradicts contract metadata")
    _require(not contract.adjusted and contract.multiplier == 100 and
             contract.deliverable_symbol == contract.underlying and contract.deliverable_shares == 100 and
             contract.deliverable_cash_usd == 0 and contract.currency == "USD" and
             contract.exercise_style == "american" and contract.settlement == "physical",
             "Only verified standard USD physical American contracts are supported")
    _require(contract.tradable, "Contract is not tradable")


def loss_measures(proposal: TradeProposal, book: ContractBook | None, now: datetime,
                  max_metadata_age: timedelta = timedelta(hours=24)) -> LossMeasures:
    """Validate identity/shape and compute full-proposal loss at its limit prices.

    Same-expiry payoffs are piecewise linear: extrema are at zero, strikes, or
    infinity. Calendars use a separate covered-calendar bound, not that curve.
    Fees, slippage, dividends/borrow and assignment funding are outside this bound.
    """
    legs = proposal.legs
    if proposal.structure == "shares":
        _require(len(legs) == 1 and legs[0].side == "buy" and legs[0].quantity_unit == "share" and legs[0].expiry is None and
                 legs[0].symbol == proposal.instrument, "Expected one matching long-share leg")
        debit = money(legs[0].limit_price) * legs[0].qty
        return LossMeasures(debit, debit, debit, "long_shares_to_zero")
    _require(proposal.structure in LEG_SHAPES, "Unsupported structure")
    _require(all(leg.quantity_unit == "contract" for leg in legs), "Option quantities must be whole contracts")
    _require(book is not None, "Broker contract metadata is required separately from the proposal")
    # Revalidate copies/constructed models at this trust boundary too.
    book = ContractBook.model_validate(book.model_dump(warnings=False))
    age = now - book.as_of
    _require(timedelta(0) <= age <= max_metadata_age, "Contract metadata is stale or future dated")
    symbols = [canonical_symbol(c.symbol) for c in book.contracts]
    _require(len(symbols) == len(set(symbols)), "Duplicate contract metadata")
    ids = [c.broker_contract_id for c in book.contracts]
    _require(len(ids) == len(set(ids)), "Duplicate broker contract identity")
    lookup = dict(zip(symbols, book.contracts))
    leg_symbols = [canonical_symbol(leg.symbol) for leg in legs]
    _require(len(leg_symbols) == len(set(leg_symbols)), "Duplicate order legs")
    _require(all(symbol in lookup for symbol in leg_symbols), "Unknown option contract")
    contracts = [lookup[symbol] for symbol in leg_symbols]
    for leg, contract in zip(legs, contracts):
        _identity(contract)
        _require(contract.underlying == proposal.instrument and leg.expiry == contract.expiry,
                 "Proposal underlying or expiry contradicts contract metadata")
        _require(contract.expiry >= now.astimezone(ET).date(), "Option has already expired")
    shape = (sum(leg.side == "buy" for leg in legs), sum(leg.side == "sell" for leg in legs))
    _require(shape == LEG_SHAPES[proposal.structure], "Leg sides/counts contradict structure")
    _require(len({leg.qty for leg in legs}) == 1, "Unsupported unequal strategy ratios")
    premium = sum((money(leg.limit_price) * leg.qty * c.multiplier * (1 if leg.side == "buy" else -1)
                   for leg, c in zip(legs, contracts)), Decimal(0))
    buys = [c for leg, c in zip(legs, contracts) if leg.side == "buy"]
    sells = [c for leg, c in zip(legs, contracts) if leg.side == "sell"]
    structure = proposal.structure
    if structure in {"long_call", "long_put"}:
        _require(contracts[0].right == structure[5:], "Long option has the wrong right")
    elif structure == "calendar":
        bought, sold = buys[0], sells[0]
        _require(bought.right == sold.right and bought.strike == sold.strike and
                 bought.expiry > sold.expiry and premium > 0,
                 "Calendar must buy the later expiry and sell the earlier, same strike/right, for a debit")
        return LossMeasures(premium, premium, premium, "covered_calendar_debit_before_costs")
    elif structure in {"debit_vertical", "credit_vertical"}:
        bought, sold = buys[0], sells[0]
        _require(bought.right == sold.right and bought.expiry == sold.expiry and bought.strike != sold.strike,
                 "Vertical needs different strikes, matching right and expiry")
        debit_direction = (bought.strike < sold.strike) == (bought.right == "call")
        _require(debit_direction == (structure == "debit_vertical") and
                 ((premium > 0) if structure == "debit_vertical" else (premium < 0)),
                 "Vertical strike ordering or net premium contradicts structure")
        width = abs(bought.strike - sold.strike) * bought.multiplier * legs[0].qty
        _require(abs(premium) < width, "Vertical premium must be inside its strike width")
    elif structure == "iron_condor":
        ordered = sorted(zip(legs, contracts), key=lambda pair: pair[1].strike)
        _require(len({c.expiry for c in contracts}) == 1 and len({c.strike for c in contracts}) == 4 and
                 [(leg.side, c.right) for leg, c in ordered] ==
                 [("buy", "put"), ("sell", "put"), ("sell", "call"), ("buy", "call")] and premium < 0,
                 "Credit iron condor requires ordered protected put/call wings and one expiry")

    # All remaining structures must share expiry; calendar returned above.
    _require(len({c.expiry for c in contracts}) == 1, "Mismatched expiries")
    call_tail = sum(c.multiplier * leg.qty * (1 if leg.side == "buy" else -1)
                    for leg, c in zip(legs, contracts) if c.right == "call")
    _require(call_tail >= 0, "Unbounded short-call exposure")

    def pnl(spot: Decimal) -> Decimal:
        intrinsic = sum((max(Decimal(0), spot - c.strike if c.right == "call" else c.strike - spot)
                         * c.multiplier * leg.qty * (1 if leg.side == "buy" else -1)
                         for leg, c in zip(legs, contracts)), Decimal(0))
        return intrinsic - premium

    maximum = -min(pnl(spot) for spot in {Decimal(0), *(c.strike for c in contracts)})
    _require(maximum > 0, "Nonpositive maximum loss: inconsistent strategy prices")
    return LossMeasures(maximum, premium, max(maximum, premium), "intact_same_expiry_payoff_before_costs")
