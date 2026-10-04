"""G5a checkpoint 2 audit repairs (Astra, 2026-10-04): F1 identity health, F2 final guard.

Synthetic Webull charts and a synthetic Alpaca transport (labelled fixtures, no
network). Every case runs the production scanner, provider, risk adapter and ticket
methods; hooks only choose *when* a real production writer runs.
"""
from contextlib import closing
from datetime import timedelta
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from desk import scanner as sc
from desk import tickets as tk
from desk.alpaca_assets import SOURCE_SCOPE, IdentityStore, symbol_scope
from desk.alpaca_source import AlpacaVolumeProvider
from desk.alpaca_volume import NETWORK_FORBIDDEN, BarRequest, VolumeCache
from desk.calendar import latest_closed_session
from desk.playbook.filters import GateResult
from desk.risk_terms import EventRiskSource
from desk.security import SecurityMetadata
from desk.signal_state import candidate_id
from desk.tickets import TicketError
from tests.alpaca_support import asset_id, asset_row
from tests.test_volume_lifecycle import DAY0, ENTRY, Desk
from tests.ticket_support import approve

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def template_passes(monkeypatch):
    monkeypatch.setattr(sc, "trend_template", lambda f, spy: GateResult({"fixture template": True}))


def healthy_assets(*extra):
    return [asset_row(s) for s in ("LEAD", "SPY", "QQQ", "IWM", *extra)]


# Each identity failure the audit reproduced, applied to the synthetic provider.
FAILURES = {
    "not_found": (lambda d: setattr(d.sim, "assets", [asset_row(s) for s in ("SPY", "QQQ", "IWM")]),
                  "ASSET_NOT_FOUND"),
    "ambiguous": (lambda d: setattr(d.sim, "assets", healthy_assets() + [asset_row("LEAD", id=asset_id("LEAD", "2"))]),
                  "ASSET_AMBIGUOUS"),
    "inactive": (lambda d: setattr(d.sim, "assets", [asset_row("LEAD", status="inactive"), *healthy_assets()[1:]]),
                 "ASSET_UNSUPPORTED"),
    "asset_list_500": (lambda d: setattr(d.sim, "status", [500]), "IDENTITY_SOURCE_FAILED:HTTP_FAILURE"),
}


def observe_identity(desk, symbols=("LEAD",), *, metadata=True, source=None):
    """A production identity resolution (the attach/refresh path) on a fresh provider."""
    src = source or desk.source()
    frames = sc.fetch(src, list(symbols), "D", 1000, {})
    if not metadata:
        for frame in frames.values():
            frame.attrs.pop("security_metadata", None)
    return src, src.decision_volume.identities(frames, latest_closed_session(desk.clock.at))


def cached_gate(source, signal, desk):
    return sc.volume_status(source, signal, desk.clock.at, refresh=False)


def fence(desk, event_id, source=None):
    with EventRiskSource(source or desk.source(), desk.log, lambda s: (s, desk.price, desk.clock.at)) \
            .held_event(event_id) as status:
        return status(desk.clock.at)


# ------------------------------------------------------------------- F1 ----

@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_f1_identity_failure_suspends_a_saved_signal_before_trigger(tmp_path, kind):
    desk = Desk(tmp_path)
    _, vcp = desk.arm()
    desk.advance()
    breaks, code = FAILURES[kind]
    breaks(desk)
    src, seen = observe_identity(desk)
    assert isinstance(seen["LEAD"], str)
    sent = desk.sim.calls()
    # The same instance and a newly constructed provider both read the recorded failure.
    for source in (src, desk.source()):
        state, why = cached_gate(source, vcp[0], desk)
        assert state == "UNAVAILABLE" and why == code, why
    rec = desk.trigger(vcp)
    assert not rec.triggered and code in rec.skipped["LEAD"]
    assert desk.sim.calls() == sent                                   # cache-only: zero requests


