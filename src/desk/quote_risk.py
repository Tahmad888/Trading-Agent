"""Independent quote bridge; no fabricated account/market/option liquidity inputs."""
from contextlib import contextmanager

from desk.risk_terms import EventRiskSource, EventStatus
from desk.tastytrade_quotes import QuoteUnavailable


class TastytradeRiskSource(EventRiskSource):
    def __init__(self, source, log, quotes, *, max_quote_age=None):
        from desk.risk import RiskLimits
        self.source, self.log, self.quotes = source, log, quotes
        self.max_quote_age = RiskLimits().max_quote_age if max_quote_age is None else max_quote_age

    def resolve(self, event_id, now):
        event = self.log.signals.get(event_id, now)
        symbol = event["signal"]["symbol"]
        observation, proof = self.quotes.trade(symbol, now, self.max_quote_age)
        if observation.identity.kind != "Equity":
            raise QuoteUnavailable("UNDERLYING_EQUITY_REQUIRED")
        # The local immutable capture belongs to this resolve, not shared mutable
        # "last provenance" state. Scanner revalidation still checks bars/volume.
        source = EventRiskSource(self.source, self.log,
                                lambda _: (symbol, float(observation.price), observation.traded_at))
        terms = source.resolve(event_id, now)
        return terms.model_copy(update={"quote_provenance": proof})

    @contextmanager
    def held_event(self, event_id):
        # ticket -> signal -> volume/identity -> quote health -> account.
        # No transport operation occurs within these locks.
        with super().held_event(event_id) as status, self.quotes.held_health():
            def final(at):
                event = status(at)
                symbol = event.symbol  # Read by the existing held signal-store view.
                try:
                    value, proof = self.quotes.trade(symbol, at, self.max_quote_age)
                except QuoteUnavailable:
                    return EventStatus(False, event.event_digest)
                return EventStatus(event.eligible, event.event_digest, proof, symbol,
                                   float(value.price), value.traded_at)
            yield final
