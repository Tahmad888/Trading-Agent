"""Evidence contracts, not a corporate-action feed or an adjustment calculator.

Only trusted data producers may attest coverage/definitions. A matching user-made
label is not proof. Unknown data remains readable but cannot qualify a decision.
"""
from datetime import date
import math
from typing import Annotated, Literal

import pandas as pd
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from desk.bars import BarDataError
from desk.calendar import ET, clock

Text = Annotated[str, Field(min_length=1)]


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: Text
    evidence_ref: Text


class CorporateAction(Evidence):
    event_id: Text
    revision: Text
    kind: Literal["split", "cash_dividend", "unsupported"]
    effective_session: date


class PriceBasis(Evidence):
    symbol: Text
    security_id: Text
    currency: Text
    coverage_start: date
    basis_session: date
    verified_at: AwareDatetime
    coverage_complete: Literal[True]  # explicit; no default/no-action inference
    normalization: Literal["unadjusted", "split_adjusted", "split_dividend_adjusted"]
    actions: tuple[CorporateAction, ...]  # explicit empty tuple means an attested empty ledger

    @field_validator("coverage_complete", mode="before")
    @classmethod
    def explicit_coverage(cls, value):
        if value is not True:
            raise ValueError("Coverage must be explicitly verified complete")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.coverage_start > self.basis_session or self.basis_session > clock(self.verified_at).date():
            raise ValueError("Corporate-action coverage dates are inconsistent")
        if len({a.event_id for a in self.actions}) != len(self.actions):
            raise ValueError("Duplicate corporate-action identity")
        if any(not self.coverage_start <= a.effective_session <= self.basis_session for a in self.actions):
            raise ValueError("Action outside attested coverage")
        return self



class PriceHistoryChanged(BarDataError):
    """The history used to arm a signal changed; a fresh detection is required."""


class VendorPriceBasis(Evidence):
    """Observed vendor consistency, explicitly NOT complete corporate-action coverage."""
    method: Literal["webull-history-v1"]
    host: Literal["api.sandbox.webull.com", "api.webull.com"]
    symbol: Text
    security_id: Text
    currency: Literal["USD"]
    coverage_start: date
    basis_session: date
    verified_at: AwareDatetime
    normalization: Literal["unadjusted", "split_dividend_adjusted"]
    # Completed daily reference: session ISO date, O/H/L/C/V. Retained with signal.
    daily_history: tuple[tuple[str, float, float, float, float, float], ...]
    anchor_session: date
    daily_anchor_close: float = Field(gt=0, allow_inf_nan=False)
    raw_anchor_close: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def consistent(self):
        rows = self.daily_history
        if not rows or len(rows) > 1000:
            raise ValueError("Expected bounded completed daily history")
        dates = [date.fromisoformat(r[0]) for r in rows]
        if dates != sorted(set(dates)) or dates[0] != self.coverage_start or dates[-1] != self.anchor_session:
            raise ValueError("Invalid daily reference interval")
        if not self.anchor_session <= self.basis_session <= clock(self.verified_at).date():
            raise ValueError("Vendor evidence dates disagree")
        for _, o, h, l, c, v in rows:
            if any(not math.isfinite(x) for x in (o,h,l,c,v)) or not 0 < l <= min(o,c) <= max(o,c) <= h or v < 0:
                raise ValueError("Invalid history observation")
        if rows[-1][4] != self.daily_anchor_close or abs(self.daily_anchor_close-self.raw_anchor_close) > 0.000001:
            raise ValueError("Daily/raw close pair disagrees")
        return self


PriceEvidence = PriceBasis | VendorPriceBasis

def _price(raw, now) -> PriceEvidence:
    try:
        basis = (VendorPriceBasis if isinstance(raw, dict) and raw.get("method") == "webull-history-v1"
                 else PriceBasis).model_validate(raw)
    except ValidationError as exc:
        raise BarDataError("Unknown/incomplete corporate-action price basis; rebuild with verified coverage") from exc
    if clock(basis.verified_at) > clock(now):
        raise BarDataError("Corporate-action evidence is later than the decision clock")
    if isinstance(basis, PriceBasis) and any(a.kind == "unsupported" for a in basis.actions):
        raise BarDataError("Unsupported corporate action; rebuild from verified normalized data")
    return basis


def price_basis(df: pd.DataFrame, now, *, symbol: str | None = None) -> PriceEvidence:
    meta = df.attrs.get("bar_provenance", {})
    basis = _price(meta.get("price_basis"), now)
    if symbol is not None and basis.symbol != symbol:
        raise BarDataError("Price basis belongs to a different symbol/security")
    if meta.get("adjustment") != basis.normalization:
        raise BarDataError("Bar adjustment and attested price normalization disagree")
    if df.empty:
        return basis
    first, last = df.index.tz_convert(ET).date[[0, -1]]
    if basis.coverage_start > first or basis.basis_session < last:
        raise BarDataError("Corporate-action coverage does not cover the supplied price history")
    if isinstance(basis, VendorPriceBasis):
        if basis.normalization == "unadjusted" and any(
                d not in {basis.anchor_session, basis.basis_session} for d in df.index.tz_convert(ET).date):
            raise BarDataError("Raw history outside verified vendor anchor/current session")
        return basis
    # Native raw history cannot cross an action or remain on yesterday's share basis.
    # A split-only series cannot bridge a dividend when comparing adjusted levels.
    unapplied = [a for a in basis.actions if first < a.effective_session <= basis.basis_session
                 and (basis.normalization == "unadjusted" or
                      basis.normalization == "split_adjusted" and a.kind == "cash_dividend")]
    if unapplied:
        raise BarDataError("Price history crosses an unapplied corporate action; rebuild normalized history")
    return basis


