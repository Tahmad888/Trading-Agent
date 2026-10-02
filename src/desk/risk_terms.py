"""Independent event/exit evidence for risk, never reconstructed from a proposal.

Plan B: unavailable evidence denies eligibility; an unavailable option valuation
only denies stop-budget sizing. Selected exposure remains reviewable.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from fractions import Fraction
from typing import Annotated, Callable, ContextManager, Literal, NamedTuple, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


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
        with self.log.signals.held_event(event_id) as view:
            def status(at):
                event = view(at)
                return EventStatus(bool(event["eligible"]), event["terms_digest"])
            yield status

    def resolve(self, event_id, now):
        from desk.playbook.cards import CARDS
        from desk.scanner import revalidate_signal

        event = self.log.signals.get(event_id, now)
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
