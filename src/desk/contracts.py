"""Typed contracts between pipeline stages.

Every hand-off between stages is one of these validated objects, never free
text. Fields ending in ``_for_human`` are for Taz to read; code must never
parse them to make a decision.

Carried over from the paused ai-trading-desk code (order contracts only) and
changed for blueprint v2.3 step 2: no perps, a worst-case loss field, setup
and tier on every proposal, single long calls and puts, and an expiry and
open interest on each option leg. Blueprint v2.4: risk is a dollar amount per
trade (Taz selects it; grade is descriptive only), plus the quote time and
trading status every order is checked against.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Unit = Annotated[float, Field(ge=0, le=1)]

# Tiers (blueprint section 5): 0 = journal and paper only, 1 = may trade live
# (after 30+ paper trades positive after costs, and only when Taz says so).
# Size no longer depends on the tier; it's the dollar risk on each ticket.
Tier = Literal[0, 1]

# The analyst grade describes quality. It does not assign or change dollar risk.
Grade = Literal["A", "B", "C"]


class Leg(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    quantity_unit: Literal["share", "contract"]
    qty: Annotated[int, Field(gt=0, strict=True)]
    limit_price: Annotated[float, Field(gt=0, allow_inf_nan=False)]   # limit orders only, ever
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
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[3] = 3
    event_id: Annotated[str, Field(min_length=1)]
    stop_price: Annotated[float, Field(gt=0, allow_inf_nan=False)]  # assertion checked against event
    target_price: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    sizing_mode: Literal["stop_budget", "maximum_loss_budget", "selected_quantity"]
    proposal_id: str
    plan_id: str | None = None      # the journal plan this came from
    setup_id: str                   # playbook setup, e.g. "1_trend_pullback", "2_breakout"
    setup_version: Annotated[str, Field(min_length=1)]
    quote_source: Annotated[str, Field(min_length=1)]
    stop_estimate_source: Annotated[str, Field(min_length=1)]
    tier: Tier                      # the setup's current tier: 0 paper, 1 live allowed
    grade: Grade | None = None      # descriptive only; never determines risk_usd
    risk_usd: Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]  # Taz's budget, including costs
    instrument: str
    structure: Structure
    legs: list[Leg] = Field(min_length=1)
    max_loss_usd: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None  # optional assertion; NEVER sizing authority
    # Caller assertion only: instruments.py independently verifies this amount.
    # Gross intact-strategy loss excludes costs and assignment/exit mishandling.
    worst_case_loss_usd: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    max_gain_usd: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    est_costs_usd: Annotated[float, Field(ge=0, allow_inf_nan=False)]  # full round-trip cost reserve
    sector: str                     # sector bucket, for concentration limits
    option_spread_pct_mid: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    already_moved_pct: Annotated[float, Field(allow_inf_nan=False)] = 0.0
    quote_as_of: AwareDatetime      # when the quote this plan is priced on was taken; naive times are refused
    security_tradable: bool | None = None   # trading status from the quote source; None = unknown
    time_stop: AwareDatetime
    exit_rules: list[str] = []

    @model_validator(mode="after")
    def _loss_fields_consistent(self) -> TradeProposal:
        if self.max_loss_usd is not None and self.worst_case_loss_usd < self.max_loss_usd:
            raise ValueError("worst_case_loss_usd cannot be below max_loss_usd")
        return self


class RuleCheck(BaseModel):
    rule: str
    passed: bool
    value: float
    limit: float


class RiskWarning(BaseModel):
    code: str
    message: str
    value: float
    threshold: float


class RiskDecision(BaseModel):
    proposal_id: str
    approved: bool
    final_qty_multiplier: Unit      # risk layer can only shrink
    checks: list[RuleCheck]
    risk_budget_usd: float | None = None
    final_leg_quantities: list[int] = Field(default_factory=list)
    estimated_stop_loss_usd: float | None = None  # gross price loss for approved quantity
    cost_reserve_usd: float = 0.0
    estimated_total_risk_usd: float | None = None  # gross stop loss + reserved costs
    computed_max_loss_usd: float | None = None  # gross; excludes fees/assignment mishandling
    net_premium_usd: float | None = None  # positive = debit, negative = credit
    estimated_funding_usd: float | None = None  # conservative local estimate, NOT broker margin
    requested_leg_quantities: list[int] = Field(default_factory=list)
    requested_max_loss_usd: float | None = None
    requested_stop_loss_usd: float | None = None
    resolved_stop_price: float | None = None
    resolved_target_price: float | None = None
    position_value_usd: float | None = None  # shares: final quantity x entry limit; information, not a warning
    stop_loss_basis: str = "unavailable"
    signal_terms_valid_until: AwareDatetime | None = None
    signal_event_valid_until: AwareDatetime | None = None  # the event's own validity, not receipt freshness
    terms_sha256: str | None = None  # includes receipt times; NOT an approval binding (see desk.tickets)
    order_authorized: Literal[False] = False
    broker_buying_power_required_usd: float | None = None
    broker_requirement_verified: bool = False
    loss_basis: str | None = None
    contract_metadata_source: str | None = None
    contract_metadata_as_of: AwareDatetime | None = None
    market_context_source: str | None = None
    market_context_as_of: AwareDatetime | None = None
    market_regime: str | None = None
    diagnostics: list[str] = Field(default_factory=list)
    warnings: list[RiskWarning] = Field(default_factory=list)
    warning_acknowledgement_required: bool = False
    exposure_summary: list[dict] = Field(default_factory=list)
    buying_power_snapshot: float
    margin_excess_snapshot: float


class ApprovalRecord(BaseModel):
    """Immutable G4 approval snapshot for one ticket version (desk.tickets).

    Schema 1 (decision/order_args_sha256/token_expires_at) never had a producer and
    is rejected: a record without the binding snapshot can never be consumed.
    This records a human decision on local terms; it never authorizes a broker order.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[2]
    approval_id: Annotated[str, Field(min_length=1)]
    ticket_id: Annotated[str, Field(min_length=1)]
    ticket_version: Annotated[int, Field(gt=0, strict=True)]
    decision: Literal["approve"]
    actor: Annotated[str, Field(min_length=1)]           # audit label; a local terminal is not authentication
    channel: Literal["terminal", "automated_fixture"]
    terminal_user: str | None = None
    decided_at: AwareDatetime
    expires_at: AwareDatetime
    budget_confirmed_usd: Annotated[str, Field(min_length=1)]   # the separately typed amount, exact cents
    acknowledgements: tuple[str, ...]
    snapshot: dict                                       # the ticket version's full binding
    snapshot_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    order_authorized: Literal[False] = False
