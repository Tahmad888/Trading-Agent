"""Independent event/exit evidence for risk, never reconstructed from a proposal.

Plan B: unavailable evidence denies eligibility; an unavailable option valuation
only denies stop-budget sizing. Selected exposure remains reviewable.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime
from fractions import Fraction
from typing import Annotated, Callable, ContextManager, Literal, NamedTuple, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


# Quote-source labels that assert independent adapter evidence. A ticket request
# carrying one is refused unless the resolved terms hold matching provenance (F6).
RESERVED_QUOTE_SOURCES = frozenset({"tastytrade-dxlink"})
QUOTE_ENVIRONMENTS = {"live": frozenset({"production"}), "paper": frozenset({"production", "sandbox"})}


class EvidenceUnavailable(ValueError):
    """Independent evidence is unavailable. Its message is a fixed, credential-free
    code (for example ``QUOTE_MAPPING_MISSING``), safe to show on a ticket."""


class QuoteProvenance(BaseModel):
    """Independent adapter identity/health, excluding changing receipt times.

    ``generation`` is the quote connection session: a reconnect, restart or another
    process is a different session, so approvals never carry across it (child 3, F8).
    ``mapping_digest`` is the reviewed Webull↔quote-provider mapping the quote was
    verified against before any signal state could change (child 3, F7).
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    streamer_symbol: str = Field(min_length=1)
    identity_digest: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    mapping_digest: str | None = None


