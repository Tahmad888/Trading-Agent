"""Risk and sizing layer. Deterministic code only: no LLM ever touches this.

Limits are blueprint v2.4 section 10. Size is a dollar amount per trade: the
user selects a budget including estimated execution costs, without a fixed
per-trade ceiling. Grade is descriptive only. There is no cap on the number
of trades or positions; quality, not quantity, is the goal. The checks that remain stop a bad or untradable trade.

The layer can only shrink or reject a proposal. It never grows a size.
"""

from __future__ import annotations

import math
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from desk.contracts import (DEBIT_STRUCTURES, LEG_SHAPES, OPTION_STRUCTURES, RiskDecision,
                            RiskWarning, RuleCheck, TradeProposal)
from desk.instruments import ContractBook, InstrumentError, loss_measures, money
from desk.risk_terms import RiskTerms, RiskTermsSource, stop_distance
from desk.risk_context import AccountEvidence, Exposure, MarketContext, SetupRegistry, default_registry
from desk.playbook.cards import CARDS
from desk.playbook.filters import MarketSize

EXCHANGE_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class RiskLimits:
    daily_loss_usd: float = 200.0            # warning threshold, user-overridable policy
    weekly_loss_usd: float = 400.0           # [Assumption]
    kill_switch_drawdown_pct: float = 0.10   # from equity high-water mark
    max_option_spread_pct_mid: float = 0.10
    min_open_interest: int = 500             # at every leg's strike
    min_days_to_expiry_for_buys: int = 14    # no opening purchases closer to expiry
    max_already_moved_pct: float = 0.03      # fast move belongs to faster players
    max_account_state_age: timedelta = timedelta(seconds=30)
    max_quote_age: timedelta = timedelta(seconds=60)
    max_contract_metadata_age: timedelta = timedelta(hours=24)  # engineering assumption
    max_market_context_age: timedelta = timedelta(seconds=60)  # recompute from valid source bars


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
    account_id: str
    source: str
    pnl_day: date
    pnl_week_start: date
    pnl_basis: str
    exposures: tuple[Exposure, ...]          # explicit empty tuple only after reconciliation
    halted: bool = False                     # explicit manual stop, never automatic loss halt