@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_f1_identity_failure_closes_the_cached_final_fence_and_the_ticket(tmp_path, kind):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    pending, pv = desk.ticket(event["id"])
    desk.advance()
    breaks, code = FAILURES[kind]
    breaks(desk)
    observe_identity(desk)
    desk.restart()                                                    # reopened stores, fresh provider
    sent = desk.sim.calls()
    assert fence(desk, event["id"]).eligible is False
    assert desk.sim.calls() == sent
    desk.sim.status = [500] * 10 if kind == "asset_list_500" else []
    blocked, bv = desk.ticket(event["id"])                            # prepare grants nothing
    assert desk.tickets.get(blocked, bv, now=desk.clock.at)["state"] == "blocked"
    with pytest.raises(TicketError):
        approve(desk.tickets, blocked, bv, desk.adapters(), now=desk.clock.at)
    with pytest.raises(TicketError):
        approve(desk.tickets, pending, pv, desk.adapters(), now=desk.clock.at)
    with pytest.raises(TicketError):
        desk.tickets.consume(tid, v, request_id="f1", inputs=desk.adapters(), now=desk.clock.at)
    assert desk.tickets.get(tid, v, now=desk.clock.at)["state"] == "approved"      # never spent
    assert desk.tickets.get(pending, pv, now=desk.clock.at)["state"] != "approved"


def test_f1_missing_webull_metadata_and_store_failure_are_recorded(tmp_path, monkeypatch):
    desk = Desk(tmp_path)
    _, vcp = desk.arm()
    desk.advance()
    _, seen = observe_identity(desk, metadata=False)
    assert seen["LEAD"] == "WEBULL_IDENTITY_MISSING"
    assert cached_gate(desk.source(), vcp[0], desk) == ("UNAVAILABLE", "WEBULL_IDENTITY_MISSING")
    # Recovery, then an identity store that cannot resolve: the attempt is still recorded.
    desk.advance()
    observe_identity(desk)
    assert cached_gate(desk.source(), vcp[0], desk) == ("OK", None)
    desk.advance()

    def broken(self, *a, **k):
        raise sqlite3.OperationalError("fixture: identity store unavailable")
    monkeypatch.setattr(IdentityStore, "resolve", broken)
    _, seen = observe_identity(desk)
    assert seen["LEAD"] == "IDENTITY_STORE_UNAVAILABLE"
    monkeypatch.undo()
    assert cached_gate(desk.source(), vcp[0], desk) == ("UNAVAILABLE", "IDENTITY_STORE_UNAVAILABLE")


def test_f1_a_ticker_failure_leaves_a_healthy_ticker_usable(tmp_path):
    desk = Desk(tmp_path, symbols=("LEAD", "OTHER"))
    _, vcp = desk.arm(("LEAD", "OTHER"))
    assert sorted(s.symbol for s in vcp) == ["LEAD", "OTHER"]
    desk.advance()
    desk.sim.assets = [asset_row(s) for s in ("OTHER", "SPY", "QQQ", "IWM")]
    observe_identity(desk, ("LEAD", "OTHER"))
    by = {s.symbol: s for s in vcp}
    assert cached_gate(desk.source(), by["LEAD"], desk) == ("UNAVAILABLE", "ASSET_NOT_FOUND")
    assert cached_gate(desk.source(), by["OTHER"], desk) == ("OK", None)
    rec = desk.trigger(vcp)
    assert [t["symbol"] for t in rec.triggered] == ["OTHER"] and "ASSET_NOT_FOUND" in rec.skipped["LEAD"]


@pytest.mark.parametrize("status,code", [(401, "AUTH_OR_ENTITLEMENT_FAILURE"), (403, "AUTH_OR_ENTITLEMENT_FAILURE"),
                                         (429, "RATE_LIMITED")])
def test_f1_asset_list_stop_keeps_the_persisted_stop_and_records_source_health(tmp_path, status, code):
    desk = Desk(tmp_path)
    _, vcp = desk.arm()
    desk.advance()
    desk.sim.status = [status]
    src, seen = observe_identity(desk)
    assert seen["LEAD"] == "IDENTITY_UNAVAILABLE:" + code
    path = tmp_path / "volume.sqlite"
    with closing(sqlite3.connect(path)) as db:
        stops = db.execute("SELECT code FROM events WHERE state='STOP'").fetchall()
        health = db.execute("SELECT state, reason FROM identity_health WHERE scope=? ORDER BY sequence DESC "
                            "LIMIT 1", (SOURCE_SCOPE,)).fetchone()
    assert stops == [(code,)] and health == ("FAILED", code)
    sent = desk.sim.calls()
    assert cached_gate(src, vcp[0], desk) == ("UNAVAILABLE", "IDENTITY_SOURCE_FAILED:" + code)
    assert cached_gate(desk.source(), vcp[0], desk)[0] == "UNAVAILABLE" and desk.sim.calls() == sent
    # The run is stopped: no later request at all, and nothing new recorded for the source.
    assert src.decision_volume.client.stopped == code