class RiskTerms(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str
    event_digest: str
    symbol: str
    setup_id: str
    setup_version: str
    direction: Literal["long", "short"]
    chase_reference: Positive
    entry_level: Positive
    stop: Positive
    target: Positive | None = None
    checked_at: AwareDatetime
    valid_until: AwareDatetime
    underlying_price: Positive
    quote_at: AwareDatetime
    quote_provenance: QuoteProvenance | None = None
    # Independent conditional exit prices, not a valuation at the stock stop.
    # Empty on the standard scanner adapter: no option valuation model is implied.
    option_exit_prices: dict[str, Annotated[float, Field(ge=0, allow_inf_nan=False)]] = Field(default_factory=dict)
    option_exit_source: str | None = None
    max_stop_fraction: Positive | None = None


class RiskTermsSource(Protocol):
    def resolve(self, event_id: str, now: datetime) -> RiskTerms: ...


class EventStatus(NamedTuple):
    """The signal event as the final ticket transaction sees it."""
    eligible: bool
    event_digest: str | None
    quote_provenance: QuoteProvenance | None = None
    symbol: str | None = None
    quote_price: float | None = None
    quote_at: datetime | None = None
    price_basis: dict | None = None   # the signal's stored price evidence (identity source)
    reason: str | None = None         # fixed code when a quote fence refuses
    webull_identity: dict | None = None  # verified under the identity fence (child 3, H1)


class EventFence(Protocol):
    """Optional companion of a terms source; tickets refuse a source without it.

    ``held_event`` holds the signal store's write lock for the final approve or
    consume write and yields ``status(at)`` read under that lock.
    """
    def held_event(self, event_id: str) -> ContextManager[Callable[[datetime], EventStatus]]: ...


def stop_distance(entry: float, stop: float, direction: str) -> Fraction:
    """Exact directional arithmetic; this does not enable short execution."""
    distance = (Fraction(str(entry)) - Fraction(str(stop))) * (1 if direction == "long" else -1)
    if direction not in {"long", "short"} or distance <= 0:
        raise ValueError("Stop must be on the loss side of the executable entry")
    return distance


EP_SETUP = "5_qullamaggie_episodic_pivot"


def chase_reference(event: dict) -> float:
    """The level the chase allowance is measured from.

    EP: the selected opening-range high frozen into the event (its entry level), per
    Taz 2026-10-02 (User policy): Kullamägi buys the break of the opening-range high
    (https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/), so an EP whose
    range high is far above the open is not a chase by itself. The 3% allowance stays
    the desk's Assumption. Other setups: the candidate's trigger, unchanged.
    """
    if event["signal"]["setup_id"] == EP_SETUP:
        return event["entry_level"]
    return event["candidate_signal"]["trigger"]


def stored_vendor_host(persisted: dict) -> str | None:
    """The Webull vendor host the stored event's terms (else candidate) name, or None.

    Read from the signal store's own row under its lock; nothing from a ticket request.
    """
    import json
    try:
        text = persisted["signal_terms"] or persisted["candidate_signal"]
        basis = json.loads(text).get("price_basis") if text else None
        host = basis.get("host") if isinstance(basis, dict) else None
        return host if isinstance(host, str) and host else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


class EventRiskSource:
    """Resolve from SignalStore after fresh scanner revalidation.

    quote_source(symbol) must be the trusted adapter's (symbol, price, quote_at),
    never proposal fields. No broker order methods are used. The caller supplies
    the actual source/log; there is no permissive fallback to proposal loss text.
    """
    def __init__(self, source, log, quote_source):
        self.source, self.log, self.quote_source = source, log, quote_source

    @contextmanager
    def held_event(self, event_id):
        """Signal store lock, then the volume/identity and vendor identity reservations.

        All are held until the ticket's final write commits (audit F2; child 3, H1), so
        a volume STOP, failure, revision, mapping change or identity-health outcome
        either committed before and is seen by ``status`` or lands after the commit.
        Order: ticket -> signal -> Alpaca volume/identity -> Webull vendor identity ->
        account (taken last by tickets). The Alpaca reservation is taken only when the
        stored event depends on Alpaca evidence (re-audit R2), and the vendor identity
        reservation only when the stored event names a Webull vendor host; both are
        decided under the signal lock from the persisted row, before the account lock
        and the final clock. Price-only events still run ``status``.
        """
        from desk.alpaca_source import needs_volume_guard, provider_of
        from desk.vendor_basis import event_identity, vendor_store
        provider = provider_of(self.source)
        with self.log.signals.held_event(event_id) as view, ExitStack() as guards:
            persisted = view.persisted()
            held_paths = set()
            if provider is not None and needs_volume_guard(persisted):
                guards.enter_context(provider.held())
                held_paths = set(provider.guard_paths())
            host, identity_db, fence_problem = stored_vendor_host(persisted), None, None
            if host is not None:
                try:
                    store = vendor_store(self.source, host)
                    if str(store.path.resolve()) not in held_paths:  # one file is reserved once
                        identity_db = guards.enter_context(store.held_identity())
                except EvidenceUnavailable as exc:
                    fence_problem = str(exc)  # refuse at status; never raise mid-transaction

            def status(at):
                event = view(at)
                eligible = bool(event["eligible"])
                identity = None
                basis = event["signal"].get("price_basis")
                if eligible:
                    # G5a: saved volume qualification must still be current in the
                    # integrated cache (local read under the held reservation; no request).
                    from desk.scanner import volume_status
                    from desk.signal_state import restore_signal
                    state, _ = volume_status(self.source, restore_signal(event["candidate_signal"]), at,
                                             refresh=False)
                    eligible = state == "OK"
                if eligible and isinstance(basis, dict) and basis.get("host"):
                    try:
                        if fence_problem or basis.get("host") != host:
                            raise EvidenceUnavailable(fence_problem or "WEBULL_IDENTITY_FENCE_NOT_HELD")
                        identity = event_identity(self.source, basis, event["signal"]["symbol"], db=identity_db)
                    except EvidenceUnavailable as exc:
                        return EventStatus(False, event["terms_digest"], symbol=event["signal"]["symbol"],
                                           price_basis=basis, reason=str(exc))
                return EventStatus(eligible, event["terms_digest"], symbol=event["signal"]["symbol"],
                                   price_basis=basis, webull_identity=identity)
            yield status

    def resolve(self, event_id, now):
        from desk.playbook.cards import CARDS
        from desk.scanner import revalidate_signal

        from desk.vendor_basis import event_identity

        event = self.log.signals.get(event_id, now)
        # H1: a signal armed on Webull vendor evidence needs its verified identity
        # before any quote lookup or state-mutating revalidation (local read).
        event_identity(self.source, event["signal"].get("price_basis"), event["signal"]["symbol"])
        symbol, price, quote_at = self.quote_source(event["signal"]["symbol"])
        report = revalidate_signal(self.source, self.log, event_id, now,
                                   symbol=symbol, price=price, quote_at=quote_at)
        if not report["eligible"]:
            raise ValueError("Signal revalidation failed")
        event = self.log.signals.get(event_id, now)
        if not event["eligible"] or not event["terms_digest"]:
            raise ValueError("Signal event is unavailable")
        sig = event["signal"]
        width = (CARDS[sig["setup_id"]].p("max_stop_adr") * sig["adr_pct"] / 100
                 if sig.get("stop_basis") == "session_low" else None)
        return RiskTerms(event_id=event_id, event_digest=event["terms_digest"],
                         symbol=sig["symbol"], setup_id=sig["setup_id"],
                         setup_version=sig["setup_version"], direction=sig["direction"],
                         entry_level=event["entry_level"], chase_reference=chase_reference(event),
                         stop=sig["stop"], target=sig["target"],
                         checked_at=report["checked_at"], valid_until=event["valid_until"],
                         underlying_price=price, quote_at=quote_at, max_stop_fraction=width)