def _size(proposal: TradeProposal, sizing_loss: Fraction | None) -> tuple[list[int], Fraction]:
    """Size exact whole strategy units, reserving the full estimated costs.

    Decimal strings avoid floating-point rounding at dollar/quantity boundaries.
    GCD, rather than the smallest leg, preserves every leg's integer ratio.
    """
    units = math.gcd(*(leg.qty for leg in proposal.legs))
    available = max(Fraction(0), Fraction(str(proposal.risk_usd)) - Fraction(str(proposal.est_costs_usd)))
    if proposal.sizing_mode == "selected_quantity":
        return [leg.qty for leg in proposal.legs], Fraction(1)
    affordable = (available * units) // sizing_loss if sizing_loss and sizing_loss > 0 else 0
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
    registry: SetupRegistry | None = None,
    market: MarketContext | None = None,
    terms_source: RiskTermsSource | None = None,
) -> RiskDecision:
    """Local eligibility only, not order authorization or verified broker margin.

    ``contract_book`` and ``terms_source`` are independently supplied adapters.
    Selected quantity is a reviewable choice, never order authorization.
    Stop estimates assume the disclosed exit fills; they do not cap actual losses.
    ``live=False`` sizes a paper trade using the same arithmetic.
    """
    now = now or datetime.now(timezone.utc)
    checks: list[RuleCheck] = []
    diagnostics: list[str] = []
    warnings: list[RiskWarning] = []

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

    try:
        account = AccountEvidence.model_validate(asdict(account))
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Risk clock must be timezone aware")
    except (ValidationError, ValueError) as exc:
        return RiskDecision(proposal_id=proposal.proposal_id, approved=False, final_qty_multiplier=0,
                            final_leg_quantities=[0] * len(proposal.legs),
                            checks=[RuleCheck(rule="account_state_valid", passed=False, value=0, limit=1)],
                            diagnostics=[str(exc)], buying_power_snapshot=0, margin_excess_snapshot=0)
    age = (now - account.as_of).total_seconds()
    check("account_state_fresh", 0 <= age <= limits.max_account_state_age.total_seconds(),
          age, limits.max_account_state_age.total_seconds())
    check("not_halted", not account.halted, float(account.halted), 0.0)
    today = now.astimezone(EXCHANGE_TZ).date()
    periods_ok = account.pnl_day == today and account.pnl_week_start == today - timedelta(days=today.weekday())
    check("account_periods_current", periods_ok, float(periods_ok), 1)
    try:
        registry = default_registry() if registry is None else SetupRegistry.model_validate(registry.model_dump(warnings=False))
        entry = next((e for e in registry.entries if e.setup_id == proposal.setup_id), None)
        known = entry is not None and proposal.setup_id in CARDS
        check("setup_known", known, float(known), 1)
        current = known and proposal.setup_version == entry.version == CARDS[proposal.setup_id].fingerprint()
        check("setup_version_current", current, float(current), 1)
        if live:
            allowed = known and entry.live_enabled and proposal.tier == 1
            check("tier_allows_live", allowed, float(allowed), 1)
    except ValidationError:
        check("setup_registry_valid", False, 0, 1)
    try:
        if market is None:
            raise ValueError("Market context is required")
        market = MarketContext.model_validate(market.model_dump(warnings=False))
        market_age = (now - market.as_of).total_seconds()
        check("market_context_fresh", 0 <= market_age <= limits.max_market_context_age.total_seconds(),
              market_age, limits.max_market_context_age.total_seconds())
    except (ValidationError, ValueError) as exc:
        market = None
        check("market_context_valid", False, 0, 1)
        diagnostics.append(str(exc))

    equity = account.equity
    drawdown = 1 - equity / account.equity_high_water_mark if account.equity_high_water_mark > 0 else 1.0
    drawdown = round(drawdown, 9)  # avoid float noise at the exact limit
    if drawdown >= limits.kill_switch_drawdown_pct:
        warnings.append(RiskWarning(code="account_drawdown", message="Account drawdown is at or above your warning threshold.",
                                    value=drawdown, threshold=limits.kill_switch_drawdown_pct))

    day_loss = max(0.0, -account.pnl_today)
    if day_loss >= limits.daily_loss_usd:
        warnings.append(RiskWarning(code="daily_loss", message="Today's net account loss is at or above your warning threshold.",
                                    value=day_loss, threshold=limits.daily_loss_usd))
    week_loss = max(0.0, -account.pnl_this_week)
    if week_loss >= limits.weekly_loss_usd:
        warnings.append(RiskWarning(code="weekly_loss", message="This week's net account loss is at or above your warning threshold.",
                                    value=week_loss, threshold=limits.weekly_loss_usd))

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

    measures = None
    try:
        measures = loss_measures(proposal, contract_book, now, limits.max_contract_metadata_age)
        check("instrument_valid", True, 1, 1)
        # Legacy field is an assertion only. Even overstatement can hide a stale
        # quantity/price and must be repaired, not silently carried into a ticket.
        loss_matches = money(proposal.worst_case_loss_usd) == measures.maximum_loss
        check("declared_max_loss_matches", loss_matches,
              proposal.worst_case_loss_usd, float(measures.maximum_loss))

    except (InstrumentError, ValidationError) as exc:
        check("instrument_valid", False, 0, 1)
        diagnostics.append(str(exc))

    terms = None
    requested_stop = None
    stop_basis = "unavailable: underlying stop does not determine an option exit price"
    try:
        if terms_source is None:
            raise ValueError("Independent signal terms source is required")
        terms = RiskTerms.model_validate(terms_source.resolve(proposal.event_id, now).model_dump())
        identity = (terms.event_id == proposal.event_id and terms.symbol == proposal.instrument
                    and terms.setup_id == proposal.setup_id and terms.setup_version == proposal.setup_version
                    and bool(terms.event_digest))
        check("signal_identity", identity, float(identity), 1)
        if terms.quote_provenance is not None:
            quote_identity = (terms.quote_provenance.source == proposal.quote_source
                              and terms.quote_provenance.symbol == terms.symbol)
            check("quote_source_matches", quote_identity, float(quote_identity), 1)
        age = (now - terms.checked_at).total_seconds()
        quote_age = (now - terms.quote_at).total_seconds()
        fresh = (0 <= age <= limits.max_quote_age.total_seconds() and now < terms.valid_until
                 and 0 <= quote_age <= limits.max_quote_age.total_seconds())
        check("signal_terms_fresh", fresh, age, limits.max_quote_age.total_seconds())
        matches = money(proposal.stop_price) == money(terms.stop)
        check("stop_matches_event", matches, proposal.stop_price, terms.stop)
        target_matches = proposal.target_price == terms.target
        check("target_matches_event", target_matches, float(target_matches), 1)
        stop_distance(terms.entry_level, terms.stop, terms.direction)
        stop_distance(terms.underlying_price, terms.stop, terms.direction)
        entry = proposal.legs[0].limit_price if not is_option else terms.underlying_price
        direction = 1 if terms.direction == "long" else -1
        moved = (entry / terms.chase_reference - 1) * direction
        chase = min(limits.max_already_moved_pct, CARDS[proposal.setup_id].params.get("max_chase").value
                    if "max_chase" in CARDS[proposal.setup_id].params else float("inf"))
        check("executable_entry", (entry - terms.entry_level) * direction >= 0 and moved <= chase + 1e-12, moved, chase)
        quote_moved = (terms.underlying_price / terms.chase_reference - 1) * direction
        quote_ok = (terms.underlying_price - terms.entry_level) * direction >= 0 and quote_moved <= chase + 1e-12
        check("underlying_entry_current", quote_ok, quote_moved, chase)
        if terms.target is not None:
            target_ok = (terms.target - entry) * direction > 0
            check("target_beyond_entry", target_ok, terms.target, entry)
        distance = stop_distance(entry, terms.stop, terms.direction)
        if terms.max_stop_fraction is not None:
            width = float(distance / Fraction(str(entry)))
            check("structural_stop_width", width <= terms.max_stop_fraction + 1e-12, width, terms.max_stop_fraction)
        if measures:
            consistent = measures.direction in {terms.direction, "neutral"}
            check("instrument_signal_direction", consistent, float(consistent), 1)
        if not is_option:
            requested_stop = distance * proposal.legs[0].qty
            stop_basis = "conditional share fill at event stop; gaps/slippage/nonexecution excluded"
            check("share_direction", terms.direction == "long", float(terms.direction == "long"), 1)
        elif terms.option_exit_prices and terms.option_exit_source and measures:
            from desk.instruments import canonical_symbol
            exits = {canonical_symbol(k): v for k, v in terms.option_exit_prices.items()}
            if len(exits) != len(terms.option_exit_prices):
                raise ValueError("Duplicate option exit identity")
            # Standard 100-share deliverables were independently validated above.
            requested_stop = sum((Fraction(str(leg.limit_price)) - Fraction(str(exits[canonical_symbol(leg.symbol)])))
                                 * (1 if leg.side == "buy" else -1) * leg.qty * 100 for leg in proposal.legs) if all(canonical_symbol(l.symbol) in exits for l in proposal.legs) else None
            if requested_stop is not None and requested_stop <= 0:
                raise ValueError("Conditional option exit must describe a loss")
            if requested_stop is not None:
                stop_basis = "conditional option-price fills from " + terms.option_exit_source + "; not a prediction at the stock stop"
        if requested_stop is not None and measures:
            check("stop_within_strategy_loss", requested_stop <= Fraction(measures.maximum_loss),
                  float(requested_stop), float(measures.maximum_loss))
        if proposal.max_loss_usd is not None:
            matches = requested_stop is not None and Fraction(str(proposal.max_loss_usd)) == requested_stop
            check("declared_stop_loss_matches", matches, proposal.max_loss_usd,
                  float(requested_stop) if requested_stop is not None else 0)
        check("signal_terms_valid", True, 1, 1)
    except Exception as exc:
        # Provider outages/malformed evidence are isolated; never expose raw provider responses.
        check("signal_terms_valid", False, 0, 1)
        diagnostics.append("Independent signal/exit terms unavailable or invalid (" + type(exc).__name__ + ")")

    costs = Fraction(str(proposal.est_costs_usd))
    sizing_loss = (Fraction(measures.maximum_loss) if measures else None) if proposal.sizing_mode == "maximum_loss_budget" else requested_stop
    quantities, fraction = _size(proposal, sizing_loss)
    multiplier = float(fraction)
    gross_stop = requested_stop * fraction if requested_stop is not None else None
    total_risk = gross_stop + costs if gross_stop is not None else None
    budget = Fraction(str(proposal.risk_usd))
    sizing_total = sizing_loss * fraction + costs if sizing_loss is not None else None
    sizable = fraction > 0 and (proposal.sizing_mode == "selected_quantity" or
                                sizing_total is not None and sizing_total <= budget)
    check("risk_per_trade_sizable", sizable, float(sizing_total) if sizing_total is not None else 0, proposal.risk_usd)
    # A long share position's full exposure is its position value, which always
    # exceeds a stop budget; show it as information (position_value_usd) instead.
    if is_option and measures and Fraction(measures.maximum_loss) * fraction + costs > budget:
        warnings.append(RiskWarning(code="exposure_above_budget", message="Full strategy exposure plus costs exceeds the entered budget; confirm the exact quantity and exposure on the ticket.",
                                    value=float(Fraction(measures.maximum_loss) * fraction + costs), threshold=proposal.risk_usd))
    if proposal.sizing_mode == "selected_quantity" and total_risk is not None and total_risk > budget:
        warnings.append(RiskWarning(code="stop_estimate_above_budget", message="Selected quantity has a conditional stop-loss estimate plus costs above the entered budget.", value=float(total_risk), threshold=proposal.risk_usd))

    if market is not None and market.regime is MarketSize.HALF:
        warnings.append(RiskWarning(code="mixed_market", message=market.reason + "; your budget is unchanged.", value=1, threshold=0))
    if market is not None and market.regime is MarketSize.NO_NEW_LONGS and measures and measures.direction != "short":
        warnings.append(RiskWarning(code="bearish_market", message=market.reason + "; review this long or neutral exposure.", value=1, threshold=0))

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
        estimated_stop_loss_usd=float(gross_stop) if approved and gross_stop is not None else None,
        cost_reserve_usd=float(costs) if approved else 0.0,
        estimated_total_risk_usd=float(total_risk) if approved and total_risk is not None else None,
        computed_max_loss_usd=float(maximum) if approved else None,
        net_premium_usd=float(premium) if approved else None,
        estimated_funding_usd=required if approved else None,
        requested_leg_quantities=[leg.qty for leg in proposal.legs],
        requested_max_loss_usd=float(measures.maximum_loss) if measures else None,
        requested_stop_loss_usd=float(requested_stop) if requested_stop is not None else None,
        resolved_stop_price=terms.stop if terms else None,
        resolved_target_price=terms.target if terms else None,
        position_value_usd=float(money(proposal.legs[0].limit_price) * quantities[0]) if approved and not is_option else None,
        stop_loss_basis=stop_basis,
        signal_terms_valid_until=min(terms.valid_until, terms.checked_at + limits.max_quote_age,
                                     terms.quote_at + limits.max_quote_age) if terms else None,
        signal_event_valid_until=terms.valid_until if terms else None,
        terms_sha256=hashlib.sha256(json.dumps({"proposal": proposal.model_dump(mode="json"),
            "terms": terms.model_dump(mode="json"), "quantities": quantities}, sort_keys=True).encode()).hexdigest() if approved and terms else None,
        loss_basis=measures.basis if measures else None,
        contract_metadata_source=contract_book.source if measures and is_option else None,
        contract_metadata_as_of=contract_book.as_of if measures and is_option else None,
        market_context_source=market.source if market else None,
        market_context_as_of=market.as_of if market else None,
        market_regime=market.regime.value if market else None,
        diagnostics=diagnostics,
        warnings=warnings,
        warning_acknowledgement_required=bool(warnings),
        exposure_summary=[e.model_dump(mode="json") for e in account.exposures],
        buying_power_snapshot=account.buying_power,
        margin_excess_snapshot=account.margin_excess,
    )