def test_f1_recovery_confirms_the_unchanged_pin_without_new_terms_and_a_change_requalifies(tmp_path):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    terms = desk.log.signals.get(event["id"], desk.clock.at)["terms_digest"]
    desk.advance()
    FAILURES["not_found"][0](desk)
    observe_identity(desk)
    assert cached_gate(desk.source(), signal, desk)[0] == "UNAVAILABLE"
    desk.advance()
    desk.sim.assets = healthy_assets()
    observe_identity(desk)
    assert cached_gate(desk.source(), signal, desk) == ("OK", None)
    assert fence(desk, event["id"]).eligible is True
    after = desk.log.signals.get(event["id"], desk.clock.at)
    assert after["terms_digest"] == terms
    _, again = desk.arm()
    assert candidate_id(again[0], ENTRY) == candidate_id(signal, ENTRY)
    result = desk.tickets.consume(tid, v, request_id="f1-recovered", inputs=desk.adapters(), now=desk.clock.at)
    assert result["replay"] is False
    with pytest.raises(TicketError, match="already consumed"):
        desk.tickets.consume(tid, v, request_id="f1-again", inputs=desk.adapters(), now=desk.clock.at)
    # A recovery to a different asset ID is a new mapping version: requalify, never revive.
    desk.advance()
    FAILURES["not_found"][0](desk)
    observe_identity(desk)
    desk.advance()
    desk.sim.assets = [asset_row("LEAD", id=asset_id("LEAD", "relisted")), *healthy_assets()[1:]]
    observe_identity(desk)
    assert cached_gate(desk.source(), signal, desk) == ("CHANGED", "IDENTITY_CHANGED")


def test_f1_stale_in_memory_list_cannot_overwrite_a_newer_failure(tmp_path):
    desk = Desk(tmp_path)
    _, vcp = desk.arm()
    desk.advance()
    stale = desk.source()
    observe_identity(desk, source=stale)                     # list received at t1, held in memory
    desk.advance()
    desk.sim.status = [500]
    observe_identity(desk)                                   # t2: source FAILED recorded
    desk.advance()
    _, seen = observe_identity(desk, source=stale)           # t3, but the t1 list is reused
    assert seen["LEAD"] == "IDENTITY_STALE_ASSET_LIST"
    assert cached_gate(desk.source(), vcp[0], desk) == ("UNAVAILABLE", "IDENTITY_SOURCE_FAILED:HTTP_FAILURE")
    observe_identity(desk)                                   # a newly fetched list at t3
    assert cached_gate(desk.source(), vcp[0], desk) == ("OK", None)
    with closing(sqlite3.connect(tmp_path / "volume.sqlite")) as db:
        rows = db.execute("SELECT scope,state FROM identity_health ORDER BY sequence").fetchall()
    assert rows[-3:] == [(SOURCE_SCOPE, "FAILED"), (symbol_scope("LEAD"), "OK"), (SOURCE_SCOPE, "OK")]


def test_f1_legacy_pins_without_health_are_ineligible_until_refreshed(tmp_path):
    desk = Desk(tmp_path)
    _, vcp = desk.arm()
    path = tmp_path / "volume.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE identity_health")              # a b5dab2c cache: pins, no health
    assert IdentityStore(path).latest("LEAD").version == 1
    assert cached_gate(desk.source(), vcp[0], desk) == ("UNAVAILABLE", "IDENTITY_HEALTH_NOT_RECORDED")
    desk.advance()
    observe_identity(desk)
    assert cached_gate(desk.source(), vcp[0], desk) == ("OK", None)
    assert len(IdentityStore(path).history("LEAD")) == 1     # the pin was confirmed, not replaced


# ------------------------------------------------------------------- F2 ----

def evidence_request(signal):
    audit = signal.volume_evidence["audit"]["inputs"][0]
    ident = signal.volume_evidence["identity"]
    return BarRequest(symbols=(ident["alpaca_symbol"],), **audit["request"]), ident["alpaca_symbol"], ident["namespace"]


