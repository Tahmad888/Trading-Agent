"""Explicit synthetic adapter metadata for old risk-policy acceptance tests.

This catalog is independent of proposal payloads; tests that alter a symbol or
expiry must choose another registered fixture contract. Never patch production
validation or manufacture metadata by reading the proposal under test.
"""
from datetime import date, timedelta

from desk.instruments import ContractBook, OptionContract
from desk.risk import evaluate as evaluate_risk
from desk.risk_terms import RiskTerms
from desk.playbook.cards import CARDS
from tests.conftest import NOW
from desk.risk_context import MarketContext, default_registry
from desk.playbook.filters import MarketSize


def contract(underlying, expiry, right, strike):
    expiry = date.fromisoformat(expiry)
    symbol = f"{underlying}{expiry:%y%m%d}{right[0].upper()}{int(strike * 1000):08d}"
    return OptionContract(
        broker_contract_id=f"fixture:{symbol}", symbol=symbol, underlying=underlying,
        right=right, strike=strike, expiry=expiry, multiplier=100,
        deliverable_symbol=underlying, deliverable_shares=100, deliverable_cash_usd=0,
        adjusted=False, exercise_style="american", settlement="physical", currency="USD", tradable=True,
    )


BOOK = ContractBook(source="synthetic acceptance fixture; not broker verified", as_of=NOW, contracts=(
    contract("SPY", "2026-09-30", "put", 500),
    contract("SPY", "2026-09-30", "put", 499),
    contract("AAPL", "2026-11-20", "call", 230),
    contract("AAPL", "2026-11-20", "put", 230),
    contract("AAPL", "2026-10-04", "call", 230),
    contract("AAPL", "2026-10-08", "call", 230),
    contract("SPY", "2026-10-02", "call", 500),
    contract("SPY", "2026-10-06", "call", 500),
    contract("SPY", "2026-10-16", "call", 500),
))


MARKET = MarketContext(regime=MarketSize.FULL, source="synthetic regime", as_of=NOW, reason="fixture uptrend")
REGISTRY = default_registry().model_copy(update={"entries": tuple(
    e.model_copy(update={"live_enabled": True}) for e in default_registry().entries)})


class FixtureTerms:
    """Fixed synthetic catalog, never reads the proposal or its loss assertion."""
    def resolve(self, event_id, now):
        symbol, entry, stop = {"spy": ("SPY", 500, 495), "aapl": ("AAPL", 230, 225),
                               "msft": ("MSFT", 250, 248.75), "msft_tight": ("MSFT", 250, 249.9),
                               "msft_wide": ("MSFT", 250, 229.99), "aapl_wide": ("AAPL", 230, 225),
                               "spy_short": ("SPY", 500, 505), "aapl_short": ("AAPL", 230, 235), "small_spread": ("SPY", 500, 495)}[event_id]
        exits = {
            "SPY260930P00500000": 1.4, "SPY260930P00499000": 0.4,
            "SPY261002C00500000": 2, "SPY261016C00500000": 2.8, "SPY261006C00500000": 2.8,
            "AAPL261120C00230000": 1.3, "AAPL261120P00230000": 1.3,
            "AAPL261004C00230000": 1.3, "AAPL261008C00230000": 1.3,
        }
        if event_id == "small_spread":
            exits = {"SPY260930P00500000": 1.5, "SPY260930P00499000": 0.9}
        if event_id == "aapl_wide":
            exits["AAPL261120C00230000"] = 0.9
        short = event_id.endswith("_short")
        setup = "8_raschke_holy_grail" if short else "1_qullamaggie_breakout"
        return RiskTerms(event_id=event_id, event_digest="fixture-v1", symbol=symbol,
            setup_id=setup, setup_version=CARDS[setup].fingerprint(),
            direction="short" if short else "long", entry_level=entry, chase_reference=entry, stop=stop, checked_at=NOW, valid_until=NOW+timedelta(minutes=15),
            underlying_price=entry, quote_at=NOW, option_exit_prices=exits,
            option_exit_source="explicit synthetic conditional exits")


def evaluate(*args, **kwargs):
    kwargs.setdefault("terms_source", FixtureTerms())
    kwargs.setdefault("contract_book", BOOK)
    kwargs.setdefault("registry", REGISTRY)
    kwargs.setdefault("market", MARKET)
    return evaluate_risk(*args, **kwargs)