def compatible_prices(previous: dict | None, current: pd.DataFrame, now, *, symbol=None):
    old, new = _price(previous, now), price_basis(current, now, symbol=symbol)
    if (old.symbol, old.security_id, old.currency) != (new.symbol, new.security_id, new.currency):
        raise PriceHistoryChanged("Corporate-action price basis security/currency mismatch; rebuild signal")
    if isinstance(old, VendorPriceBasis) != isinstance(new, VendorPriceBasis):
        raise PriceHistoryChanged("Price evidence method changed; rebuild signal")
    if new.basis_session != clock(now).date() or new.coverage_start > old.coverage_start or new.basis_session < old.basis_session:
        raise BarDataError("Current corporate-action coverage cannot revalidate the signal")

    if isinstance(old, VendorPriceBasis) or isinstance(new, VendorPriceBasis):
        if not isinstance(old, VendorPriceBasis) or not isinstance(new, VendorPriceBasis) or old.host != new.host:
            raise PriceHistoryChanged("Price evidence method/host changed; rebuild signal")
        latest = {row[0]: row[1:] for row in new.daily_history}
        if any(row[0] not in latest for row in old.daily_history):
            raise BarDataError("Missing original history overlap; cannot revalidate signal")
        if any(row[1:] != latest[row[0]] for row in old.daily_history):
            raise PriceHistoryChanged("Daily OHLCV used to arm this signal was revised; rebuild signal")
        return new

    def events(basis):
        return sorted((a.event_id, a.revision, a.kind, a.effective_session)
                      for a in basis.actions if a.effective_session >= old.coverage_start)

    if events(old) != events(new):
        raise BarDataError("Changed corporate-action price basis; rebuild the signal")
    return new


class VolumeBasis(Evidence):
    channel: Text
    definition_id: Text | None  # sale/session inclusion definition, not simply 'volume'
    units: Literal["shares"]
    share_basis_id: Text | None  # separately attested share adjustment; never inferred from prices
    symbol: Text | None = None
    security_id: Text | None = None
    valid_from: date | None = None
    valid_through: date | None = None
    comparison_policy: Literal["webull-rth30/native-daily50-v1"] | None = None

    @model_validator(mode="after")
    def bounded_identity(self):
        fields = (self.symbol, self.security_id, self.valid_from, self.valid_through)
        if any(v is not None for v in fields) and not all(v is not None for v in fields):
            raise ValueError("Bounded volume evidence requires identity and both dates")
        if self.valid_from is not None and self.valid_from > self.valid_through:
            raise ValueError("Invalid volume evidence interval")
        if self.comparison_policy and self.symbol is None:
            raise ValueError("Mixed-channel comparison requires bounded instrument evidence")
        return self


def volume_basis(df: pd.DataFrame, *, allow_developing=False) -> VolumeBasis:
    try:
        basis = VolumeBasis.model_validate(df.attrs.get("volume_basis"))
    except ValidationError as exc:
        raise BarDataError("Unknown volume definition/share basis") from exc
    if not basis.definition_id or not basis.share_basis_id:
        raise BarDataError("Unverified volume definition/share basis")
    if df.attrs.get("developing_as_of") and not allow_developing:
        raise BarDataError("Developing intraday volume is not a completed daily-volume observation")
    if basis.valid_from is not None:
        if df.empty or not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
            raise BarDataError("Volume calculation needs dated observations")
        dates = df.index.tz_convert(ET).date
        if min(dates) < basis.valid_from or max(dates) > basis.valid_through:
            raise BarDataError("Volume window crosses unverified split share units or evidence dates")
        identity = df.attrs.get("provider_identity")
        if identity is not None and identity != {"symbol": basis.symbol, "instrument_id": basis.security_id}:
            raise BarDataError("Volume evidence belongs to another instrument")
    return basis


def compatible_volume(daily: pd.DataFrame, early: dict | None):
    baseline = volume_basis(daily)
    try:
        observed = VolumeBasis.model_validate(early)
    except ValidationError as exc:
        raise BarDataError("Unknown early-volume definition/share basis") from exc
    if not observed.definition_id or not observed.share_basis_id or (
        baseline.units, baseline.share_basis_id
    ) != (observed.units, observed.share_basis_id):
        raise BarDataError("Daily and intraday volume share bases are not verified compatible")
    if (baseline.symbol, baseline.security_id) != (observed.symbol, observed.security_id):
        raise BarDataError("Daily and intraday volume instrument identities differ")
    if baseline.valid_through is not None and baseline.valid_through != observed.valid_through:
        raise BarDataError("Daily and intraday volume evidence dates differ")
    if baseline.definition_id == observed.definition_id:
        return
    # An explicit directional comparison is not a claim of identical trade coverage.
    if not (baseline.comparison_policy == observed.comparison_policy == "webull-rth30/native-daily50-v1"
            and baseline.channel == "webull:native:D" and observed.channel == "webull:minute:RTH"
            and baseline.definition_id == "webull:native:D:provider-reported"
            and observed.definition_id == "webull:minute:RTH:provider-reported"):
        raise BarDataError("Daily and intraday volume definitions lack an accepted comparison policy")
