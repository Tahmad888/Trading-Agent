"""Risk and sizing layer. Deterministic code only: no LLM ever touches this.

Limits are blueprint v2.4 section 10. Size is a dollar amount per trade: the
analyst's grade suggests it and Taz sets it on the ticket, up to a hard
ceiling. There is no cap on the number of trades or positions; quality, not
quantity, is the goal. The checks that remain stop a bad or untradable trade.

The layer can only shrink or reject a proposal. It never grows a size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from desk.contracts import (DEBIT_STRUCTURES, LEG_SHAPES, OPTION_STRUCTURES, Grade, RiskDecision,
                            RuleCheck, TradeProposal)

EXCHANGE_TZ = ZoneInfo("America/New_York")

# The dollar risk a ticket suggests for each analyst grade. Taz can set any
# amount up to the ceiling when he approves; these are only starting points.
GRADE_RISK_USD: dict[str, float] = {"A": 100.0, "B": 50.0, "C": 20.0}


def suggested_risk_usd(grade: Grade) -> float:
    return GRADE_RISK_USD[grade]


@dataclass(frozen=True)
class RiskLimits:
    max_risk_per_trade_usd: float = 100.0    # hard ceiling on loss at the stop, whatever the ticket says
    daily_loss_usd: float = 200.0            # halts new entries until manual reset [Assumption]
    weekly_loss_usd: float = 400.0           # [Assumption]
    kill_switch_drawdown_pct: float = 0.10   # from equity high-water mark
    max_option_spread_pct_mid: float = 0.10
    min_open_interest: int = 500             # at every leg's strike
    min_days_to_expiry_for_buys: int = 14    # no opening purchases closer to expiry
    max_already_moved_pct: float = 0.03      # fast move belongs to faster players
    max_account_state_age: timedelta = timedelta(seconds=30)
    max_quote_age: timedelta = timedelta(seconds=60)


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

    # Size: shrink so the loss at the stop fits the ticket's dollar risk,
    # never above the hard ceiling. Never grow. Paper trades are sized the
    # same way, so the journal shows what the live trade would have done.
    min_leg_qty = min(leg.qty for leg in proposal.legs)
    budget = min(proposal.risk_usd, limits.max_risk_per_trade_usd)
    multiplier = _lots(min_leg_qty, budget / proposal.max_loss_usd)
    check("risk_per_trade_sizable", multiplier > 0, proposal.max_loss_usd * multiplier, budget)

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
