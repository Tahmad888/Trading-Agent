from datetime import datetime, timedelta, timezone

import pytest

from desk.contracts import Leg, TradeProposal
from desk.risk import AccountState

NOW = datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


@pytest.fixture
def account() -> AccountState:
    return AccountState(
        as_of=NOW - timedelta(seconds=5),
        equity=10_000,
        equity_high_water_mark=10_000,
        buying_power=10_000,
        margin_excess=10_000,
        pnl_today=0,
        pnl_this_week=0,
    )


@pytest.fixture
def proposal() -> TradeProposal:
    # A 1-lot put credit spread risking $20, inside the tier 1 budget of $25 (0.25% of $10k).
    return TradeProposal(
        proposal_id="p1",
        plan_id="plan1",
        setup_id="5_oversold_bounce",
        tier=1,
        instrument="SPY",
        structure="credit_vertical",
        legs=[Leg(symbol="SPY 260930P00500000", side="sell", qty=1, limit_price=1.20,
                  expiry=TODAY + timedelta(days=6), open_interest=2_000),
              Leg(symbol="SPY 260930P00499800", side="buy", qty=1, limit_price=1.10,
                  expiry=TODAY + timedelta(days=6), open_interest=1_500)],
        max_loss_usd=20,
        worst_case_loss_usd=20,
        max_gain_usd=10,
        est_costs_usd=2,
        sector="us_index",
        option_spread_pct_mid=0.04,
        time_stop=NOW + timedelta(days=5),
    )


@pytest.fixture
def long_call() -> TradeProposal:
    # 3 calls at $1.50: $450 of premium at risk in the worst case, $60 at the stop.
    return TradeProposal(
        proposal_id="c1",
        setup_id="1_trend_pullback",
        tier=2,
        instrument="AAPL",
        structure="long_call",
        legs=[Leg(symbol="AAPL 261120C00230000", side="buy", qty=3, limit_price=1.50,
                  expiry=TODAY + timedelta(days=57), open_interest=4_000)],
        max_loss_usd=60,
        worst_case_loss_usd=450,
        est_costs_usd=3,
        sector="tech",
        option_spread_pct_mid=0.05,
        time_stop=NOW + timedelta(days=20),
    )


@pytest.fixture
def shares() -> TradeProposal:
    # 20 shares at $250 with a $1.25 stop: $25 at the stop, $5,000 of cost.
    return TradeProposal(
        proposal_id="s1",
        setup_id="R1_18sma_daily_swing",
        tier=1,
        instrument="MSFT",
        structure="shares",
        legs=[Leg(symbol="MSFT", side="buy", qty=20, limit_price=250.0)],
        max_loss_usd=25,
        worst_case_loss_usd=5_000,
        est_costs_usd=1,
        sector="tech",
        time_stop=NOW + timedelta(days=15),
    )
