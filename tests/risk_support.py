"""Explicit synthetic adapter metadata for old risk-policy acceptance tests.

This catalog is independent of proposal payloads; tests that alter a symbol or
expiry must choose another registered fixture contract. Never patch production
validation or manufacture metadata by reading the proposal under test.
"""
from datetime import date

from desk.instruments import ContractBook, OptionContract
from desk.risk import evaluate as evaluate_risk
from tests.conftest import NOW


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


def evaluate(*args, **kwargs):
    return evaluate_risk(*args, contract_book=BOOK, **kwargs)
