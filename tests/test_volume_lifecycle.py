"""G5a checkpoint 2, sections 10.6 and 10.7: saved volume qualification through signals and tickets.

Synthetic Webull charts and a synthetic Alpaca provider (labelled fixtures). Each
"process" (scan, ticket prepare, approve, consume) gets a fresh provider over the
same cache file, as separate command runs would. Approvals are automated fixtures.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from desk import scanner as sc
from desk.alpaca_source import configured_volume
from desk.playbook.filters import GateResult
from desk.risk_state import RiskStateStore
from desk.risk_terms import EventRiskSource
from desk.signal_state import candidate_id
from desk.tickets import TicketError, TicketLeg, TicketStore
from tests.alpaca_support import AlpacaSim, Clock, asset_id, asset_row, dry_daily, with_volume
from tests.risk_support import MARKET
from tests.test_scanner import ET, Fake, daily, frames, m15
from tests.test_triggers import CUP
from tests.ticket_support import account, approve, inputs, observer, share_request

DAY0, ENTRY = date(2026, 9, 29), date(2026, 9, 30)
VCP = "2_minervini_vcp"
CROSS = [(148.0, 148.5, 147.5, 148.3), (148.3, 149.5, 148.2, 149.2)]   # crosses the VCP pivot 148.74


def at(hour=10, minute=0):
    return datetime(2026, 9, 30, hour, minute, tzinfo=ET)


@pytest.fixture(autouse=True)
def template_passes(monkeypatch):
    # The Trend Template is not under test here; volume qualification is.
    monkeypatch.setattr(sc, "trend_template", lambda f, spy: GateResult({"fixture template": True}))


class Desk:
    """Webull fixture charts, one Alpaca simulator and the persistent desk files."""

    def __init__(self, tmp_path, symbols=("LEAD",), **sim):
        self.tmp = tmp_path
        self.frames = frames(DAY0)
        for s in symbols:
            self.frames[(s, "D")] = daily(CUP, end=DAY0)
            self.frames[(s, "M15")] = m15(CROSS, day=ENTRY)
        self.sim = AlpacaSim(symbols=[*symbols, "SPY", "QQQ", "IWM"], daily=dry_daily(DAY0, days=12), **sim)
        self.clock = Clock(at(9, 45))
        self.log = sc.ScanLog(tmp_path / "scan")
        self.state = RiskStateStore(tmp_path / "risk.sqlite")
        self.tickets = TicketStore(tmp_path / "tickets.sqlite", approval_lifetime=timedelta(hours=1))
        self.price = 149.0
        self.metadata = {s: Fake({}).security_metadata([s])[0] for s in symbols}

    def source(self, *, budget=6):
        """A new process: fresh provider and budget over the persistent cache."""
        return with_volume(Fake(self.frames), self.tmp / "volume.sqlite", self.sim, self.clock, budget=budget)

    def restart(self):
        self.log = sc.ScanLog(self.tmp / "scan")
        self.tickets = TicketStore(self.tmp / "tickets.sqlite", approval_lifetime=timedelta(hours=1))

    def arm(self, names=("LEAD",)):
        rec, armed = sc.close_scan(self.source(), list(names), self.clock.at, preparing=True)
        vcp = [s for s in armed if s.setup_id == VCP]
        self.log.add_armed(ENTRY, sc.MarketSize(rec.market), vcp)
        return rec, vcp

    def trigger(self, signals):
        self.clock.at = at(10, 0)
        rec = sc.intraday_scan(self.source(), signals, self.clock.at, store=self.log.signals)
        return rec

    def snapshot(self):
        now = self.clock.at
        if getattr(self, "_snap", None) != now:
            self._snap = now
            self.state.save_snapshot(account(as_of=now - timedelta(seconds=5), pnl_day=now.date(),
                                             pnl_week_start=now.date() - timedelta(days=now.weekday())),
                                     f"obs-{now.isoformat()}", now=now)

    def adapters(self, source=None):
        now = self.clock.at
        self.snapshot()
        terms = EventRiskSource(source or self.source(), self.log, lambda s: (s, self.price, now))
        return inputs(self.state, terms=terms, market=MARKET.model_copy(update={"as_of": now}),
                      observe=observer(quote_as_of=now - timedelta(seconds=1)))

    def ticket(self, event_id):
        now = self.clock.at
        request = share_request(event_id=event_id, legs=(TicketLeg(symbol="LEAD", limit_price=Decimal("149.00")),),
                                budget_usd=Decimal("100"), time_stop=now + timedelta(days=15))
        return self.tickets.prepare(request, self.adapters(), now=now)

    def approved(self):
        _, vcp = self.arm()
        rec = self.trigger(vcp)
        assert len(rec.triggered) == 1, rec.skipped
        event = self.log.signals.get(rec.triggered[0]["event_id"], self.clock.at)
        tid, v = self.ticket(event["id"])
        assert self.tickets.get(tid, v, now=self.clock.at)["state"] == "pending"
        approve(self.tickets, tid, v, self.adapters(), now=self.clock.at)
        return vcp[0], event, tid, v

    def advance(self, minutes=1):
        self.clock.at += timedelta(minutes=minutes)


def test_alpaca_qualified_vcp_reaches_a_single_use_ticket(tmp_path):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    assert event["signal"]["volume_evidence"]["consumer"] == "2_minervini_vcp:dry_volume"
    assert event["signal"]["volume_evidence"]["identity"]["alpaca_asset_id"] == asset_id("LEAD")
    desk.advance()
    result = desk.tickets.consume(tid, v, request_id="vol-1", inputs=desk.adapters(), now=desk.clock.at)
    assert result["order_submitted"] is False and result["broker_action"] == "none"


@pytest.mark.parametrize("status,code", [(500, "HTTP_FAILURE"), (401, "AUTH_OR_ENTITLEMENT_FAILURE"),
                                         (403, "AUTH_OR_ENTITLEMENT_FAILURE"), (429, "RATE_LIMITED")])
def test_failed_refresh_blocks_prepare_approve_consume_and_survives_restart(tmp_path, status, code):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    desk.advance()
    # One failed refresh: the asset list answers, the bar refetch fails.
    failing = desk.source()
    desk.sim.bar_status = [status] * 20
    with pytest.raises(TicketError, match="Recheck failed"):
        desk.tickets.consume(tid, v, request_id="vol-2", inputs=desk.adapters(failing), now=desk.clock.at)
    assert not desk.log.signals.get(event["id"], desk.clock.at)["eligible"]
    blocked, bv = desk.ticket(event["id"])
    assert desk.tickets.get(blocked, bv, now=desk.clock.at)["state"] == "blocked"
    with pytest.raises(TicketError):
        approve(desk.tickets, blocked, bv, desk.adapters(), now=desk.clock.at)
    # Restart: the persisted failure keeps the cache-only final fence closed with no request.
    desk.restart()
    sent = desk.sim.calls()
    state, why = sc.volume_status(desk.source(), signal, desk.clock.at, refresh=False)
    assert state == "UNAVAILABLE" and code in why
    with EventRiskSource(desk.source(), desk.log, lambda s: (s, 149.0, desk.clock.at)).held_event(event["id"]) as status_at:
        assert status_at(desk.clock.at).eligible is False
    assert desk.sim.calls() == sent
    with pytest.raises(TicketError):
        desk.tickets.consume(tid, v, request_id="vol-2", inputs=desk.adapters(), now=desk.clock.at)
    assert desk.tickets.get(tid, v, now=desk.clock.at)["state"] == "approved"   # never spent


def test_stop_inside_a_run_blocks_every_later_volume_request(tmp_path):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    desk.advance()
    run = desk.source()
    desk.sim.bar_status = [429]
    assert run.decision_volume.gate(signal.volume_evidence, desk.clock.at, refresh=True,
                                    metadata=desk.metadata) == ("UNAVAILABLE", "RATE_LIMITED")
    sent = desk.sim.calls()
    state, code = run.decision_volume.gate(signal.volume_evidence, desk.clock.at, refresh=False)
    assert (state, desk.sim.calls()) == ("UNAVAILABLE", sent) and "RATE_LIMITED" in code


def test_malformed_ticker_is_isolated_while_healthy_tickers_qualify(tmp_path):
    desk = Desk(tmp_path, symbols=("LEAD", "BAD"), malformed={"BAD"})
    rec, vcp = desk.arm(("LEAD", "BAD"))
    assert [s.symbol for s in vcp] == ["LEAD"]
    assert "NONFINITE_OR_NEGATIVE_VOLUME" in rec.skipped["BAD/2_minervini_vcp"]
    assert desk.sim.calls("bars") == 1                     # one batch; the bad ticker did not fail it


def test_unchanged_refresh_and_unrelated_changes_keep_terms(tmp_path):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    before = desk.log.signals.get(event["id"], desk.clock.at)["terms_digest"]
    # A renamed asset and a revised bar for another ticker are not this event's dependency.
    desk.sim.assets = [asset_row("LEAD", name="Lead Renamed Corp"), asset_row("SPY"), asset_row("QQQ"),
                       asset_row("IWM")]
    original = desk.sim.daily
    desk.sim.daily = lambda s, d: original(s, d) + (7 if s == "SPY" else 0)
    desk.advance()
    result = desk.tickets.consume(tid, v, request_id="vol-3", inputs=desk.adapters(), now=desk.clock.at)
    assert result["replay"] is False
    after = desk.log.signals.get(event["id"], desk.clock.at)
    assert after["terms_digest"] == before
    # Re-arming identical evidence (new receipts) is the same candidate, not new terms.
    desk.advance()
    _, again = desk.arm()
    assert candidate_id(again[0], ENTRY) == candidate_id(signal, ENTRY)
    assert again[0].volume_evidence["dependency_digest"] == signal.volume_evidence["dependency_digest"]
    assert again[0].volume_evidence["audit"]["inputs"][0]["received_at"] != \
        signal.volume_evidence["audit"]["inputs"][0]["received_at"]


def test_revised_used_volume_invalidates_queues_rebuild_and_never_revives_the_approval(tmp_path):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    original = desk.sim.daily
    desk.sim.daily = lambda s, d: original(s, d) + (1 if (s, d) == ("LEAD", DAY0) else 0)
    desk.advance()
    with pytest.raises(TicketError, match="Recheck failed"):
        desk.tickets.consume(tid, v, request_id="vol-4", inputs=desk.adapters(), now=desk.clock.at)
    assert desk.log.signals.get(event["id"], desk.clock.at)["state"] == "invalidated"
    pending = desk.log.signals.pending_rebuilds(ENTRY)
    assert [(r["symbol"], r["setup_id"]) for r in pending] == [("LEAD", VCP)]
    assert "VOLUME_EVIDENCE_REVISED" in pending[0]["reason"]
    # The provider reverts: an unchanged-looking refresh cannot revive the retired event or approval.
    desk.sim.daily = original
    desk.advance()
    with pytest.raises(TicketError):
        desk.tickets.consume(tid, v, request_id="vol-4", inputs=desk.adapters(), now=desk.clock.at)
    assert desk.tickets.get(tid, v, now=desk.clock.at)["state"] == "approved"
    assert desk.log.signals.get(event["id"], desk.clock.at)["state"] == "invalidated"


def test_changed_identity_requalifies(tmp_path):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    desk.sim.assets = [asset_row("LEAD", id=asset_id("LEAD", "relisted")), asset_row("SPY"), asset_row("QQQ"),
                       asset_row("IWM")]
    desk.advance()
    with pytest.raises(TicketError, match="Recheck failed"):
        desk.tickets.consume(tid, v, request_id="vol-5", inputs=desk.adapters(), now=desk.clock.at)
    assert "IDENTITY_CHANGED" in desk.log.signals.pending_rebuilds(ENTRY)[0]["reason"]
    # The rebuild cannot qualify across the change: windows before it are unsupported.
    _, rebuilt = desk.arm()
    assert rebuilt == []


def test_source_change_requalifies_and_price_only_signals_are_untouched(tmp_path):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    desk.advance()
    webull_only = configured_volume(Fake(desk.frames), {})          # Alpaca no longer configured
    assert webull_only.__class__ is Fake
    assert sc.volume_status(webull_only, signal, desk.clock.at, refresh=False) == ("CHANGED", "VOLUME_SOURCE_CHANGED")
    with pytest.raises(TicketError, match="Recheck failed"):
        desk.tickets.consume(tid, v, request_id="vol-6", inputs=desk.adapters(webull_only), now=desk.clock.at)
    assert "VOLUME_SOURCE_CHANGED" in desk.log.signals.pending_rebuilds(ENTRY)[0]["reason"]
    # A price-only setup carries no volume dependency under either configuration.
    from tests.test_scanner import sig
    price_only = sig()
    assert sc.volume_status(desk.source(), price_only, desk.clock.at, refresh=False) == ("OK", None)
    assert sc.volume_status(webull_only, price_only, desk.clock.at, refresh=False) == ("OK", None)


def test_share_basis_and_rule_changes_requalify(tmp_path, monkeypatch):
    desk = Desk(tmp_path)
    _, vcp = desk.arm()
    evidence = vcp[0].volume_evidence
    gate = desk.source().decision_volume.gate
    raw = {**evidence, "inputs": [{**evidence["inputs"][0], "share_basis_id": "alpaca:sip:adjustment=raw"}]}
    assert gate(raw, desk.clock.at, refresh=False) == ("CHANGED", "SHARE_BASIS_CHANGED")
    iex = {**evidence, "inputs": [{**evidence["inputs"][0], "feed": "iex"}]}
    assert gate(iex, desk.clock.at, refresh=False) == ("CHANGED", "VOLUME_SOURCE_CHANGED")
    from desk.playbook.cards import CARDS
    card = CARDS[VCP]
    monkeypatch.setattr(type(card), "fingerprint", lambda self: "changed-card")
    assert gate(evidence, desk.clock.at, refresh=False) == ("CHANGED", "VOLUME_RULE_CHANGED_REQUALIFY")


def test_revoked_or_consumed_tickets_stay_dead_after_an_unchanged_refresh(tmp_path):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    desk.advance()
    desk.tickets.consume(tid, v, request_id="vol-7", inputs=desk.adapters(), now=desk.clock.at)
    desk.advance()
    with pytest.raises(TicketError, match="already consumed"):
        desk.tickets.consume(tid, v, request_id="vol-8", inputs=desk.adapters(), now=desk.clock.at)
    tid2, v2 = desk.ticket(event["id"])
    approve(desk.tickets, tid2, v2, desk.adapters(), now=desk.clock.at)
    desk.tickets.revoke(tid2, v2, actor="fixture:automated-test", reason="fixture revoke",
                        channel="automated_fixture", now=desk.clock.at)
    desk.advance()
    with pytest.raises(TicketError):
        desk.tickets.consume(tid2, v2, request_id="vol-9", inputs=desk.adapters(), now=desk.clock.at)
    assert desk.tickets.get(tid2, v2, now=desk.clock.at)["state"] == "revoked"


def test_cache_gate_runs_before_trigger_observation_without_requests(tmp_path):
    desk = Desk(tmp_path)
    _, vcp = desk.arm()
    sent = desk.sim.calls()
    rec = desk.trigger(vcp)
    assert len(rec.triggered) == 1 and desk.sim.calls() == sent       # observation used the cache only
    # A persisted failure for the evidence's request key suspends observation.
    desk.sim.bar_status = [500]
    assert desk.source().decision_volume.gate(vcp[0].volume_evidence, desk.clock.at, refresh=True,
                                              metadata=desk.metadata) == ("UNAVAILABLE", "HTTP_FAILURE")
    desk.advance(15)
    rec = sc.intraday_scan(desk.source(), vcp, desk.clock.at, store=desk.log.signals)
    assert "HTTP_FAILURE" in rec.skipped["LEAD"] and not rec.triggered
