"""Taz's decisions of 2026-10-02 on the audit questions (User policy).

1. Price increments: an invalid limit is refused when the ticket is prepared,
   never rounded. Shares: whole cents at $1.00 and above, $0.0001 below (SEC Rule
   612). Single-leg options: the class schedule (Cboe Rule 5.4(a)); multi-leg
   option tickets: whole cents (Cboe Rule 5.33(f)(1)).
2. Loss/drawdown warnings: re-asked when new, when the threshold changes, or when
   they worsen from the acknowledged value by 10% of the threshold or more
   (cumulative; the baseline never resets). Improvements update the display.
"""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from pydantic import ValidationError
import pytest

from desk.instruments import ContractBook
from desk.risk_state import RiskStateStore
from desk.risk import RiskLimits
from desk.tickets import TicketError, TicketLeg, TicketStore
from tests.conftest import NOW
from tests.risk_support import BOOK
from tests.ticket_support import CALL, account, approve, call_request, inputs, share_request


@pytest.fixture
def risk_state(tmp_path):
    state = RiskStateStore(tmp_path / "risk.sqlite")
    state.save_snapshot(account(buying_power=300_000, margin_excess=300_000, equity=300_000,
                                equity_high_water_mark=300_000), "obs1", now=NOW)
    return state


@pytest.fixture
def store(tmp_path):
    return TicketStore(tmp_path / "tickets.sqlite")


# ---- Decision 1: price increments --------------------------------------------------
@pytest.mark.parametrize("limit", ["250.001", "250.0049", "1.005", "1.0001"])
def test_sub_penny_share_limit_at_or_above_one_dollar_is_refused(limit):
    with pytest.raises(ValidationError, match="SEC Rule 612.*does not round"):
        share_request(legs=(TicketLeg(symbol="MSFT", limit_price=Decimal(limit)),))


@pytest.mark.parametrize("limit", ["250", "250.5", "250.01", "1.00", "0.9999", "0.5", "0.0001"])
def test_valid_share_increments_are_accepted(limit):
    request = share_request(legs=(TicketLeg(symbol="MSFT", limit_price=Decimal(limit)),))
    assert request.legs[0].limit_price == Decimal(limit)  # kept exactly, not rounded


def test_share_limit_below_one_dollar_finer_than_a_hundredth_of_a_cent_is_refused():
    with pytest.raises(ValidationError, match="SEC Rule 612"):
        share_request(legs=(TicketLeg(symbol="MSFT", limit_price=Decimal("0.99995")),))


@pytest.mark.parametrize("limit", ["1.505", "0.001"])
def test_option_limits_finer_than_a_cent_are_refused(limit):
    with pytest.raises(ValidationError, match="whole cents or coarser for options"):
        call_request(legs=(TicketLeg(symbol=CALL, side="buy", limit_price=Decimal(limit), qty=3),))


def _book(increment):
    return ContractBook(source=BOOK.source, as_of=BOOK.as_of, contracts=tuple(
        c.model_copy(update={"price_increment": increment}) for c in BOOK.contracts))


@pytest.mark.parametrize("increment,limit,ok", [
    ("penny", "1.51", True), ("penny", "2.99", True), ("penny", "3.05", True), ("penny", "3.01", False),
    ("standard", "1.55", True), ("standard", "1.51", False), ("standard", "3.10", True),
    ("standard", "3.05", False), ("penny_all_prices", "3.01", True), ("penny_all_prices", "12.37", True),
])
def test_single_leg_option_limit_follows_its_class_schedule(store, risk_state, increment, limit, ok):
    adapters = inputs(risk_state, book=_book(increment))
    request = call_request(legs=(TicketLeg(symbol=CALL, side="buy", limit_price=Decimal(limit), qty=1),),
                           budget_usd=Decimal("2000"))
    tid, v = store.prepare(request, adapters, now=NOW)
    problems = store.history(tid)[-1]["payload"]["problems"]
    increment_problems = [p for p in problems if "minimum increment" in p]
    assert bool(increment_problems) is not ok, problems
    if not ok:
        assert store.get(tid, v, now=NOW)["state"] == "blocked"
        assert "does not round" in increment_problems[0] and "Cboe Rule 5.4(a)" in increment_problems[0]