def disqualify(kind, desk, signal):
    """Commit one disqualifying change through the real production writers (own connection)."""
    path = desk.tmp / "volume.sqlite"
    at = desk.clock.at
    if kind == "stop":
        VolumeCache(path).record_stop("RATE_LIMITED", at)
    elif kind == "key_failed":
        req, symbol, space = evidence_request(signal)
        VolumeCache(path).record_failure(req, symbol, "HTTP_FAILURE", at, space)
    elif kind == "revised":
        req, symbol, space = evidence_request(signal)
        original = desk.sim.daily
        desk.sim.daily = lambda s, d: original(s, d) + (1 if (s, d) == ("LEAD", DAY0) else 0)
        client = desk.source().decision_volume.client
        client.fetch(req, identities={symbol: IdentityStore(path).latest("LEAD")})
    elif kind == "mapping":
        desk.sim.assets = [asset_row("LEAD", id=asset_id("LEAD", "relisted")), *healthy_assets()[1:]]
        observe_identity(desk)
    elif kind == "identity_health":
        IdentityStore(path).record_source_failure("HTTP_FAILURE", at)
    else:
        raise AssertionError(kind)


EXPECTED = {"stop": "PROVIDER_STOP_AFTER_LAST_SUCCESS:RATE_LIMITED", "key_failed": "HTTP_FAILURE",
            "revised": "VOLUME_EVIDENCE_REVISED", "mapping": "IDENTITY_CHANGED",
            "identity_health": "IDENTITY_SOURCE_FAILED:HTTP_FAILURE"}


def before_guard(monkeypatch, write):
    """Run ``write`` after the recheck (and its refresh) but before the final guard is taken."""
    real = EventRiskSource.held_event

    def wrapped(self, event_id):
        write()
        return real(self, event_id)
    monkeypatch.setattr(EventRiskSource, "held_event", wrapped)


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_f2_consume_refuses_a_disqualifier_committed_before_the_guard(tmp_path, monkeypatch, kind):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    desk.advance()
    adapters = desk.adapters()
    before_guard(monkeypatch, lambda: disqualify(kind, desk, signal))
    with pytest.raises(TicketError, match="nothing was consumed"):
        desk.tickets.consume(tid, v, request_id="f2-" + kind, inputs=adapters, now=desk.clock.at)
    monkeypatch.undo()
    assert desk.tickets.get(tid, v, now=desk.clock.at)["state"] == "approved"
    state, why = cached_gate(desk.source(), signal, desk)
    assert state != "OK" and why == EXPECTED[kind]


@pytest.mark.parametrize("kind", sorted(EXPECTED))
def test_f2_approve_refuses_a_disqualifier_committed_before_the_guard(tmp_path, monkeypatch, kind):
    desk = Desk(tmp_path)
    _, vcp = desk.arm()
    rec = desk.trigger(vcp)
    event = desk.log.signals.get(rec.triggered[0]["event_id"], desk.clock.at)
    tid, v = desk.ticket(event["id"])
    adapters = desk.adapters()
    before_guard(monkeypatch, lambda: disqualify(kind, desk, vcp[0]))
    with pytest.raises(TicketError, match="nothing was approved"):
        approve(desk.tickets, tid, v, adapters, now=desk.clock.at)
    monkeypatch.undo()
    assert desk.tickets.get(tid, v, now=desk.clock.at)["state"] == "pending"


@pytest.fixture(params=["delete", "wal"])
def journal(request):
    return request.param


def set_journal(path, mode):
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(f"PRAGMA journal_mode={mode}").fetchone()[0] == mode


def ticket_state(desk, tid, v):
    with closing(sqlite3.connect(desk.tmp / "tickets.sqlite")) as db:
        return db.execute("SELECT state FROM tickets WHERE ticket_id=? AND version=?", (tid, v)).fetchone()[0]


def in_gap(monkeypatch, action):
    """Run ``action`` inside the final transaction, after the final eligibility check
    (``final_problem`` has read the fence) and before the ticket COMMIT."""
    real = tk.final_evaluation

    def wrapped(*a, **k):
        out = real(*a, **k)
        action()
        return out
    monkeypatch.setattr(tk, "final_evaluation", wrapped)


def writer_in_thread(desk, tid, v, write):
    """A production writer on its own connection; records the ticket state it saw at its commit."""
    seen, started = {}, threading.Event()
    seen["committed"] = committed = threading.Event()

    def run():
        started.set()
        try:
            write()
            committed.set()
            seen["ticket_state_after_writer_commit"] = ticket_state(desk, tid, v)
        except Exception as exc:  # noqa: BLE001 - reported to the test
            seen["error"] = repr(exc)
    thread = threading.Thread(target=run, daemon=True)
    return thread, started, seen


