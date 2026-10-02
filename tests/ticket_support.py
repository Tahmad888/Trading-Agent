"""Synthetic adapters for G4 ticket tests and the labelled CLI demo.

Everything here is fixture data, not a provider observation. Approvals made with
these helpers use channel ``automated_fixture`` and actor ``fixture:automated-test``;
they are never Taz's approval.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import os
from pathlib import Path

from desk.risk import AccountState
from desk.risk_state import RiskStateStore
from desk.tickets import MarketObservation, RiskInputs, TicketLeg, TicketRequest
from tests.conftest import NOW, TODAY
from tests.risk_support import BOOK, MARKET, REGISTRY, FixtureTerms

FIXTURE_ACTOR = "fixture:automated-test"
CALL = "AAPL 261120C00230000"


class Terms(FixtureTerms):
    """The fixed synthetic event catalogue with explicit evidence changes."""
    def __init__(self, **changes):
        self.changes = changes

    def resolve(self, event_id, at):
        return super().resolve(event_id, at).model_copy(update=self.changes)


def account(**changes) -> AccountState:
    base = AccountState(
        account_id="fixture-account", source="synthetic broker", pnl_day=TODAY, exposures=(),
        pnl_week_start=TODAY - timedelta(days=TODAY.weekday()), pnl_basis="net_liquidation_ex_cashflows",
        as_of=NOW - timedelta(seconds=5), equity=10_000, equity_high_water_mark=10_000,
        buying_power=10_000, margin_excess=10_000, pnl_today=0, pnl_this_week=0)
    return replace(base, **changes)


def observer(**changes):
    def observe(symbol, legs, now):
        data = dict(quote_as_of=NOW - timedelta(seconds=5), security_tradable=True, already_moved_pct=0.0,
                    option_spread_pct_mid=0.05, open_interest={"AAPL261120C00230000": 4000})
        data.update(changes)
        return MarketObservation(**data)
    return observe


def inputs(risk_state: RiskStateStore, *, terms=None, book=BOOK, market=MARKET, observe=None,
           registry=REGISTRY, **kw) -> RiskInputs:
    return RiskInputs(risk_state=risk_state, terms_source=terms or Terms(),
                      market=lambda now: market, contract_book=lambda now: book,
                      observe=observe or observer(), registry=registry, **kw)


def share_request(**changes) -> TicketRequest:
    data = dict(account_id="fixture-account", environment="paper", event_id="msft", structure="shares",
                legs=(TicketLeg(symbol="MSFT", limit_price=Decimal("250")),), budget_usd=Decimal("50"),
                est_costs_usd=Decimal("1"), time_stop=NOW + timedelta(days=15),
                exit_rules=("sell a third after 3 to 5 days",), quote_source="synthetic quote fixture")
    data.update(changes)
    return TicketRequest(**data)


def call_request(**changes) -> TicketRequest:
    data = dict(account_id="fixture-account", environment="paper", event_id="aapl", structure="long_call",
                legs=(TicketLeg(symbol=CALL, side="buy", limit_price=Decimal("1.50"), qty=3),),
                sizing_mode="selected_quantity", budget_usd=Decimal("100"), est_costs_usd=Decimal("3"),
                time_stop=NOW + timedelta(days=20), quote_source="synthetic quote fixture")
    data.update(changes)
    return TicketRequest(**data)


def approve(store, ticket_id, version, adapters, *, budget=None, acks=None, now=NOW):
    view = store.get(ticket_id, version, now=now)
    return store.approve(ticket_id, version,
                         budget_confirmation=budget if budget is not None else view["request"]["budget_usd"],
                         acknowledgements=view["acknowledgement_tokens"] if acks is None else acks,
                         actor=FIXTURE_ACTOR, channel="automated_fixture", inputs=adapters, now=now)


def demo_inputs() -> RiskInputs:
    """CLI demonstration only: synthetic account/evidence at the fixed fixture clock.

    Set DESK_DEMO_RISK_STATE to a scratch path. The approval typed into the CLI
    during a demo is still a fixture approval, not Taz's.
    """
    path = Path(os.environ["DESK_DEMO_RISK_STATE"])
    store = RiskStateStore(path)
    rich = account(buying_power=300_000, margin_excess=300_000, equity=300_000, equity_high_water_mark=300_000)
    store.save_snapshot(rich, "demo-snapshot", now=NOW)
    return inputs(store, clock=lambda: NOW)
