"""Evidence contracts, not a corporate-action feed or an adjustment calculator.

Only trusted data producers may attest coverage/definitions. A matching user-made
label is not proof. Unknown data remains readable but cannot qualify a decision.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
import math
from typing import Annotated, Literal

import pandas as pd
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from desk.alpaca_assets import IdentityRecord
from desk.alpaca_volume import (BarRequest, VolumeCache, VolumeObservation, definition_id as alpaca_definition,
                                prior_sessions, share_basis_id as alpaca_share_basis)
from desk.bars import BarDataError
from desk.calendar import ET, clock, sessions

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


class DailyHistoryChanged(PriceHistoryChanged):
    """Validated replacement history differs from the armed daily reference."""


class AnchorAdjustment(Evidence):
    """Original ordinary action economics explaining only the latest raw anchor."""
    event_id: Text
    effective_session: date
    verified_at: AwareDatetime
    kind: Literal["split", "cash_dividend"]
    value: Decimal = Field(gt=0, allow_inf_nan=False)

    def expected_close(self, raw):
        value = Decimal(str(raw))
        return value / self.value if self.kind == "split" else value - self.value


class VendorPriceBasis(Evidence):
    """Observed vendor consistency, explicitly NOT complete corporate-action coverage.

    ``webull-discovery-v1`` (G5a checkpoint 3) is the same check over a discovery
    scope's required sessions only. It serves the weekly leader build and nothing else:
    it can never arm, revalidate or replace full-history (``webull-history-v1``) evidence.
    """
    method: Literal["webull-history-v1", "webull-discovery-v1"]
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
    anchor_adjustment: AnchorAdjustment | None = None

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
        expected = Decimal(str(self.raw_anchor_close))
        if self.anchor_adjustment is not None:
            action = self.anchor_adjustment
            if not self.anchor_session < action.effective_session == self.basis_session or clock(action.verified_at) > clock(self.verified_at) or clock(action.verified_at).date() != self.basis_session:
                raise ValueError("Anchor adjustment interval/receipt invalid")
            if action.kind == "split" and action.value == 1:
                raise ValueError("Anchor split changes no shares")
            expected = action.expected_close(self.raw_anchor_close)
        if rows[-1][4] != self.daily_anchor_close or expected <= 0 or abs(Decimal(str(self.daily_anchor_close))-expected) > Decimal("0.000001"):
            raise ValueError("Daily/raw close pair disagrees")
        return self


PriceEvidence = PriceBasis | VendorPriceBasis
VENDOR_METHODS = ("webull-history-v1", "webull-discovery-v1")
DISCOVERY_METHOD = "webull-discovery-v1"

def _price(raw, now) -> PriceEvidence:
    try:
        basis = (VendorPriceBasis if isinstance(raw, dict) and raw.get("method") in VENDOR_METHODS
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
        if basis.normalization == "unadjusted" and basis.anchor_adjustment and basis.anchor_session in df.index.tz_convert(ET).date:
            raise BarDataError("Historical raw anchor is on a pre-action price basis")
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
            raise DailyHistoryChanged("Daily OHLCV used to arm this signal was revised; rebuild signal")
        if DISCOVERY_METHOD in (old.method, new.method):
            # Withhold, never invalidate: discovery evidence is not signal evidence.
            raise BarDataError("Discovery-scoped price evidence cannot arm or revalidate a signal")
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


# ---------------------------------------------------------------------------
# Alpaca SIP decision volume (G5a checkpoint 2). A separate, typed input attached
# as ``attrs["decision_volume"]``; the Webull ``volume`` column, ``volume_basis``
# and price evidence are never rewritten. With it attached, it is the only volume
# source for the decision consumers (no Webull, IEX or native fallback inside one
# comparison). Without it, the unchanged Webull path above applies.
# ---------------------------------------------------------------------------
ALPACA_DAILY_POLICY = "alpaca-sip-split:native-daily-v1"
ALPACA_RTH_POLICY = "alpaca-sip-split:rth-m15-v1"
ALPACA_CHANNEL_POLICY = {"native-daily": ALPACA_DAILY_POLICY, "rth-m15": ALPACA_RTH_POLICY}


class VolumeUnavailable(BarDataError):
    """A volume-dependent check cannot run; ``code`` says why. Other checks continue."""

    def __init__(self, code: str):
        self.code = code
        super().__init__("volume unavailable: " + code)


class AlpacaDecisionVolume(BaseModel):
    """One ticker's Alpaca SIP volume for one channel and request window, or why not."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    policy: Literal["alpaca-sip-split:native-daily-v1", "alpaca-sip-split:rth-m15-v1"]
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    reason: Text | None = None
    desk_symbol: Text
    channel: Literal["native-daily", "rth-m15"]
    complete_through: date | None = None      # daily: latest session final at receipt
    identity: IdentityRecord | None = None
    request: dict | None = None               # timeframe/start/end/adjustment/feed for this ticker
    observation: VolumeObservation | None = None

    @model_validator(mode="after")
    def consistent(self):
        if ALPACA_CHANNEL_POLICY[self.channel] != self.policy:
            raise ValueError("Decision-volume policy does not match its channel")
        if self.status == "UNAVAILABLE":
            if not self.reason or self.observation is not None:
                raise ValueError("Unavailable volume needs a reason and no observation")
            return self
        obs, record = self.observation, self.identity
        if obs is None or record is None or self.request is None or self.reason is not None:
            raise ValueError("Available volume needs identity, request and observation")
        if (obs.feed, obs.adjustment, obs.channel) != ("sip", "split", self.channel) or \
                obs.definition_id != alpaca_definition("split", self.channel) or \
                obs.share_basis_id != alpaca_share_basis("split"):
            raise ValueError("Decision volume must be split-adjusted SIP on its own channel")
        if (obs.provider_identity, obs.mapping_provenance, obs.requested_symbol) != (
                record.alpaca_asset_id, record.namespace, record.alpaca_symbol) or record.desk_symbol != self.desk_symbol:
            raise ValueError("Volume observation and identity mapping disagree")
        if self.channel == "native-daily" and self.complete_through is None:
            raise ValueError("Daily decision volume needs its completion session")
        return self

    def bar_request(self) -> BarRequest:
        return BarRequest(symbols=(self.identity.alpaca_symbol,), **self.request)

    def request_key(self) -> str:
        return VolumeCache.key(self.bar_request(), self.identity.alpaca_symbol, self.identity.namespace)


