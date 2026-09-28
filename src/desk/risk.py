"""Risk and sizing layer. Deterministic code only: no LLM ever touches this.

Limits are blueprint v2.3 section 10's starting values for a small account.
They are engineering choices, not research findings, and may only be
tightened without Taz's explicit sign-off.

The layer can only shrink or reject a proposal. It never grows a size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from desk.contracts import (DEBIT_STRUCTURES, LEG_SHAPES, OPTION_STRUCTURES, RiskDecision,
                            RuleCheck, TradeProposal)

EXCHANGE_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class RiskLimits:
    # Loss at the stop per trade, as a fraction of equity, by size tier.
    # Tier 0 is paper only and is sized like tier 1, so paper results show
    # what a first live trade would have done.
    tier1_risk_pct: float = 0.0025
    tier2_risk_pct: float = 0.005
    max_worst_case_pct: float = 0.02         # per option position; a bought option's whole premium counts
    max_total_open_risk_pct: float = 0.03    # all open positions at their stops, plus this one
    daily_loss_pct: float = 0.015            # halts new entries until manual reset
    weekly_loss_pct: float = 0.04
    max_open_positions: int = 5
    max_per_sector: int = 2
    max_option_spread_pct_mid: float = 0.10
    min_open_interest: int = 500             # at every leg's strike
    min_days_to_expiry_for_buys: int = 14    # no opening purchases closer to expiry
    max_already_moved_pct: float = 0.03      # fast move belongs to faster players
    kill_switch_drawdown_pct: float = 0.10   # from equity high-water mark
    max_account_state_age: timedelta = timedelta(seconds=30)

    def risk_pct_for_tier(self, tier: int) -> float:
        return self.tier2_risk_pct if tier == 2 else self.tier1_risk_pct


@dataclass(frozen=True)
class OpenPosition:
    instrument: str
    sector: str
    risk_usd: float                          # loss if it hits its stop now


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
    open_positions: list[OpenPosition] = field(default_factory=list)
    halted: bool = False                     # set by loss limits; only Taz resets it


def _lots(min_leg_qty: int, fraction: float) -> float:
    """Largest multiplier <= fraction that keeps every leg a whole contract or share."""
    return math.floor(min_leg_qty * min(1.0, fraction)) / min_leg_qty


def evaluate(
    proposal: TradeProposal,
    account: AccountState,
    limits: RiskLimits = RiskLimits(),
    now: datetime | None = None,
    live: bool = True,
) -> RiskDecision:
    """Check a proposal against every limit. ``live=False`` sizes a paper trade."""
    now = now or datetime.now(timezone.utc)
    checks: list[RuleCheck] = []

    def check(rule: str, passed: bool, value: float, limit: float) -> None:
        checks.append(RuleCheck(rule=rule, passed=passed, value=value, limit=limit))

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

    day_loss = max(0.0, -account.pnl_today) / equity if equity > 0 else 1.0
    check("daily_loss_limit", day_loss < limits.daily_loss_pct, day_loss, limits.daily_loss_pct)
    week_loss = max(0.0, -account.pnl_this_week) / equity if equity > 0 else 1.0
    check("weekly_loss_limit", week_loss < limits.weekly_loss_pct, week_loss, limits.weekly_loss_pct)

    n_open = len(account.open_positions)
    check("max_open_positions", n_open + 1 <= limits.max_open_positions,
          n_open + 1, limits.max_open_positions)
    n_sector = sum(1 for p in account.open_positions if p.sector == proposal.sector)
    check("max_per_sector", n_sector + 1 <= limits.max_per_sector,
          n_sector + 1, limits.max_per_sector)

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

    # Size: shrink so the loss at the stop fits the tier's budget and, for
    # options, the worst case fits the 2% cap. Never grow.
    min_leg_qty = min(leg.qty for leg in proposal.legs)
    budget = limits.risk_pct_for_tier(proposal.tier) * equity
    multiplier = _lots(min_leg_qty, budget / proposal.max_loss_usd) if budget > 0 else 0.0
    check("risk_per_trade_sizable", multiplier > 0, proposal.max_loss_usd * multiplier, budget)
    if is_option:
        cap = limits.max_worst_case_pct * equity
        wc_multiplier = _lots(min_leg_qty, cap / proposal.worst_case_loss_usd) if cap > 0 else 0.0
        check("worst_case_cap", wc_multiplier > 0, proposal.worst_case_loss_usd * wc_multiplier, cap)
        multiplier = min(multiplier, wc_multiplier)

    open_risk = sum(p.risk_usd for p in account.open_positions) + proposal.max_loss_usd * multiplier
    open_risk_cap = limits.max_total_open_risk_pct * equity
    check("total_open_risk", open_risk <= open_risk_cap, open_risk, open_risk_cap)

    # The worst case is what the broker holds: premium, spread margin or share cost.
    required = proposal.worst_case_loss_usd * multiplier + proposal.est_costs_usd
    check("buying_power", required <= account.buying_power, required, account.buying_power)
    check("margin_excess_positive", account.margin_excess > required, account.margin_excess, required)

    approved = all(c.passed for c in checks)
    return RiskDecision(
        proposal_id=proposal.proposal_id,
        approved=approved,
        final_qty_multiplier=multiplier if approved else 0.0,
        checks=checks,
        buying_power_snapshot=account.buying_power,
        margin_excess_snapshot=account.margin_excess,
    )