@pytest.mark.parametrize("kind", ["stop", "mapping", "identity_health", "key_failed"])
def test_f2_a_writer_racing_the_gap_serializes_after_the_ticket_commit(tmp_path, monkeypatch, journal, kind):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    set_journal(tmp_path / "volume.sqlite", journal)
    desk.advance()
    path = tmp_path / "volume.sqlite"
    if kind == "mapping":                                      # the provider reply is prepared outside the guard
        kept = desk.sim.assets
        desk.sim.assets = [asset_row("LEAD", id=asset_id("LEAD", "relisted")), *healthy_assets()[1:]]
        late = desk.source()
        frames = sc.fetch(late, ["LEAD"], "D", 1000, {})
        assets = late.decision_volume.client.fetch_assets()
        desk.sim.assets = kept                                  # the ticket's own recheck sees the old reply
        meta = {"LEAD": SecurityMetadata.model_validate(frames["LEAD"].attrs["security_metadata"])}
        write = lambda: IdentityStore(path).resolve(meta, assets, latest_closed_session(desk.clock.at))
    else:
        write = lambda: disqualify(kind, desk, signal)
    adapters = desk.adapters()
    thread, started, seen = writer_in_thread(desk, tid, v, write)
    during = {}

    def gap():
        thread.start()
        assert started.wait(5)
        thread.join(0.5)                                         # bounded: the writer must still be waiting
        during["writer_waiting"] = thread.is_alive()
        state, why = cached_gate(desk.source(), signal, desk)    # a fresh reader sees no change yet
        during["gate"] = state
        # Another process cannot take the writer slot either (no wait: fails at once).
        probe = subprocess.run([sys.executable, "-c", (
            "import sqlite3,sys; db=sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)\n"
            "try:\n db.execute('BEGIN IMMEDIATE'); print('acquired')\n"
            "except sqlite3.OperationalError as e: print('locked' if 'locked' in str(e) else e)"),
            str(path)], capture_output=True, text=True, timeout=30)
        during["other_process"] = probe.stdout.strip()
    in_gap(monkeypatch, gap)
    result = desk.tickets.consume(tid, v, request_id="f2-race-" + kind, inputs=adapters, now=desk.clock.at)
    thread.join(10)
    assert during == {"writer_waiting": True, "gate": "OK", "other_process": "locked"}
    assert result["replay"] is False and not thread.is_alive() and "error" not in seen, seen
    assert seen["committed"].is_set()
    assert seen["ticket_state_after_writer_commit"] == "consumed"   # writer committed after the ticket
    assert cached_gate(desk.source(), signal, desk)[0] != "OK"       # and its change is now visible


def test_f2_control_a_wal_read_snapshot_guard_lets_the_writer_in(tmp_path, monkeypatch):
    """Discrimination check: replacing the reservation with a WAL read snapshot (the
    insufficient guard) lets the STOP commit before the ticket. The race test above
    would fail with it; this proves that test can tell the two apart."""
    from contextlib import contextmanager
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    set_journal(tmp_path / "volume.sqlite", "wal")
    desk.advance()

    @contextmanager
    def read_only(self):
        db = sqlite3.connect(tmp_path / "volume.sqlite", isolation_level=None)
        db.execute("BEGIN")
        db.execute("SELECT count(*) FROM events").fetchone()
        try:
            yield
        finally:
            db.execute("ROLLBACK")
            db.close()
    monkeypatch.setattr(AlpacaVolumeProvider, "held", read_only)
    adapters = desk.adapters()
    thread, started, seen = writer_in_thread(desk, tid, v, lambda: disqualify("stop", desk, signal))

    during = {}

    def gap():
        thread.start()
        # The STOP commits while the ticket transaction is still open: the race the real guard closes.
        during["writer_committed_before_ticket_commit"] = seen["committed"].wait(5)
    in_gap(monkeypatch, gap)
    desk.tickets.consume(tid, v, request_id="f2-control", inputs=adapters, now=desk.clock.at)
    thread.join(10)
    assert during == {"writer_committed_before_ticket_commit": True} and "error" not in seen, seen
    assert ticket_state(desk, tid, v) == "consumed"


