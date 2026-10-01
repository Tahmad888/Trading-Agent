"""Risk and sizing layer. Deterministic code only: no LLM ever touches this.

Limits are blueprint v2.4 section 10. Size is a dollar amount per trade: the
user selects a budget including estimated execution costs, without a fixed
per-trade ceiling. Grade is descriptive only. There is no cap on the number
of trades or positions; quality, not quantity, is the goal. The checks that remain stop a bad or untradable trade.

The layer can only shrink or reject a proposal. It never grows a size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from desk.contracts import (DEBIT_STRUCTURES, LEG_SHAPES, OPTION_STRUCTURES, RiskDecision,
                            RuleCheck, TradeProposal)
from desk.instruments import ContractBook, InstrumentError, loss_measures, money

EXCHANGE_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class RiskLimits:
    daily_loss_usd: float = 200.0            # halts new entries until manual reset [Assumption]
    weekly_loss_usd: float = 400.0           # [Assumption]
    kill_switch_drawdown_pct: float = 0.10   # from equity high-water mark
    max_option_spread_pct_mid: float = 0.10
    min_open_interest: int = 500             # at every leg's strike
    min_days_to_expiry_for_buys: int = 14    # no opening purchases closer to expiry
    max_already_moved_pct: float = 0.03      # fast move belongs to faster players
    max_account_state_age: timedelta = timedelta(seconds=30)
    max_quote_age: timedelta = timedelta(seconds=60)
    max_contract_metadata_age: timedelta = timedelta(hours=24)  # engineering assumption


@dataclass(frozen=True)
class AccountState:
    """Broker account snapshot, read fresh for every proposal."""

    as_of: datetime
    equity: float
    equity_high_water_mark: float
    buying_power: float
    margin_excess: float
    pnl_today: float
    pnl_this_week: float
    halted: bool = False                     # set by loss limits; only Taz resets it


def _size(proposal: TradeProposal) -> tuple[list[int], Fraction]:
    """Size exact whole strategy units, reserving the full estimated costs.

    Decimal strings avoid floating-point rounding at dollar/quantity boundaries.
    GCD, rather than the smallest leg, preserves every leg's integer ratio.
    """
    units = math.gcd(*(leg.qty for leg in proposal.legs))
    available = max(Fraction(0), Fraction(str(proposal.risk_usd)) - Fraction(str(proposal.est_costs_usd)))
    affordable = (available * units) // Fraction(str(proposal.max_loss_usd))
    selected = min(units, affordable)
    return [leg.qty // units * selected for leg in proposal.legs], Fraction(selected, units)


def evaluate(
    proposal: TradeProposal,
    account: AccountState,
    limits: RiskLimits = RiskLimits(),
    now: datetime | None = None,
    live: bool = True,
    *,
    contract_book: ContractBook | None = None,
) -> RiskDecision:
    """Local eligibility only, not order authorization or verified broker margin.

    ``contract_book`` is supplied independently by the data/broker adapter.
    ``live=False`` sizes a paper trade using the same arithmetic.
    """
    now = now or datetime.now(timezone.utc)
    checks: list[RuleCheck] = []
    diagnostics: list[str] = []

    def check(rule: str, passed: bool, value: float, limit: float) -> None:
        checks.append(RuleCheck(rule=rule, passed=passed, value=value, limit=limit))

    # model_copy/model_construct can bypass Pydantic validation. Revalidate at
    # the financial boundary so invalid budgets cannot receive an approval.
    try:
        proposal = TradeProposal.model_validate(proposal.model_dump(warnings=False))
    except ValidationError:
        return RiskDecision(
            proposal_id=proposal.proposal_id, approved=False, final_qty_multiplier=0.0,
            checks=[RuleCheck(rule="proposal_valid", passed=False, value=0.0, limit=1.0)],
            buying_power_snapshot=account.buying_power, margin_excess_snapshot=account.margin_excess,
        )

    age = (now - account.as_of).total_seconds()
    check("account_state_fresh", 0 <= age <= limits.max_account_state_age.total_seconds(),
          age, limits.max_account_state_age.total_seconds())
    check("not_halted", not account.halted, float(account.halted), 0.0)
    if live:
        check("tier_allows_live", proposal.tier >= 1, proposal.tier, 1)

    equity = account.equity
    drawdown = 1 - equity / account.equity_high_water_mark if account.equity_high_water_mark > 0 else 1.0
    drawdown = round(drawdown, 9)  # avoid float noise at the exact limit
    check("kill_switch_drawdown", drawdown < limits.kill_switch_drawdown_pct,
          drawdown, limits.kill_switch_drawdown_pct)

    day_loss = max(0.0, -account.pnl_today)
    check("daily_loss_limit", day_loss < limits.daily_loss_usd, day_loss, limits.daily_loss_usd)
    week_loss = max(0.0, -account.pnl_this_week)
    check("weekly_loss_limit", week_loss < limits.weekly_loss_usd, week_loss, limits.weekly_loss_usd)

    # A fresh quote doesn't prove the stock trades: a halted stock can show
    # fresh quotes before it reopens. Unknown status fails closed.
    quote_age = (now - proposal.quote_as_of).total_seconds()
    check("quote_fresh", 0 <= quote_age <= limits.max_quote_age.total_seconds(),
          quote_age, limits.max_quote_age.total_seconds())
    check("security_tradable", proposal.security_tradable is True,
          float(bool(proposal.security_tradable)), 1.0)

    is_option = proposal.structure in OPTION_STRUCTURES
    legs_have_expiry = [leg.expiry is not None for leg in proposal.legs]
    if is_option:
        shape = (sum(leg.side == "buy" for leg in proposal.legs),
                 sum(leg.side == "sell" for leg in proposal.legs))
        shape_ok = shape == LEG_SHAPES[proposal.structure]
    else:
        shape_ok = len(proposal.legs) == 1 and not any(legs_have_expiry)
    check("legs_match_structure", shape_ok, float(shape_ok), 1.0)
    if is_option:
        # Every option leg must carry its expiry and open interest; missing data fails closed.
        known = all(legs_have_expiry) and all(leg.open_interest is not None for leg in proposal.legs)
        check("option_leg_data_known", known, float(known), 1.0)
        if known:
            min_oi = min(leg.open_interest for leg in proposal.legs)
            check("open_interest", min_oi >= limits.min_open_interest, min_oi, limits.min_open_interest)
            if proposal.structure in DEBIT_STRUCTURES:
                today = now.astimezone(EXCHANGE_TZ).date()
                min_dte = min(((leg.expiry - today).days for leg in proposal.legs if leg.side == "buy"),
                              default=-1)   # no bought leg: malformed, fail closed
                check("days_to_expiry_for_buys", min_dte >= limits.min_days_to_expiry_for_buys,
                      min_dte, limits.min_days_to_expiry_for_buys)
        if proposal.option_spread_pct_mid is not None:
            check("option_spread", proposal.option_spread_pct_mid <= limits.max_option_spread_pct_mid,
                  proposal.option_spread_pct_mid, limits.max_option_spread_pct_mid)
        else:
            check("option_spread_known", False, math.nan, limits.max_option_spread_pct_mid)

    check("already_moved", abs(proposal.already_moved_pct) <= limits.max_already_moved_pct,
          abs(proposal.already_moved_pct), limits.max_already_moved_pct)

    # The user budget covers gross stop price loss plus the full cost reserve.
    # Keep exact unit quantities; the multiplier is informational, not order rounding.
    quantities, fraction = _size(proposal)
    multiplier = float(fraction)
    gross_stop = Fraction(str(proposal.max_loss_usd)) * fraction
    costs = Fraction(str(proposal.est_costs_usd))
    total_risk = gross_stop + costs
    check("risk_per_trade_sizable", fraction > 0 and total_risk <= Fraction(str(proposal.risk_usd)),
          float(total_risk), proposal.risk_usd)

    measures = None
    try:
        measures = loss_measures(proposal, contract_book, now, limits.max_contract_metadata_age)
        check("instrument_valid", True, 1, 1)
        # Legacy field is an assertion only. Even overstatement can hide a stale
        # quantity/price and must be repaired, not silently carried into a ticket.
        loss_matches = money(proposal.worst_case_loss_usd) == measures.maximum_loss
        check("declared_max_loss_matches", loss_matches,
              proposal.worst_case_loss_usd, float(measures.maximum_loss))
        check("stop_within_strategy_loss", money(proposal.max_loss_usd) <= measures.maximum_loss,
              proposal.max_loss_usd, float(measures.maximum_loss))
    except (InstrumentError, ValidationError) as exc:
        check("instrument_valid", False, 0, 1)
        diagnostics.append(str(exc))

    # A conservative local funding estimate is distinct from a broker requirement.
    # Step 15 must obtain/revalidate actual account/strategy eligibility and margin.
    maximum = Fraction(measures.maximum_loss) * fraction if measures else None
    premium = Fraction(measures.net_premium) * fraction if measures else None
    funding = Fraction(measures.funding_estimate) * fraction + costs if measures else None
    required = float(funding) if funding is not None else 0.0
    check("buying_power", required <= account.buying_power, required, account.buying_power)
    check("margin_excess_positive", account.margin_excess > required, account.margin_excess, required)

    approved = all(c.passed for c in checks)
    return RiskDecision(
        proposal_id=proposal.proposal_id,
        approved=approved,
        final_qty_multiplier=multiplier if approved else 0.0,
        checks=checks,
        risk_budget_usd=proposal.risk_usd,
        final_leg_quantities=quantities if approved else [0] * len(quantities),
        estimated_stop_loss_usd=float(gross_stop) if approved else 0.0,
        cost_reserve_usd=float(costs) if approved else 0.0,
        estimated_total_risk_usd=float(total_risk) if approved else 0.0,
        computed_max_loss_usd=float(maximum) if approved else None,
        net_premium_usd=float(premium) if approved else None,
        estimated_funding_usd=required if approved else None,
        loss_basis=measures.basis if measures else None,
        contract_metadata_source=contract_book.source if measures and is_option else None,
        contract_metadata_as_of=contract_book.as_of if measures and is_option else None,
        diagnostics=diagnostics,
        buying_power_snapshot=account.buying_power,
        margin_excess_snapshot=account.margin_excess,
    )
