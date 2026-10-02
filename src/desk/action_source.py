"""Opt-in bridge from a reviewed action ledger to fresh Webull bar fetches.

It annotates already-normalized vendor prices; it never adjusts OHLCV. The channel
review must establish the supplied price basis separately from action access.
"""
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from desk.action_ledger import ActionLedger
from desk.bar_contract import BarProvenance
from desk.bars import BarDataError
from desk.calendar import clock
from desk.data_basis import Evidence, VolumeBasis


class PriceChannelReview(Evidence):
    host: Literal["api.sandbox.webull.com", "api.webull.com"]
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
    timeframe: Literal["D", "M15"]
    normalization: Literal["unadjusted", "split_adjusted", "split_dividend_adjusted"]

    volume_policy: Literal["native_no_split_window"] | None = None
    volume_evidence_ref: str | None = Field(default=None, min_length=1)
    ep_volume_comparison: bool = False

    @model_validator(mode="after")
    def native_minutes(self):
        if self.timeframe == "M15" and self.normalization != "unadjusted":
            raise ValueError("Native Webull minutes are raw; this bridge cannot adjust them")
        if bool(self.volume_policy) != bool(self.volume_evidence_ref):
            raise ValueError("Native volume acceptance requires a policy and evidence reference")
        if self.ep_volume_comparison and not self.volume_policy:
            raise ValueError("EP comparison requires native volume acceptance")
        return self


def native_volume_basis(channel, basis):
    """Only rows since the latest split are attested in current share units.

    Never guess whether the provider adjusts older volume. Consumers must check
    their actual window; price warm-up can extend before this volume interval.
    """
    splits = [a for a in basis.actions if a.kind == "split"]
    last = max(splits, key=lambda a: a.effective_session) if splits else None
    first = last.effective_session if last else basis.coverage_start
    native_channel = "webull:native:D" if channel.timeframe == "D" else "webull:minute:RTH"
    return VolumeBasis(source="Webull OpenAPI", evidence_ref=channel.volume_evidence_ref,
        channel=native_channel, definition_id=native_channel + ":provider-reported", units="shares",
        share_basis_id=f"webull:{basis.security_id}:since:{last.revision if last else basis.coverage_start}",
        symbol=basis.symbol, security_id=basis.security_id, valid_from=first,
        valid_through=basis.basis_session,
        comparison_policy="webull-rth30/native-daily50-v1" if channel.ep_volume_comparison else None)


class ActionBackedSource:
    def __init__(self, source, ledger: ActionLedger, channels: tuple[PriceChannelReview, ...], *,
                 host: str, clock=lambda: datetime.now(timezone.utc)):
        self.source, self.ledger, self._clock = source, ledger, clock
        if any(c.host != host for c in channels):
            raise BarDataError("Price channel evidence belongs to a different Webull host")
        self.channels = {(c.symbol, c.timeframe): c for c in channels}
        if len(self.channels) != len(channels):
            raise BarDataError("Duplicate price channel review")

    def __getattr__(self, name):
        return getattr(self.source, name)

    def bars(self, symbols, *, timespan, **kwargs):
        # Don't spend provider calls for symbols with no accepted source profile.
        for symbol in symbols:
            channel = self.channels.get((symbol, timespan))
            if channel is None:
                raise BarDataError("No reviewed price channel for this symbol/timeframe")
            self.ledger.basis(symbol, self._clock(), normalization=channel.normalization)
        frames = self.source.bars(symbols, timespan=timespan, **kwargs)
        out = {}
        for symbol in symbols:
            frame = frames[symbol].copy()
            channel = self.channels[(symbol, timespan)]
            basis = self.ledger.basis(symbol, self._clock(), normalization=channel.normalization)
            if frame.attrs.get("provider_identity") != {"symbol": symbol, "instrument_id": basis.security_id}:
                raise BarDataError("Webull instrument does not match reviewed corporate-action identity")
            request = frame.attrs.get("webull_request", {})
            if timespan == "M15" and request.get("trading_sessions") != "RTH":
                raise BarDataError("Reviewed bar channel supports regular-session minutes only")
            received = frame.attrs.get("received_at")
            if received is None or clock(received) < clock(basis.verified_at):
                raise BarDataError("Bars predate the current action refresh; refetch and rebuild")
            if clock(received) > clock(self._clock()):
                raise BarDataError("Bar receipt is later than the decision clock")
            meta = frame.attrs.get("bar_provenance", {})
            if meta.get("delay_minutes") != 0 or type(meta.get("delay_minutes")) is not int:
                raise BarDataError("Delayed/unknown data cannot use a decision price profile")
            frame.attrs["bar_provenance"] = BarProvenance(source=channel.source,
                evidence_ref=channel.evidence_ref, timeframe=timespan,
                timestamp_semantics="session_label" if timespan == "D" else "start",
                session="regular", delay_minutes=0, adjustment=channel.normalization,
                price_scale_id="reviewed-action-ledger", price_basis=basis).model_dump(mode="json")
            if channel.volume_policy:
                frame.attrs["volume_basis"] = native_volume_basis(channel, basis).model_dump(mode="json")
            out[symbol] = frame
        return out


def configured_source(source, env, *, clock=lambda: datetime.now(timezone.utc)):
    """Scanner opt-in; configuring an API key alone does not enable profiles."""
    vendor_path = env.get("DESK_VENDOR_BASIS_DB")
    if vendor_path:
        from desk.vendor_basis import VendorBasisSource, VendorHistoryStore
        legacy_env = {k:v for k,v in env.items() if k != "DESK_VENDOR_BASIS_DB"}
        fallback_issue = None
        try:
            fallback = configured_source(source,legacy_env,clock=clock) if (
                env.get("DESK_ACTION_CHANNELS") or env.get("DESK_ACTION_LEDGER")) else None
        except (BarDataError,OSError,ValueError,sqlite3.Error):
            fallback = None
            fallback_issue = "Optional reviewed fallback configuration unavailable"

        try:
            store = VendorHistoryStore(vendor_path)
        except sqlite3.Error:
            raise BarDataError("Vendor history store unavailable") from None
        actions = None
        action_issue = None
        if env.get("DESK_AUTO_ACTION_DB"):
            from desk.batch_actions import BatchActions
            try:
                actions = BatchActions(env["DESK_AUTO_ACTION_DB"],env.get("MASSIVE_API_KEY"),clock_fn=clock,
                    volume_policy=env.get("DESK_NATIVE_VOLUME_POLICY") or None)
            except (BarDataError,OSError,ValueError,sqlite3.Error):
                action_issue = "Optional automatic action configuration unavailable"
        wrapped = VendorBasisSource(source,store,
            host=env.get("WEBULL_HOST") or "api.webull.com",clock_fn=clock,fallback=fallback,actions=actions)
        wrapped.fallback_issue = fallback_issue
        wrapped.auto_action_issue = action_issue
        return wrapped
    config_path = env.get("DESK_ACTION_CHANNELS")
    ledger_path = env.get("DESK_ACTION_LEDGER")
    if not config_path and not ledger_path:
        return source
    if not config_path or not ledger_path or not Path(ledger_path).is_file():
        raise BarDataError("Both reviewed action channels and an existing ledger are required")
    try:
        channels = tuple(PriceChannelReview.model_validate(c) for c in json.loads(Path(config_path).read_text()))
    except (OSError, ValueError, TypeError):
        raise BarDataError("Invalid reviewed price channel configuration") from None
    return ActionBackedSource(source, ActionLedger(ledger_path), channels,
                              host=env.get("WEBULL_HOST") or "api.webull.com", clock=clock)
