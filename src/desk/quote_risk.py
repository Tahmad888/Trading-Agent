"""Independent quote bridge; no fabricated account/market/option liquidity inputs.

Before any signal-state change, ``resolve`` checks (in order) the healthy tastytrade
identity, the Webull identity currently verified by the vendor path (child 3, H1: a
committed contradiction, an unresolved check or an unverified legacy pin refuses), the
reviewed Webull↔tastytrade mapping against the signal's own stored Webull basis and
that verified identity (including its classification), the provider trading status (a
retained halt refuses), and a fresh same-session trade. Any failure is a per-symbol QuoteUnavailable code; the signal's
state, levels, expiry and revision evidence are untouched. Only then does scanner
revalidation run, which may legitimately invalidate a signal whose verified price
crossed its stop.
"""
from contextlib import ExitStack, contextmanager

from desk.risk_terms import EventRiskSource, EventStatus, EvidenceUnavailable
from desk.tastytrade_quotes import QuoteUnavailable
from desk.vendor_basis import event_identity, verified_identity


def webull_identity(source, host, symbol) -> dict | None:
    """The Webull identity currently verified for this host and symbol, else None.

    Local read. A pin alone is not returned: see ``desk.vendor_basis.verified_identity``.
    """
    try:
        return verified_identity(source, host, symbol)
    except EvidenceUnavailable:
        return None


class TastytradeRiskSource(EventRiskSource):
    def __init__(self, source, log, quotes, mappings, *, max_quote_age=None):
        from desk.risk import RiskLimits
        if mappings is None:
            raise QuoteUnavailable("QUOTE_MAPPING_STORE_REQUIRED")
        self.source, self.log, self.quotes, self.mappings = source, log, quotes, mappings
        self.max_quote_age = RiskLimits().max_quote_age if max_quote_age is None else max_quote_age

    def _verified(self, view, symbol, price_basis, at, webull=None):
        """(trade, provenance bound to the mapping). Local reads only, no network.

        ``webull`` is the identity the final status verified under the identity fence;
        otherwise it is read here (EvidenceUnavailable with a fixed code on refusal).
        """
        identity = self.quotes.identity(symbol, at)
        if identity.kind != "Equity":
            raise QuoteUnavailable("UNDERLYING_EQUITY_REQUIRED")
        if webull is None:
            webull = event_identity(self.source, price_basis, symbol)
        mapping = view.verify(symbol, price_basis=price_basis, identity=identity,
                              environment=self.quotes.environment, webull_identity=webull)
        status, _ = self.quotes.status(symbol, at)
        if status == "HALTED":
            # A known halt is never hidden by a young cached price. UNKNOWN is not
            # ACTIVE either: tradability still comes from the separate status input.
            raise QuoteUnavailable("SECURITY_HALTED")
        observation, proof = self.quotes.trade(symbol, at, self.max_quote_age)
        if proof.identity_digest != identity.identity_digest:
            raise QuoteUnavailable("IDENTITY_CHANGED")
        return observation, proof.model_copy(update={"mapping_digest": mapping.mapping_digest})

    def resolve(self, event_id, now):
        event = self.log.signals.get(event_id, now)
        symbol = event["signal"]["symbol"]
        with self.mappings.held() as view:
            observation, proof = self._verified(view, symbol, event["signal"].get("price_basis"), now)
        # The local immutable capture belongs to this resolve, not shared mutable
        # "last provenance" state. Scanner revalidation still checks bars/volume.
        source = EventRiskSource(self.source, self.log,
                                 lambda _: (symbol, float(observation.price), observation.traded_at))
        terms = source.resolve(event_id, now)
        return terms.model_copy(update={"quote_provenance": proof})

    @contextmanager
    def held_event(self, event_id):
        # Lock order: ticket -> signal -> Alpaca volume/identity (when required) -> Webull
        # vendor identity (when the event names a vendor host) -> mapping (shared sidecar
        # lock, inter-process) -> quote health (in-process lock) -> account. All are held
        # until the ticket COMMIT; no network operation occurs within them.
        with super().held_event(event_id) as status, ExitStack() as guards:
            try:
                view, fence_problem = guards.enter_context(self.mappings.held()), None
            except QuoteUnavailable as exc:
                view, fence_problem = None, str(exc)  # busy/unreadable store: refuse, never raise mid-transaction
            guards.enter_context(self.quotes.held_health())

            def final(at):
                event = status(at)
                if event.reason:  # e.g. the Webull identity failed under its fence (H1)
                    return EventStatus(False, event.event_digest, symbol=event.symbol, reason=event.reason)
                if fence_problem:
                    return EventStatus(False, event.event_digest, symbol=event.symbol, reason=fence_problem)
                try:
                    value, proof = self._verified(view, event.symbol, event.price_basis, at,
                                                  webull=event.webull_identity)
                except EvidenceUnavailable as exc:
                    return EventStatus(False, event.event_digest, symbol=event.symbol, reason=str(exc))
                return EventStatus(event.eligible, event.event_digest, proof, event.symbol,
                                   float(value.price), value.traded_at, event.price_basis)
            yield final
