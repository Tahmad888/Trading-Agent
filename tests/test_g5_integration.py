"""G5: the combined G1-G4 (+G3a) repairs on one synthetic path. No provider requests.

Earnings configuration is broken (G1), the breakout stop is the observed session
low (G2), prices come through the vendor-history basis with an ordinary dividend
revision explained by batch action evidence (G3/G3a), and the ticket is approved
and consumed locally (G4). The approval is an automated fixture, never Taz's.
"""
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal

import pytest

from desk import scanner as sc
from desk.batch_actions import POLICY, BatchActions
from desk.earnings import UnavailableEarningsSource, qualify, scanner_source
from desk.risk_state import RiskStateStore
from desk.risk_terms import EventRiskSource
from desk.signal_state import signal_payload
from desk.tickets import TicketError, TicketLeg, TicketStore
from tests.risk_support import MARKET
from tests.test_batch_actions import KEY, Feed, dividend
from tests.test_revision_rebuild import RevisedCharts
from tests.test_vendor_basis import NOW, source
from tests.ticket_support import account, approve, inputs, observer, share_request

BREAKOUT = "1_qullamaggie_breakout"
EP = "5_qullamaggie_episodic_pivot"


class Desk:
    """One synthetic desk: vendor feed, scan log, account state and ticket store."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.native = RevisedCharts()
        self.vendor = source(tmp_path, self.native)
        bad = tmp_path / "bad-policy.json"
        bad.write_text("{not json")
        self.src = scanner_source(self.vendor, {"DESK_EARNINGS_POLICY": str(bad)}, refresh_source=self.native)
        self.log = sc.ScanLog(tmp_path / "scan")
        self.state = RiskStateStore(tmp_path / "risk.sqlite")
        self.tickets = TicketStore(tmp_path / "tickets.sqlite", approval_lifetime=timedelta(hours=1))
        self.snapshots = 0
        self.snapshot_at = None
        self.price = None

    @property
    def now(self):
        return self.native.now

    def advance(self, minutes=15):
        self.native.now += timedelta(minutes=minutes)

    def snapshot(self):
        at = self.now
        if at == self.snapshot_at:
            return
        self.snapshots += 1
        self.snapshot_at = at
        self.state.save_snapshot(account(as_of=at - timedelta(seconds=1), pnl_day=at.date(),
                                         pnl_week_start=at.date() - timedelta(days=at.weekday())),
                                 f"obs{self.snapshots}", now=at)

    def adapters(self):
        terms = EventRiskSource(self.src, self.log, lambda symbol: (symbol, self.price, self.now))
        return inputs(self.state, terms=terms, market=MARKET.model_copy(update={"as_of": self.now}),
                      observe=observer(quote_as_of=self.now - timedelta(seconds=1)))

    def ticket(self, event_id, entry):
        limit = Decimal(str(entry)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)  # buy limit in cents
        self.price = float(limit)
        self.snapshot()
        request = share_request(event_id=event_id, legs=(TicketLeg(symbol="LEAD", limit_price=limit),),
                                budget_usd=Decimal("100"), time_stop=self.now + timedelta(days=15))
        return self.tickets.prepare(request, self.adapters(), now=self.now)


def approved_breakout(desk):
    """G1-G4 up to a fixture-approved, unused ticket for the observed breakout event."""
    # G1: broken earnings configuration leaves prices working; EP evidence stays pending.
    assert isinstance(desk.src, UnavailableEarningsSource)
    rec, armed = sc.close_scan(desk.src, ["LEAD"], NOW, preparing=True)
    candidate = next(s for s in armed if s.setup_id == BREAKOUT)
    assert candidate.stop is None and candidate.stop_basis == "session_low"  # G2: no ADR-distance stop
    assert candidate.price_basis["method"] == "webull-history-v1"              # G3: no manual enrolment
    assert qualify(desk.src, replace(candidate, setup_id=EP), NOW)["status"] == "PENDING_EVIDENCE"
    assert qualify(desk.src, candidate, NOW)["status"] == "NOT_REQUIRED"
    rec.armed = [signal_payload(candidate)]
    desk.log.save_armed(NOW.date(), rec)
    first = sc.run(desk.src, ["LEAD"], desk.log, NOW)
    assert len(first.triggered) == 1
    event = desk.log.signals.get(first.triggered[0]["event_id"], NOW)
    assert event["signal"]["stop"] == 146.5                                    # G2: observed session low

    # G4: displayed, independently checked ticket; fixture approval.
    entry = event["entry_level"]
    tid, v = desk.ticket(event["id"], entry)
    view = desk.tickets.get(tid, v, now=desk.now)
    assert view["state"] == "pending", view["binding"]["failed_checks"]
    assert view["terms"]["stop"] == 146.5 and view["proposal"]["sizing_mode"] == "stop_budget"
    distance = Decimal(str(entry)).quantize(Decimal("0.01"), rounding=ROUND_CEILING) - Decimal("146.5")
    assert view["binding"]["legs"][0]["final_qty"] == int(Decimal("99") // distance)
    approve(desk.tickets, tid, v, desk.adapters(), now=desk.now)
    assert desk.tickets.get(tid, v, now=desk.now)["state"] == "approved"
    return event, tid, v


def test_combined_repairs_from_broken_earnings_to_single_use_ticket_and_revision(tmp_path):
    desk = Desk(tmp_path)
    event, tid, v = approved_breakout(desk)
    # G3a: an ordinary dividend revises daily history before the approval is used.
    cash = float(desk.native.charts[("LEAD", "D")].close.iloc[-1]) * .0025
    desk.vendor.actions = BatchActions(tmp_path / "auto.sqlite", KEY, clock_fn=lambda: desk.native.now,
                                       transport=Feed(dividends=[dividend(amount=cash)]), volume_policy=POLICY)
    desk.native.factor["LEAD"] = .9975
    desk.native.raw_anchor_unchanged = True
    desk.advance(5)
    desk.snapshot()
    with pytest.raises(TicketError, match="Recheck failed"):
        desk.tickets.consume(tid, v, request_id="g5-1", inputs=desk.adapters(), now=desk.now)
    assert desk.log.signals.get(event["id"], desk.now)["state"] == "invalidated"
    assert desk.log.signals.pending_rebuilds(NOW.date())  # the ticket recheck queued the G3a rebuild
    assert desk.tickets.get(tid, v, now=desk.now)["state"] == "approved"  # unused; cannot follow the new chart

    # The next ordinary scan rebuilds from fresh history; the old crossing is not replayed.
    desk.advance(10)
    rebuilt = sc.run(desk.src, ["LEAD"], desk.log, desk.now)
    assert rebuilt.discovery["revision_rebuilds"][0]["status"] == "REBUILT" and not rebuilt.triggered
    desk.advance()
    assert not sc.run(desk.src, ["LEAD"], desk.log, desk.now).triggered
    desk.advance()
    later = sc.run(desk.src, ["LEAD"], desk.log, desk.now)
    assert len(later.triggered) == 1 and later.triggered[0]["event_id"] != event["id"]
    new_event = desk.log.signals.get(later.triggered[0]["event_id"], desk.now)
    assert new_event["signal"]["stop"] == pytest.approx(146.5 * .9975)

    # The old approval never transfers to the rebuilt event; the new one needs its own.
    desk.snapshot()
    with pytest.raises(TicketError):
        desk.tickets.consume(tid, v, request_id="g5-1", inputs=desk.adapters(), now=desk.now)
    tid2, v2 = desk.ticket(new_event["id"], new_event["entry_level"])
    assert desk.tickets.get(tid2, v2, now=desk.now)["state"] == "pending"
    with pytest.raises(TicketError, match="no usable approval"):
        desk.tickets.consume(tid2, v2, request_id="g5-2", inputs=desk.adapters(), now=desk.now)
    approve(desk.tickets, tid2, v2, desk.adapters(), now=desk.now)
    result = desk.tickets.consume(tid2, v2, request_id="g5-2", inputs=desk.adapters(), now=desk.now)
    assert result["order_submitted"] is False and result["broker_action"] == "none"
    assert desk.tickets.get(tid2, v2, now=desk.now)["state"] == "consumed"


def test_unexplained_ex_dividend_revision_blocks_the_approved_ticket_without_rebuild(tmp_path):
    desk = Desk(tmp_path)
    event, tid, v = approved_breakout(desk)
    # Same daily revision, but no batch action evidence is configured (the G3a default).
    desk.native.factor["LEAD"] = .9975
    desk.native.raw_anchor_unchanged = True
    desk.advance(5)
    desk.snapshot()
    with pytest.raises(TicketError, match="Recheck failed"):
        desk.tickets.consume(tid, v, request_id="g5-3", inputs=desk.adapters(), now=desk.now)
    assert not desk.log.signals.get(event["id"], desk.now)["eligible"]
    desk.advance(10)
    result = sc.run(desk.src, ["LEAD"], desk.log, desk.now)
    assert "DAILY_RAW_CLOSE_MISMATCH" in result.skipped["LEAD"]
    assert "revision_rebuilds" not in result.discovery and not result.triggered
    desk.snapshot()
    with pytest.raises(TicketError):
        desk.tickets.consume(tid, v, request_id="g5-3", inputs=desk.adapters(), now=desk.now)
    assert desk.tickets.get(tid, v, now=desk.now)["state"] != "consumed"