def test_f2_no_request_inside_the_guard_and_refresh_is_refused_there(tmp_path, monkeypatch):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    desk.advance()
    calls = []
    real = AlpacaVolumeProvider.held

    from contextlib import contextmanager

    @contextmanager
    def counted(self):
        with real(self):
            calls.append(desk.sim.calls())
            assert self.gate(signal.volume_evidence, desk.clock.at, refresh=True,
                             metadata=desk.metadata) == ("UNAVAILABLE", NETWORK_FORBIDDEN)
            with pytest.raises(Exception, match=NETWORK_FORBIDDEN):
                self.client.fetch_assets()
            yield
            calls.append(desk.sim.calls())
    monkeypatch.setattr(AlpacaVolumeProvider, "held", counted)
    desk.tickets.consume(tid, v, request_id="f2-net", inputs=desk.adapters(), now=desk.clock.at)
    assert len(calls) == 2 and calls[0] == calls[1]


def test_f2_unrelated_revision_rollback_lock_timeout_concurrency_and_manual_stop(tmp_path, monkeypatch):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    desk.advance()
    path = tmp_path / "volume.sqlite"
    # An exception inside the final transaction refuses, spends nothing and releases every guard.
    adapters = desk.adapters()

    def boom():
        raise ValueError("fixture failure inside the final transaction")
    in_gap(monkeypatch, boom)
    with pytest.raises(TicketError, match="Final check unavailable"):
        desk.tickets.consume(tid, v, request_id="f2-boom", inputs=adapters, now=desk.clock.at)
    monkeypatch.undo()
    with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as db:
        db.execute("BEGIN IMMEDIATE")                         # nothing is still held
        db.execute("ROLLBACK")
    assert ticket_state(desk, tid, v) == "approved"
    # Another holder of the writer slot: the guard times out, the ticket refuses unspent.
    monkeypatch.setattr("desk.alpaca_source.GUARD_TIMEOUT_SECONDS", 0.05)
    blocker = sqlite3.connect(path, isolation_level=None)
    before_guard(monkeypatch, lambda: blocker.execute("BEGIN IMMEDIATE"))   # after the recheck
    try:
        with pytest.raises(TicketError, match="Final check unavailable"):
            desk.tickets.consume(tid, v, request_id="f2-busy", inputs=desk.adapters(), now=desk.clock.at)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    monkeypatch.undo()
    assert ticket_state(desk, tid, v) == "approved"
    # A manual stop committed before the guard still refuses.
    revision = desk.state.set_manual_halt("fixture-account", True, actor="fixture:automated-test",
                                          reason="fixture", now=desk.clock.at)
    with pytest.raises(TicketError):
        desk.tickets.consume(tid, v, request_id="f2-halt", inputs=desk.adapters(), now=desk.clock.at)
    desk.state.set_manual_halt("fixture-account", False, expected_revision=revision,
                               actor="fixture:automated-test", reason="fixture", now=desk.clock.at)
    assert ticket_state(desk, tid, v) == "approved"
    # An unrelated ticker's revision before the guard is not this ticket's dependency.
    adapters = desk.adapters()
    spy = BarRequest(symbols=("SPY",), **signal.volume_evidence["audit"]["inputs"][0]["request"])
    before_guard(monkeypatch, lambda: desk.source().decision_volume.client.fetch(spy))
    # A second consumer racing in the gap waits for the ticket lock and then finds it used.
    second = {}
    racer = threading.Thread(target=lambda: second.setdefault("result", _consume_or_error(
        desk, tid, v, "f2-second")), daemon=True)
    in_gap(monkeypatch, lambda: (racer.start(), racer.join(0.2)))
    first = desk.tickets.consume(tid, v, request_id="f2-first", inputs=adapters, now=desk.clock.at)
    racer.join(30)
    assert first["replay"] is False and "already consumed" in second["result"]
    with pytest.raises(TicketError, match="already consumed"):
        desk.tickets.consume(tid, v, request_id="f2-third", inputs=desk.adapters(), now=desk.clock.at)


def _consume_or_error(desk, tid, v, request_id):
    try:
        desk.tickets.consume(tid, v, request_id=request_id, inputs=desk.adapters(), now=desk.clock.at)
        return "consumed"
    except TicketError as exc:
        return str(exc)