def attached_decision(df: pd.DataFrame) -> AlpacaDecisionVolume | None:
    raw = df.attrs.get("decision_volume")
    if raw is None:
        return None
    try:
        return raw if isinstance(raw, AlpacaDecisionVolume) else AlpacaDecisionVolume.model_validate(raw)
    except ValidationError:
        raise VolumeUnavailable("MALFORMED_DECISION_VOLUME") from None


def _webull_instrument(df: pd.DataFrame) -> str | None:
    identity = df.attrs.get("provider_identity") or {}
    meta = df.attrs.get("security_metadata") or {}
    return identity.get("instrument_id") or meta.get("instrument_id")


def _usable(df: pd.DataFrame, decision: AlpacaDecisionVolume, channel: str) -> AlpacaDecisionVolume:
    if decision.channel != channel:
        raise VolumeUnavailable("WRONG_CHANNEL")
    if decision.status != "AVAILABLE":
        raise VolumeUnavailable(decision.reason)
    if _webull_instrument(df) != decision.identity.webull_instrument_id:
        raise VolumeUnavailable("WEBULL_IDENTITY_MISMATCH")
    return decision


@dataclass(frozen=True)
class VolumeWindow:
    """The volumes one comparison uses, all from one source and definition.

    ``source == "alpaca"``: exact ``Decimal`` share counts joined by NY session date.
    ``source == "webull"``: the unchanged Webull path (floats from the frame).
    """
    source: Literal["alpaca", "webull"]
    sessions: tuple[date, ...]
    values: tuple
    decision: AlpacaDecisionVolume | None = None

    def used(self, start: int = 0) -> list[tuple[str, Decimal]]:
        return [(d.isoformat(), v) for d, v in zip(self.sessions[start:], self.values[start:])]


def decision_window(df: pd.DataFrame, n: int, *, final_only: bool = False) -> VolumeWindow:
    """The last ``n`` sessions of ``df`` (or, with ``final_only``, ending at the latest
    session whose Alpaca native daily is final). Missing or unfinished sessions raise
    ``VolumeUnavailable`` with a specific code; nothing is zero- or forward-filled."""
    decision = attached_decision(df)
    if decision is None:
        window = df.iloc[-n:]
        volume_basis(window)
        return VolumeWindow("webull", tuple(window.index.tz_convert(ET).date), tuple(window["volume"]))
    decision = _usable(df, decision, "native-daily")
    if df.empty or not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise VolumeUnavailable("UNDATED_PRICE_HISTORY")
    dates = list(df.index.tz_convert(ET).date)
    if final_only:
        dates = [d for d in dates if d <= decision.complete_through]
    window = dates[-n:]
    if len(window) < n:
        raise VolumeUnavailable("SHORT_WINDOW")
    if window != sessions(window[0], window[-1]):
        raise VolumeUnavailable("MISALIGNED_SESSIONS")
    if window[-1] > decision.complete_through:
        raise VolumeUnavailable("DAILY_NOT_COMPLETED_AT_RECEIPT")
    after = decision.identity.valid_after_session
    if after is not None and window[0] <= after:
        raise VolumeUnavailable("UNSUPPORTED_HISTORICAL_MAPPING")
    by_day = {pd.Timestamp(t).tz_convert(ET).date(): v for t, v in decision.observation.bars}
    if any(d not in by_day for d in window):
        raise VolumeUnavailable("MISSING_DAILY_SESSIONS")
    return VolumeWindow("alpaca", tuple(window), tuple(by_day[d] for d in window), decision)