def test_unknown_option_increment_class_blocks_the_ticket(store, risk_state):
    adapters = inputs(risk_state, book=_book(None))
    tid, v = store.prepare(call_request(), adapters, now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "blocked"
    assert any("price increment class" in p and "unavailable" in p
               for p in store.history(tid)[-1]["payload"]["problems"])


def test_known_increment_class_with_valid_limit_is_pending(store, risk_state):
    tid, v = store.prepare(call_request(), inputs(risk_state), now=NOW)
    assert store.get(tid, v, now=NOW)["state"] == "pending"


# ---- Decision 2: warning band ------------------------------------------------------
RICH = dict(buying_power=300_000, margin_excess=300_000)
_tick = iter(range(1, 10_000))


def _snapshot(risk_state, **changes):
    """A newer routine snapshot (as of NOW minus a few seconds, fresh for the checks)."""
    n = next(_tick)
    risk_state.save_snapshot(account(as_of=NOW - timedelta(seconds=5) + timedelta(microseconds=n), **RICH,
                                     **changes), f"band-{n}", now=NOW)


def _loss(usd):
    return dict(pnl_today=-usd, pnl_this_week=-usd)


@pytest.fixture
def banded(tmp_path):
    def make(**changes):
        state = RiskStateStore(tmp_path / f"risk-{next(_tick)}.sqlite")
        state.save_snapshot(account(**RICH, **changes), "obs1", now=NOW)
        tickets = TicketStore(tmp_path / f"tickets-{next(_tick)}.sqlite")
        adapters = inputs(state)
        tid, v = tickets.prepare(share_request(), adapters, now=NOW)
        return state, tickets, adapters, tid, v
    return make


def _consume(tickets, tid, v, adapters):
    return tickets.consume(tid, v, request_id="r", inputs=adapters, now=NOW)


@pytest.mark.parametrize("later,ok", [(265, True), (269.99, True), (270, False), (300, False)])
def test_daily_loss_band_is_ten_percent_of_its_threshold(banded, later, ok):
    state, tickets, adapters, tid, v = banded(**_loss(250))
    assert [w["code"] for w in tickets.get(tid, v, now=NOW)["binding"]["warnings"]] == ["daily_loss"]
    approve(tickets, tid, v, adapters)
    _snapshot(state, **_loss(later))
    if ok:
        assert _consume(tickets, tid, v, adapters)["order_submitted"] is False
        assert tickets.history(tid)[-1]["payload"]["warning_values"] == {"daily_loss": later}
    else:
        with pytest.raises(TicketError, match="daily_loss worsened from \\$250.00 against \\$200.00 to"):
            _consume(tickets, tid, v, adapters)


def test_deterioration_is_measured_from_the_acknowledged_value_not_the_last_update(banded):
    state, tickets, adapters, tid, v = banded(**_loss(250))
    _snapshot(state, **_loss(265))                  # within the band at approval
    approve(tickets, tid, v, adapters)
    _snapshot(state, **_loss(270))                  # only $5 since approval, $20 since acknowledged
    with pytest.raises(TicketError, match="daily_loss worsened"):
        _consume(tickets, tid, v, adapters)


def test_weekly_loss_band_is_forty_dollars_at_the_default(banded):
    state, tickets, adapters, tid, v = banded(pnl_today=0, pnl_this_week=-450)
    approve(tickets, tid, v, adapters)
    _snapshot(state, pnl_today=0, pnl_this_week=-489)
    assert _consume(tickets, tid, v, adapters)["replay"] is False
    state2, tickets2, adapters2, tid2, v2 = banded(pnl_today=0, pnl_this_week=-450)
    approve(tickets2, tid2, v2, adapters2)
    _snapshot(state2, pnl_today=0, pnl_this_week=-490)
    with pytest.raises(TicketError, match="weekly_loss worsened"):
        _consume(tickets2, tid2, v2, adapters2)


@pytest.mark.parametrize("equity,ok", [(8_810, True), (8_800, False)])
def test_drawdown_band_is_one_percentage_point_at_the_default(banded, equity, ok):
    state, tickets, adapters, tid, v = banded(equity=8_900, equity_high_water_mark=10_000)
    assert tickets.get(tid, v, now=NOW)["binding"]["warnings"][0]["code"] == "account_drawdown"
    approve(tickets, tid, v, adapters)
    _snapshot(state, equity=equity, equity_high_water_mark=10_000)
    if ok:
        _consume(tickets, tid, v, adapters)
    else:
        with pytest.raises(TicketError, match="account_drawdown worsened from 11.00% against 10.00% to 12.00%"):
            _consume(tickets, tid, v, adapters)


def test_improvement_does_not_re_ask_and_updates_the_display(banded):
    state, tickets, adapters, tid, v = banded(**_loss(250))
    _snapshot(state, **_loss(230))
    approve(tickets, tid, v, adapters)
    text = tickets.display(tid, v, now=NOW)
    assert "Value $250.00 against $200.00" in text            # what was acknowledged
    assert "latest final check" in text and "$230.00 against $200.00" in text
    _snapshot(state, **_loss(50))                            # the warning clears entirely
    _consume(tickets, tid, v, adapters)
    assert tickets.history(tid)[-1]["payload"]["warning_values"] == {}
    assert "below its threshold" in tickets.display(tid, v, now=NOW)


def test_a_new_loss_warning_is_re_asked(banded):
    state, tickets, adapters, tid, v = banded(**_loss(150))
    approve(tickets, tid, v, adapters)
    _snapshot(state, **_loss(200))
    with pytest.raises(TicketError, match="new warning daily_loss"):
        _consume(tickets, tid, v, adapters)


def test_a_changed_threshold_is_re_asked(banded):
    state, tickets, adapters, tid, v = banded(**_loss(250))
    approve(tickets, tid, v, adapters)
    stricter = replace(adapters, limits=RiskLimits(daily_loss_usd=240))
    with pytest.raises(TicketError, match="warning policy changed since this version was prepared: daily loss \\$240.00"):
        _consume(tickets, tid, v, stricter)


def test_a_new_warning_before_approval_is_re_asked(banded):
    state, tickets, adapters, tid, v = banded(**_loss(0))
    _snapshot(state, equity=8_900, equity_high_water_mark=10_000)
    with pytest.raises(TicketError, match="new warning account_drawdown"):
        approve(tickets, tid, v, adapters)


# ---- Decision 3: EP chase measured from the frozen opening-range high --------------
def test_ep_chase_reference_is_the_frozen_opening_range_high(tmp_path):
    from dataclasses import replace as dc_replace
    from desk import scanner as sc
    from desk.risk_terms import chase_reference
    from desk.signal_state import SignalStore
    from tests.test_scanner import m15, sig
    from tests.test_signal_lifecycle import BASE, now
    for setup, expected in [("5_qullamaggie_episodic_pivot", "entry_level"), ("1_qullamaggie_breakout", "trigger")]:
        store = SignalStore(tmp_path / f"{setup}.db")
        event = sc.observe_signal(store, dc_replace(sig(setup), stop=None, stop_basis="session_low", adr_pct=4),
                                  m15(BASE), now())[0]
        assert event["candidate_signal"]["trigger"] == 100 and event["entry_level"] == 101
        reference = chase_reference(event)
        assert reference == (event["entry_level"] if expected == "entry_level" else event["candidate_signal"]["trigger"])


@pytest.mark.parametrize("limit,ok", [(250, True), (257.5, True), (257.51, False)])
def test_ep_limit_within_three_percent_of_the_range_high_passes_even_far_above_the_open(shares, account,
                                                                                         limit, ok):
    """ORH $250 is 5% above a $238.10 open. Measured from the open every limit failed;
    measured from the frozen ORH the executable limit may sit up to 3% above it."""
    from tests.risk_support import FixtureTerms
    from tests.test_gap_stop_risk import risk
    from tests.test_risk import failed

    class EP(FixtureTerms):
        def resolve(self, event_id, at):
            return super().resolve(event_id, at).model_copy(update={
                "entry_level": 250, "chase_reference": 250, "underlying_price": limit})
    p = shares.model_copy(update={"max_loss_usd": None,
                                  "legs": [shares.legs[0].model_copy(update={"limit_price": limit})]})
    result = risk(p, account, terms_source=EP())
    assert ("executable_entry" not in failed(result)) is ok, failed(result)
    if ok:
        old = FixtureTerms.resolve(EP(), p.event_id, NOW).model_copy(update={"chase_reference": 238.10})

        class Open(FixtureTerms):
            def resolve(self, event_id, at):
                return old.model_copy(update={"underlying_price": limit})
        assert "executable_entry" in failed(risk(p, account, terms_source=Open()))  # the open-based rule


@pytest.mark.parametrize("price,chased", [(105, False), (107.12, False), (107.13, True)])
def test_ep_revalidation_measures_the_chase_from_the_frozen_range_high(tmp_path, price, chased):
    from dataclasses import replace as dc_replace
    from desk import scanner as sc
    from tests.test_scanner import Fake, m15, sig
    from tests.test_signal_lifecycle import now
    log = sc.ScanLog(tmp_path)
    rows = [(100, 104, 99.5, 103), (103, 105, 102.5, 104.5)]   # ORH 104 is 4% above the $100 open
    candidate = dc_replace(sig("5_qullamaggie_episodic_pivot"), stop=None, stop_basis="session_low", adr_pct=10)
    event = sc.observe_signal(log.signals, candidate, m15(rows), now())[0]
    assert event["candidate_signal"]["trigger"] == 100 and event["entry_level"] == 104
    result = sc.revalidate_signal(Fake({("LEAD", "M15"): m15(rows)}), log, event["id"], now(),
                                  symbol="LEAD", price=price, quote_at=now())
    assert ("price exceeds the existing chase limit" in result["reasons"]) is chased, result["reasons"]


# ---- Astra's re-audit of 5e24e02 (P2): a relaxed threshold cannot clear a warning ----
RELAXED = {  # warning -> (account at prepare, relaxed limits); synthetic, not recommendations
    "daily_loss": (_loss(250), RiskLimits(daily_loss_usd=500)),
    "weekly_loss": (dict(pnl_today=0, pnl_this_week=-450), RiskLimits(weekly_loss_usd=1000)),
    "account_drawdown": (dict(equity=8_900, equity_high_water_mark=10_000), RiskLimits(kill_switch_drawdown_pct=0.20)),
}
IMPROVED = {"daily_loss": _loss(50), "weekly_loss": dict(pnl_today=0, pnl_this_week=-100),
            "account_drawdown": dict(equity=9_500, equity_high_water_mark=10_000)}


@pytest.mark.parametrize("code", sorted(RELAXED))
@pytest.mark.parametrize("step", ["approve", "consume"])
def test_relaxing_a_threshold_that_clears_the_warning_is_re_asked(banded, code, step):
    account_changes, relaxed = RELAXED[code]
    state, tickets, adapters, tid, v = banded(**account_changes)
    assert [w["code"] for w in tickets.get(tid, v, now=NOW)["binding"]["warnings"]] == [code]
    looser = replace(adapters, limits=relaxed)
    if step == "consume":
        approve(tickets, tid, v, adapters)
        with pytest.raises(TicketError, match="warning policy changed"):
            _consume(tickets, tid, v, looser)
        assert tickets.get(tid, v, now=NOW)["state"] == "approved"
    else:
        with pytest.raises(TicketError, match="warning policy changed"):
            approve(tickets, tid, v, looser)
        assert tickets.get(tid, v, now=NOW)["state"] == "pending"


@pytest.mark.parametrize("code", sorted(IMPROVED))
def test_a_warning_cleared_by_real_improvement_still_consumes(banded, code):
    state, tickets, adapters, tid, v = banded(**RELAXED[code][0])
    approve(tickets, tid, v, adapters)
    _snapshot(state, **IMPROVED[code])
    assert _consume(tickets, tid, v, adapters)["order_submitted"] is False
    assert tickets.history(tid)[-1]["payload"]["warning_values"] == {}


def test_policy_change_refuses_even_a_ticket_without_warnings(banded):
    """Engineering choice (fail closed): the whole account-warning policy is bound,
    not only the thresholds of warnings that happened to be showing."""
    state, tickets, adapters, tid, v = banded()
    approve(tickets, tid, v, adapters)
    with pytest.raises(TicketError, match="warning policy changed"):
        _consume(tickets, tid, v, replace(adapters, limits=RiskLimits(daily_loss_usd=150)))


def test_ticket_without_a_policy_snapshot_is_not_assumed_unchanged(banded, tmp_path):
    """Migration: a ticket prepared before the snapshot existed cannot be approved."""
    import json
    import sqlite3
    from contextlib import closing
    from desk.tickets import _sha
    state, tickets, adapters, tid, v = banded(**_loss(250))
    with closing(sqlite3.connect(tickets.path)) as db, db:
        stored = json.loads(db.execute("SELECT binding FROM tickets").fetchone()[0])
        stored.pop("warning_policy")
        db.execute("UPDATE tickets SET binding=?, binding_sha256=?",
                   (json.dumps(stored, sort_keys=True, separators=(",", ":")), _sha(stored)))
    with pytest.raises(TicketError, match="predates the warning-policy snapshot"):
        approve(tickets, tid, v, adapters)


def test_ticket_shows_the_warning_levels_it_was_checked_against(banded):
    state, tickets, adapters, tid, v = banded(**_loss(250))
    text = tickets.display(tid, v, now=NOW)
    assert "Warning levels: daily loss $200.00, weekly loss $400.00, drawdown 10.00%" in text
