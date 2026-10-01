"""Validated operator/broker inputs, supplied separately from analyst proposals."""
from datetime import date, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from desk.playbook.cards import CARDS
from desk.playbook.filters import MarketSize

Text = Annotated[str, Field(min_length=1)]
Finite = Annotated[float, Field(allow_inf_nan=False)]


class SetupEligibility(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    setup_id: Text
    version: Text
    live_enabled: bool = False


class SetupRegistry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entries: tuple[SetupEligibility, ...]

    @model_validator(mode="after")
    def unique(self):
        if len({e.setup_id for e in self.entries}) != len(self.entries):
            raise ValueError("Duplicate setup eligibility")
        return self


def default_registry() -> SetupRegistry:
    # Paper until the existing observation requirements and explicit user promotion.
    return SetupRegistry(entries=tuple(SetupEligibility(setup_id=c.id, version=c.fingerprint())
                                       for c in CARDS.values()))


class MarketContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    regime: MarketSize
    source: Text
    as_of: AwareDatetime  # time regime was evaluated, NOT the prior daily bar's timestamp
    reason: Text


class Exposure(BaseModel):
    """Authoritative open/pending position summary; never inferred from proposals."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    reference: Text
    instrument: Text
    state: Literal["open", "pending"]
    quantity: Annotated[int, Field(gt=0, strict=True)]
    unit: Literal["share", "strategy"]
    direction: Literal["long", "short", "neutral"]
    market_value_usd: Finite
    estimated_stop_risk_usd: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    sector: Text


class AccountEvidence(BaseModel):
    """Validated representation used for the dataclass API and durable storage.

    P&L is net liquidation change including realized/unrealized and fees, excluding
    external cash flows. The adapter must supply correct ET day/Monday baselines.
    This model does not invent baselines or convert realized-only broker P&L.
    """
    model_config = ConfigDict(extra="forbid")
    account_id: Text
    source: Text
    as_of: AwareDatetime
    pnl_day: date
    pnl_week_start: date
    pnl_basis: Literal["net_liquidation_ex_cashflows"]
    equity: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    equity_high_water_mark: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    buying_power: Finite
    margin_excess: Finite
    pnl_today: Finite
    pnl_this_week: Finite
    halted: bool = False  # explicit manual stop only, never set by a loss warning
    exposures: tuple[Exposure, ...]

    @model_validator(mode="after")
    def consistent(self):
        if self.equity_high_water_mark < self.equity:
            raise ValueError("High-water mark cannot be below current equity")
        if self.pnl_week_start.weekday() != 0 or not (
            self.pnl_week_start <= self.pnl_day < self.pnl_week_start + timedelta(days=7)
        ):
            raise ValueError("Incorrect Monday-based P&L week")
        if len({e.reference for e in self.exposures}) != len(self.exposures):
            raise ValueError("Duplicate exposure reference")
        return self