def decision_rel_volume(df: pd.DataFrame, length: int) -> pd.Series | None:
    """Daily relative volume from attached Alpaca volume, or None (unchanged Webull path).

    Same denominator semantics as the Webull feature: the bar's volume over the mean of
    the ``length`` sessions ending with that bar. Computed in Decimal and converted to
    float once per point; no trigger or filter consumes it (display/diagnostic only).
    Points without a full, final, same-identity window are NaN.
    """
    try:
        decision = attached_decision(df)
    except VolumeUnavailable:
        return pd.Series(float("nan"), index=df.index, name="rel_volume")
    if decision is None:
        return None
    out = pd.Series(float("nan"), index=df.index, name="rel_volume")
    try:
        decision = _usable(df, decision, "native-daily")
    except VolumeUnavailable:
        return out
    by_day = {pd.Timestamp(t).tz_convert(ET).date(): v for t, v in decision.observation.bars}
    dates = list(df.index.tz_convert(ET).date)
    after = decision.identity.valid_after_session
    for i in range(length - 1, len(dates)):
        window = dates[i - length + 1:i + 1]
        if (window[-1] > decision.complete_through or (after is not None and window[0] <= after)
                or any(d not in by_day for d in window) or window != sessions(window[0], window[-1])):
            continue
        total = sum((by_day[d] for d in window), Decimal(0))
        if total > 0:
            out.iloc[i] = float(by_day[window[-1]] * length / total)
    return out


def alpaca_ep_inputs(daily: pd.DataFrame, m15: pd.DataFrame, entry: date):
    """(daily observation, RTH observation, decisions) for the EP component, or raise."""
    d, m = attached_decision(daily), attached_decision(m15)
    if d is None or m is None:
        raise VolumeUnavailable("EP_VOLUME_NOT_ATTACHED")
    d, m = _usable(daily, d, "native-daily"), _usable(m15, m, "rth-m15")
    if d.identity != m.identity:
        raise VolumeUnavailable("IDENTITY_MISMATCH")
    prior = prior_sessions(entry)
    if d.identity.valid_after_session is not None and prior[0] <= d.identity.valid_after_session:
        raise VolumeUnavailable("UNSUPPORTED_HISTORICAL_MAPPING")
    # The Webull instrument's own price history must cover the volume window.
    if not set(prior) <= set(daily.index.tz_convert(ET).date):
        raise VolumeUnavailable("WEBULL_HISTORY_DOES_NOT_COVER_WINDOW")
    return d.observation, m.observation, (d, m)


def _vhash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def volume_evidence(consumer: str, rule_version: str, result: dict,
                    parts: list[tuple[AlpacaDecisionVolume, list[tuple[str, Decimal]]]]) -> dict:
    """Persisted qualification evidence. ``dependency_digest`` covers exactly the used
    values, definitions, identity mapping, rule and result; ``audit`` keeps receipts,
    request keys and snapshot digests (never part of candidate identity)."""
    identities = {p.identity.namespace for p, _ in parts}
    if len(identities) != 1:
        raise VolumeUnavailable("IDENTITY_MISMATCH")
    inputs, audit = [], []
    for decision, used in parts:
        obs = decision.observation
        inputs.append({"policy": decision.policy, "channel": obs.channel, "feed": obs.feed,
                       "adjustment": obs.adjustment, "definition_id": obs.definition_id,
                       "share_basis_id": obs.share_basis_id, "values": [[k, str(v)] for k, v in used]})
        audit.append({"request": decision.request, "request_key": decision.request_key(),
                      "snapshot_digest": obs.content_digest, "page_digests": list(obs.page_digests),
                      "first_sent_at": obs.first_sent_at.isoformat(), "received_at": obs.received_at.isoformat(),
                      "complete_through": decision.complete_through.isoformat() if decision.complete_through else None})
    record = parts[0][0].identity
    dependency = {"source": "alpaca", "consumer": consumer, "identity": record.dependency(),
                  "rule_version": rule_version, "result": result, "inputs": inputs}
    return {**dependency, "dependency_digest": _vhash(dependency),
            "audit": {"inputs": audit, "identity_receipt": record.received_at.isoformat(),
                      "asset_list_digest": record.asset_list_digest,
                      "webull": {"symbol": record.webull_symbol, "name": record.webull_name,
                                 "exchange": record.webull_exchange},
                      "alpaca": {"name": record.alpaca_name, "exchange": record.alpaca_exchange,
                                 "status": record.alpaca_status}}}


def dependency_terms(evidence: dict | None) -> dict | None:
    """The part of persisted volume evidence that defines trade terms."""
    if evidence is None:
        return None
    return {k: v for k, v in evidence.items() if k != "audit"}
