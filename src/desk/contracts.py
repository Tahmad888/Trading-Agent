"""Typed contracts between pipeline stages.

Every hand-off between stages is one of these validated objects, never free
text. Fields ending in ``_for_human`` are for Taz to read; code must never
parse them to make a decision.

Carried over from the paused ai-trading-desk code (order contracts only) and
changed for blueprint v2.3 step 2: no perps, a worst-case loss field, setup
and tier on every proposal, single long calls and puts, and an expiry and
open interest on each option leg. Blueprint v2.4: risk is a dollar amount per
trade (the analyst's grade suggests it, Taz sets it), plus the quote time and
trading status every order is checked against.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

Unit = Annotated[float, Field(ge=0, le=1)]

# Tiers (blueprint section 5): 0 = journal and paper only, 1 = may trade live
# (after 30+ paper trades positive after costs, and only when Taz says so).
# Size no longer depends on the tier; it's the dollar risk on each ticket.
Tier = Literal[0, 1]

# The chart analyst's grade for a plan. It suggests the ticket's dollar risk.
Grade = Literal["A", "B", "C"]


class Leg(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    qty: Annotated[int, Field(gt=0)]
    limit_price: Annotated[float, Field(gt=0)]   # limit orders only, ever
    expiry: date | None = None                   # options only; None for shares and spot
    open_interest: Annotated[int, Field(ge=0)] | None = None   # options only, at this strike


Structure = Literal["shares", "spot", "long_call", "long_put", "debit_vertical",
                    "credit_vertical", "iron_condor", "calendar"]

OPTION_STRUCTURES: frozenset[str] = frozenset(
    {"long_call", "long_put", "debit_vertical", "credit_vertical", "iron_condor", "calendar"})

# Structures that pay a net debit: their bought legs fall under the 14-day rule.
# The bought wings of credit spreads and condors are protection, not a purchase
# of premium, so they are exempt.
DEBIT_STRUCTURES: frozenset[str] = frozenset(
    {"long_call", "long_put", "debit_vertical", "calendar"})


# (buy legs, sell legs) each structure must have. Anything else, such as a
# "long_call" with a sold leg, would hide a naked short option.
LEG_SHAPES: dict[str, tuple[int, int]] = {
    "long_call": (1, 0), "long_put": (1, 0), "debit_vertical": (1, 1),
    "credit_vertical": (1, 1), "iron_condor": (2, 2), "calendar": (1, 1),
}


class TradeProposal(BaseModel):
    proposal_id: str
    plan_id: str | None = None      # the journal plan this came from
    setup_id: str                   # playbook setup, e.g. "1_trend_pullback", "2_breakout"
    tier: Tier                      # the setup's current tier: 0 paper, 1 live allowed
    grade: Grade | None = None      # the analyst's grade, which suggests risk_usd
    risk_usd: Annotated[float, Field(gt=0)]   # dollars at the stop Taz accepts for this trade
    instrument: str
    structure: Structure
    legs: list[Leg] = Field(min_length=1)
    max_loss_usd: Annotated[float, Field(gt=0)]         # loss at the stop, for the whole proposal
    # Structure's max loss: a bought option's whole premium, a spread's width
    # less credit, and for shares or spot the full position cost.
    worst_case_loss_usd: Annotated[float, Field(gt=0)]
    max_gain_usd: float | None = None
    est_costs_usd: float            # spread crossing + fees
    sector: str                     # sector bucket, for concentration limits
    option_spread_pct_mid: float | None = None   # widest leg bid-ask as % of mid
    already_moved_pct: float = 0.0
    quote_as_of: AwareDatetime      # when the quote this plan is priced on was taken; naive times are refused
    security_tradable: bool | None = None   # trading status from the quote source; None = unknown
    time_stop: datetime
    exit_rules: list[str] = []

    @model_validator(mode="after")
    def _loss_fields_consistent(self) -> TradeProposal:
        if self.worst_case_loss_usd < self.max_loss_usd:
            raise ValueError("worst_case_loss_usd cannot be below max_loss_usd")
        return self


class RuleCheck(BaseModel):
    rule: str
    passed: bool
    value: float
    limit: float


class RiskDecision(BaseModel):
    proposal_id: str
    approved: bool
    final_qty_multiplier: Unit      # risk layer can only shrink
    checks: list[RuleCheck]
    buying_power_snapshot: float
    margin_excess_snapshot: float


class ApprovalRecord(BaseModel):
    proposal_id: str
    decision: Literal["approve", "reject", "reduce"]
    decided_by: str
    decided_at: datetime
    order_args_sha256: str          # token bound to exact order arguments
    token_expires_at: datetime      # e.g. 120 s, single use
    reason: str
