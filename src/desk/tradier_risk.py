"""Tradier signal/ticket composition; trusted account and contract adapters stay mandatory."""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
import threading

from desk.instruments import canonical_symbol
from desk.risk_terms import EventRiskSource, EventStatus, EvidenceUnavailable
from desk.tastytrade_quotes import QuoteUnavailable
from desk.tickets import ExecutableQuotes
from desk.tradier_quotes import SOURCE, requested
from desk.vendor_basis import event_identity


class TradierRiskSource(EventRiskSource):
    def __init__(self, source, log, store, environment):
        if environment not in {"production", "sandbox"}:
            raise QuoteUnavailable("ENVIRONMENT_INVALID")
        self.source, self.log, self.store, self.environment = source, log, store, environment
        self.local = threading.local()

    def verified(self, db, symbol, basis, at, webull=None):
        if webull is None:
            webull = event_identity(self.source, basis, symbol)
        data, _ = self.store.read(db, self.environment, symbol, "trade", at)
        mapping, version = self.store.review(db, self.environment, symbol, basis, webull, data["identity"])
        view, proof = self.store.proof(db, self.environment, symbol, "trade", at, mapping, version)
        return view, proof, mapping, version, webull

    def refresh_signal(self, event_id, now, client):
        """Explicit standalone signal check: GET before identity/scanner revalidation."""
        if client.environment != self.environment:
            raise QuoteUnavailable("ENVIRONMENT_MISMATCH")
        event = self.log.signals.get(event_id, now)
        self.store.fetch(client, [requested(event["signal"]["symbol"], "stock")])
        return self.resolve(event_id, max(now, client.clock()))

    def resolve(self, event_id, now):
        event = self.log.signals.get(event_id, now)
        symbol = event["signal"]["symbol"]
        with self.store.held() as db:
            view, proof, _, _, _ = self.verified(db, symbol, event["signal"].get("price_basis"), now)
        # Immutable local capture, never mutable 'last ticket' or 'last provenance'.
        trade = view["prices"]["last"]["value"]
        stamp = datetime.fromisoformat(view["times"]["trade_date"]["utc"])
        base = EventRiskSource(self.source, self.log, lambda _: (symbol, float(trade), stamp))
        return base.resolve(event_id, now).model_copy(update={"quote_provenance": proof})

    @contextmanager
    def held_event(self, event_id):
        with super().held_event(event_id) as status, self.store.held() as db:
            def final(at):
                event = status(at)
                self.local.event = event
                if event.reason or not event.eligible:
                    return event
                try:
                    view, proof, _, _, _ = self.verified(db, event.symbol, event.price_basis, at,
                                                        event.webull_identity)
                    return EventStatus(event.eligible, event.event_digest, proof, event.symbol,
                                       float(view["prices"]["last"]["value"]),
                                       datetime.fromisoformat(view["times"]["trade_date"]["utc"]),
                                       event.price_basis, webull_identity=event.webull_identity)
                except EvidenceUnavailable as exc:
                    return EventStatus(False, event.event_digest, symbol=event.symbol, reason=str(exc))
            try:
                yield final
            finally:
                self.local.event = None

    def executable(self, request, terms, book, at):
        held_event = getattr(self.local, "event", None)
        if held_event is not None:
            basis, webull = held_event.price_basis, held_event.webull_identity
        else:
            event = self.log.signals.get(request.event_id, at)
            basis, webull = event["signal"].get("price_basis"), None
        symbol = terms.symbol
        with self.store.held() as db:
            _, trade_proof, mapping, version, webull = self.verified(db, symbol, basis, at, webull)
            if trade_proof != terms.quote_provenance:
                raise QuoteUnavailable("TRADIER_TRADE_EVIDENCE_CHANGED")
            view, stock_proof = self.store.proof(db, self.environment, symbol, "quote", at, mapping, version)
            stock_data, _ = self.store.read(db, self.environment, symbol, "quote", at)
            self.store.review(db, self.environment, symbol, basis, webull, stock_data["identity"])
            proofs = {symbol: stock_proof}
            stamps = [datetime.fromisoformat(view["times"][k]["utc"]) for k in ("bid_date", "ask_date")]
            spreads = []
            if request.structure != "shares":
                if book is None:
                    raise QuoteUnavailable("CONTRACT_BOOK_REQUIRED")
                for leg in request.legs:
                    key = canonical_symbol(leg.symbol)
                    matches = [c for c in book.contracts if canonical_symbol(c.symbol) == key]
                    if len(matches) != 1:
                        raise QuoteUnavailable("CONTRACT_BOOK_IDENTITY_UNAVAILABLE")
                    view, proof = self.store.proof(db, self.environment, key, "quote", at, mapping, version,
                                                  contract=matches[0])
                    if matches[0].underlying != symbol:
                        raise QuoteUnavailable("TRADIER_OPTION_UNDERLYING_MISMATCH")
                    proofs[key] = proof
                    stamps.extend(datetime.fromisoformat(view["times"][k]["utc"]) for k in ("bid_date", "ask_date"))
                    bid, ask = (Decimal(view["prices"][k]["value"]) for k in ("bid", "ask"))
                    spreads.append((ask - bid) / ((ask + bid) / 2))
            return ExecutableQuotes(quote_as_of=min(stamps), option_spread_pct_mid=float(max(spreads)) if spreads else None,
                                    provenance=proofs)


def compose(base, *, price_source, log, store, client):
    """Wrap existing independent RiskInputs. No default account/status/OI/market factory.

    A factory can return this object through tickets --adapters module:factory.
    Each command performs a fresh GET before local revalidation. All final reads borrow
    the quote reservation held by TradierRiskSource until the ticket write commits.
    """
    source = TradierRiskSource(price_source, log, store, client.environment)

    def refresh(request, at):
        event = log.signals.get(request.event_id, at)
        names = [requested(event["signal"]["symbol"], "stock")]
        if request.structure != "shares":
            names.extend(requested(leg.symbol, "option") for leg in request.legs)
        # Same option in multiple legs is one quote request, still validated per leg.
        names = list({r["symbol"]: r for r in names}.values())
        store.fetch(client, names)

    return replace(base, terms_source=source, refresh_quotes=refresh, executable_quotes=source.executable)


@dataclass(frozen=True)
class Dependencies:
    """Return this from DESK_TRADIER_BASE_FACTORY; no permissive substitute adapters."""
    base: object
    price_source: object
    log: object


def factory():
    """Optional tickets --adapters desk.tradier_risk:factory entry point.

    Explicit environment/store/base-factory settings required. No credentials saved.
    Only the base factory supplies account, halt/tradability, OI and contract evidence.
    """
    import importlib
    import os
    from desk.tickets import RiskInputs
    from desk.tradier_client import TradierClient
    from desk.tradier_quotes import QuoteStore
    try:
        module, name = os.environ["DESK_TRADIER_BASE_FACTORY"].split(":")
        environment = os.environ["DESK_TRADIER_ENVIRONMENT"]
        path = os.environ["DESK_TRADIER_QUOTE_STORE"]
        if not path.strip():
            raise ValueError
        dependencies = getattr(importlib.import_module(module), name)()
        if not isinstance(dependencies, Dependencies) or not isinstance(dependencies.base, RiskInputs):
            raise ValueError
        client = TradierClient(os.environ["TRADIER_ACCESS_TOKEN"], environment=environment)
    except Exception:
        raise QuoteUnavailable("TRADIER_FACTORY_CONFIGURATION_INVALID") from None
    return compose(dependencies.base, price_source=dependencies.price_source, log=dependencies.log,
                   store=QuoteStore(path), client=client)
