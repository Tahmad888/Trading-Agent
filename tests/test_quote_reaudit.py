"""Child 3 re-audit repairs R1–R4 (Astra, 2026-10-05). Synthetic only: no provider call.

Real SignalStore, TicketStore, MappingStore, QuoteService and DXLink Session code;
provider rows, mappings and approvals are labelled fixtures.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time

import pytest

from desk import scanner as sc
from desk.quote_check import diagnostic
from desk.quote_mapping import MappingStore, QuoteMapping, build
from desk.quote_risk import webull_identity
from desk.security import SecurityMetadata
from desk.tastytrade_quotes import CORE, FIELDS, QuoteUnavailable, instrument
from desk.tastytrade_transport import PROFILE_CHANNEL, Session, StreamToken, capture
from desk.tickets import TicketError, TicketStore
from desk.vendor_basis import VendorHistoryStore
from tests.quote_support import QuoteDesk, record
from tests.test_quote_repairs import PROFILE_MAP, Server, compact, profile_row
from tests.test_tastytrade_quotes import AT, client_fixture, quote_row, service, stock, trade_row
from tests.ticket_support import approve

REPO = Path(__file__).resolve().parents[1]


def prepared(tmp_path, *, approved=False):
    q = QuoteDesk(tmp_path)
    tickets = TicketStore(tmp_path / "tickets.sqlite")
    tid, version = tickets.prepare(q.request(), q.inputs(), now=q.at)
    assert tickets.get(tid, version, now=q.at)["state"] == "pending"
    if approved:
        approve(tickets, tid, version, q.inputs(), now=q.at)
    return q, tickets, tid, version


def act(q, tickets, tid, version, operation, inputs=None):
    inputs = inputs or q.inputs()
    if operation == "approve":
        approve(tickets, tid, version, inputs, now=q.at)
    else:
        tickets.consume(tid, version, request_id="fixture:r1", inputs=inputs, now=q.at)


def at_commit_hook(tickets, operation, start_writer):
    """Run start_writer() at the final audit write: final checks done, COMMIT pending."""
    original, seen = tickets._audit, {}

    def hook(*args, **kwargs):
        if args[4] == {"approve": "approved", "consume": "consumed"}[operation]:
            seen["writer"] = start_writer()
        return original(*args, **kwargs)
    tickets._audit = hook
    return seen


# ---- R1: the reviewed mapping stays fixed from the final check to COMMIT -----------------------------
@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_thread_writer_with_its_own_store_waits_for_the_ticket_commit(tmp_path, operation):
    q, tickets, tid, version = prepared(tmp_path, approved=operation == "consume")
    other = MappingStore(q.store.path)  # a separate store object, as the review command uses
    done, errors = Event(), []

    def start():
        def write():
            try:
                other.add(record(webull_id="id:OTHER"))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))
            finally:
                done.set()
        Thread(target=write).start()
        return not done.wait(0.5)  # True: the writer is still waiting at COMMIT time
    seen = at_commit_hook(tickets, operation, start)
    act(q, tickets, tid, version, operation)
    assert seen["writer"] is True  # it could not complete before the ticket committed
    assert done.wait(5) and errors == []
    assert tickets.get(tid, version, now=q.at)["state"] == {"approve": "approved", "consume": "consumed"}[operation]
    with pytest.raises(QuoteUnavailable, match="QUOTE_MAPPING_WEBULL_MISMATCH"):
        q.source().resolve(q.event_id, q.at)  # the later change is seen by every later check


WRITER = """
import sys
from desk.quote_mapping import MappingStore, QuoteMapping
MappingStore(sys.argv[1]).add(QuoteMapping.model_validate_json(sys.argv[2]))
print("WROTE")
"""


@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_separate_process_writer_waits_for_the_ticket_commit(tmp_path, operation):
    q, tickets, tid, version = prepared(tmp_path, approved=operation == "consume")
    changed = record(webull_id="id:OTHER").model_dump_json()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(REPO / "src"), str(REPO)]))
    procs = []

    def start():
        proc = subprocess.Popen([sys.executable, "-c", WRITER, str(q.store.path), changed], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(proc)
        time.sleep(0.5)
        return proc.poll() is None  # still blocked on the shared fence
    seen = at_commit_hook(tickets, operation, start)
    act(q, tickets, tid, version, operation)
    out, err = procs[0].communicate(timeout=20)
    assert seen["writer"] is True and procs[0].returncode == 0 and "WROTE" in out, err
    assert q.store.records()[0].webull.instrument_id == "id:OTHER"


def test_writer_that_finishes_before_the_fence_makes_the_action_refuse(tmp_path):
    q, tickets, tid, version = prepared(tmp_path)
    adapters = q.inputs()
    observe = adapters.observe

    def change_then_observe(*args):
        MappingStore(q.store.path).add(record(webull_id="id:OTHER"))
        return observe(*args)
    with pytest.raises(TicketError):
        approve(tickets, tid, version, replace(adapters, observe=change_then_observe), now=q.at)
    assert tickets.get(tid, version, now=q.at)["state"] == "pending"


def hold_exclusive(path):
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_busy_fence_refuses_then_releases_cleanly(tmp_path):
    q, tickets, tid, version = prepared(tmp_path)
    fd = hold_exclusive(q.store.path)  # a writer stuck mid-update elsewhere
    impatient = MappingStore(q.store.path, lock_timeout=0.2)
    try:
        with pytest.raises(TicketError, match="QUOTE_MAPPING_STORE_BUSY"):
            approve(tickets, tid, version, q.inputs(terms=q.source(store=impatient)), now=q.at)
    finally:
        os.close(fd)
    assert tickets.get(tid, version, now=q.at)["state"] == "pending"
    approve(tickets, tid, version, q.inputs(terms=q.source(store=impatient)), now=q.at)  # no leaked lock
    assert tickets.get(tid, version, now=q.at)["state"] == "approved"


def test_unreadable_store_refuses_and_leaves_the_lock_free(tmp_path):
    q, tickets, tid, version = prepared(tmp_path)
    adapters = q.inputs()
    observe = adapters.observe

    def corrupt(*args):
        result = observe(*args)
        q.store.path.write_text("{not json")
        return result
    with pytest.raises(TicketError, match="QUOTE_MAPPING_STORE_UNREADABLE"):
        approve(tickets, tid, version, replace(adapters, observe=corrupt), now=q.at)
    fd = os.open(str(q.store.path) + ".lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # would raise if a lock had leaked
    os.close(fd)


def test_failed_write_leaves_no_partial_file_temp_file_or_lock(tmp_path, monkeypatch):
    store = MappingStore(tmp_path / "m.json")
    store.add(record())
    before = store.path.read_text()

    def broken(*args):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        store.add(record(webull_id="id:OTHER"))
    monkeypatch.undo()
    assert store.path.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.json", "m.json.lock"]
    store.add(record(webull_id="id:OTHER"))  # lock free
    assert store.records()[0].webull.instrument_id == "id:OTHER"


def test_no_mapping_file_stays_read_only(tmp_path):
    store = MappingStore(tmp_path / "absent" / "m.json")
    with store.held() as view:
        with pytest.raises(QuoteUnavailable, match="QUOTE_MAPPING_MISSING"):
            view.verify("LEAD", price_basis={}, identity=None, environment="production", webull_identity=None)
    assert not (tmp_path / "absent").exists()


def test_symlinked_lock_is_refused(tmp_path):
    store = MappingStore(tmp_path / "m.json")
    store.add(record())
    store.lock_path.unlink()
    store.lock_path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(QuoteUnavailable, match="QUOTE_MAPPING_LOCK_UNAVAILABLE"):
        store.records()


def test_v1_records_are_preserved_refused_and_migrated_without_erasing_peers(tmp_path):
    store = MappingStore(tmp_path / "m.json")
    legacy = {"desk_symbol": "OLD", "method": "reviewed-crosswalk-v1", "note": "pre-classification record"}
    store.path.write_text(json.dumps({"schema": "desk-quote-mappings-v1", "mappings": [legacy]}))
    with store.held() as view:
        assert view.problems == {"OLD": "QUOTE_MAPPING_REVIEW_REQUIRED"}
    bad_peer = {**record("PEER").model_dump(mode="json"), "mapping_digest": "tampered"}
    store.add(record())
    data = json.loads(store.path.read_text())
    assert data["schema"] == "desk-quote-mappings-v2" and data["legacy_unverified"] == [legacy]
    raw = json.loads(store.path.read_text())
    raw["mappings"].append(bad_peer)
    store.path.write_text(json.dumps(raw))
    store.add(record())  # re-adding LEAD keeps the invalid peer and the legacy record verbatim
    data = json.loads(store.path.read_text())
    assert bad_peer in data["mappings"] and data["legacy_unverified"] == [legacy]
    with store.held() as view:
        assert set(view.records) == {"LEAD"} and view.problems == {"PEER": "QUOTE_MAPPING_INVALID",
                                                                    "OLD": "QUOTE_MAPPING_REVIEW_REQUIRED"}


# ---- R2: current classification is bound and checked --------------------------------------------------
def test_common_stock_becoming_an_etf_refuses_before_any_signal_change(tmp_path, monkeypatch):
    q = QuoteDesk(tmp_path)
    q.quotes.register(q.changed_identity(**{"is-etf": True}))  # same symbol, CUSIP and streamer
    q.feed(price="3.10")
    called = []
    monkeypatch.setattr(sc, "revalidate_signal", lambda *a, **k: called.append(1))
    before = q.event()
    with pytest.raises(QuoteUnavailable, match="QUOTE_MAPPING_CLASSIFICATION_MISMATCH"):
        q.source().resolve(q.event_id, q.at)
    assert called == [] and q.event()["state"] == before["state"] == "triggered"


@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_reclassification_between_recheck_and_commit_refuses(tmp_path, operation):
    q, tickets, tid, version = prepared(tmp_path, approved=operation == "consume")
    adapters = q.inputs()
    observe = adapters.observe

    def flip(*args):
        result = observe(*args)
        q.quotes.register(q.changed_identity(**{"is-etf": True}))
        q.feed()
        return result
    with pytest.raises(TicketError):
        act(q, tickets, tid, version, operation, replace(adapters, observe=flip))
    assert tickets.get(tid, version, now=q.at)["state"] == {"approve": "pending", "consume": "approved"}[operation]


def etf_pin(tmp_path, sub_category="ETF"):
    vendor = VendorHistoryStore(tmp_path / "vendor.sqlite")
    vendor.pin("api.sandbox.webull.com", SecurityMetadata(
        symbol="SPY", instrument_id="id:SPY", name="Synthetic SPY ETF", category="US_STOCK",
        sub_category=sub_category, exchange_code="PSE", currency="USD", observed_at=AT.isoformat()))
    return vendor.pinned("api.sandbox.webull.com", "SPY")


SPY_BASIS = {"host": "api.sandbox.webull.com", "security_id": "id:SPY", "currency": "USD", "symbol": "SPY"}


def test_correctly_mapped_etf_verifies_and_an_etf_turning_common_refuses(tmp_path):
    store = MappingStore(tmp_path / "m.json")
    store.add(record("SPY", etf=True))
    pinned = etf_pin(tmp_path)
    etf = instrument("SPY", stock("SPY", **{"is-etf": True}), AT)
    assert store.verify("SPY", price_basis=SPY_BASIS, identity=etf, environment="production",
                        webull_identity=pinned).desk_symbol == "SPY"
    common = instrument("SPY", stock("SPY", **{"is-etf": False}), AT)
    with pytest.raises(QuoteUnavailable, match="QUOTE_MAPPING_CLASSIFICATION_MISMATCH"):
        store.verify("SPY", price_basis=SPY_BASIS, identity=common, environment="production",
                     webull_identity=pinned)


def test_review_against_a_differently_classified_webull_pin_refuses(tmp_path):
    store = MappingStore(tmp_path / "m.json")
    store.add(record("SPY", etf=True))
    etf = instrument("SPY", stock("SPY", **{"is-etf": True}), AT)
    with pytest.raises(QuoteUnavailable, match="QUOTE_MAPPING_CLASSIFICATION_MISMATCH"):
        store.verify("SPY", price_basis=SPY_BASIS, identity=etf, environment="production",
                     webull_identity=etf_pin(tmp_path, "COMMON_STOCK"))
    with pytest.raises(QuoteUnavailable, match="QUOTE_MAPPING_WEBULL_IDENTITY_UNAVAILABLE"):
        store.verify("SPY", price_basis=SPY_BASIS, identity=etf, environment="production", webull_identity=None)


@pytest.mark.parametrize("value", ["missing", None, "false", 0, "True"])
def test_missing_or_corrupt_classification_is_unavailable_never_common_stock(tmp_path, value):
    row = stock("LEAD")
    if value == "missing":
        row.pop("is-etf")
    else:
        row["is-etf"] = value
    identity = instrument("LEAD", row, AT)
    assert identity.is_etf is None
    store = MappingStore(tmp_path / "m.json")
    store.add(record())
    basis = {"host": "api.sandbox.webull.com", "security_id": "id:LEAD", "currency": "USD", "symbol": "LEAD"}
    with pytest.raises(QuoteUnavailable, match="QUOTE_MAPPING_CLASSIFICATION_UNAVAILABLE"):
        store.verify("LEAD", price_basis=basis, identity=identity, environment="production",
                     webull_identity={"instrument_id": "id:LEAD", "currency": "USD", "sub_category": "COMMON_STOCK"})


def test_review_refuses_a_string_classification():
    webull = {"host": "api.sandbox.webull.com", "checks": [{"symbol": "LEAD", "instrument_id": "id:LEAD",
              "name": "Synthetic LEAD Inc", "security_type": "COMMON_STOCK", "exchange_code": "NSQ",
              "currency": "USD"}]}
    capture = instrument("LEAD", stock("LEAD", description="Synthetic LEAD Inc"), AT).capture()
    capture["provider_fields"]["is-etf"] = "false"
    with pytest.raises(ValueError):
        build("LEAD", webull, {"environment": "production", "identity_capture": [capture]}, "x", AT)


def test_harmless_refresh_needs_no_new_review(tmp_path):
    """Newer receipt, new description and new capture hashes keep the same identity."""
    q, tickets, tid, version = prepared(tmp_path)
    later = q.at + timedelta(seconds=2)
    q.quotes.register(instrument("LEAD", stock("LEAD", description="LEAD Holdings (renamed)"), later))
    q.feed(at=later)
    approve(tickets, tid, version, q.inputs(), now=later)
    assert tickets.get(tid, version, now=later)["state"] == "approved"
    renamed = record().model_dump(mode="json")
    renamed["webull"]["name"] = "LEAD Holdings"
    renamed["tastytrade"]["description"] = "LEAD Holdings common"
    renamed["webull_capture_sha256"] = renamed["tastytrade_capture_sha256"] = "newer-capture"
    assert QuoteMapping.model_validate(renamed).mapping_digest == record().mapping_digest
    assert record(etf=True).mapping_digest != record().mapping_digest


def test_the_signal_path_pins_the_webull_classification(tmp_path):
    q = QuoteDesk(tmp_path)
    pinned = webull_identity(q.desk.src, "api.sandbox.webull.com", "LEAD")
    assert pinned == {"instrument_id": "id:LEAD", "currency": "USD", "exchange_code": "TEST",
                      "sub_category": "COMMON_STOCK"}


# ---- R3: a known halt survives Profile failures until resumption evidence ----------------------------
def live_session(q, *, halted=True):
    token = StreamToken("wss://fixture.dxfeed.com", "synthetic-token", q.at + timedelta(hours=1), "api")
    session = Session(q.quotes, token, q.at, profile=True)
    for message in (dict(type="SETUP", channel=0, keepaliveTimeout=60),
                    dict(type="AUTH_STATE", channel=0, state="UNAUTHORIZED"),
                    dict(type="AUTH_STATE", channel=0, state="AUTHORIZED"),
                    dict(type="CHANNEL_OPENED", channel=3),
                    dict(type="FEED_CONFIG", channel=3, dataFormat="COMPACT",
                         eventFields={k: list(FIELDS[k]) for k in CORE}),
                    dict(type="CHANNEL_OPENED", channel=PROFILE_CHANNEL), PROFILE_MAP):
        session.receive(message, q.at)
    q.feed()
    if halted:
        session.receive(profile_row("HALTED", "LEAD"), q.at)
    return session


FAULTS = {
    "error": dict(type="ERROR", channel=PROFILE_CHANNEL, error="UNKNOWN"),
    "closed": dict(type="CHANNEL_CLOSED", channel=PROFILE_CHANNEL),
    "invalid_map": dict(PROFILE_MAP, eventFields={"Profile": ["eventType", "eventSymbol"]}),
    "invalid_row": dict(type="FEED_DATA", channel=PROFILE_CHANNEL, data=["Profile", ["Profile", "LEAD", "OPEN", 0, 0]]),
    "broken_frame": dict(type="FEED_DATA", channel=PROFILE_CHANNEL, data=["Profile", ["Profile", "LEAD"]]),
    "undefined": profile_row("UNDEFINED", "LEAD"),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_halt_is_retained_through_profile_faults(tmp_path, fault):
    q = QuoteDesk(tmp_path)
    session = live_session(q)
    session.receive(FAULTS[fault], q.at)
    q.feed()  # a fresh trade is not resumption evidence
    assert q.quotes.status("LEAD", q.at)[0] == "HALTED" and q.quotes.connected
    with pytest.raises(QuoteUnavailable, match="SECURITY_HALTED"):
        q.source().resolve(q.event_id, q.at)
    tickets = TicketStore(tmp_path / "tickets.sqlite")
    tid, version = tickets.prepare(q.request(), q.inputs(), now=q.at)
    assert tickets.get(tid, version, now=q.at)["state"] == "blocked"


@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_halt_then_profile_fault_between_recheck_and_commit_refuses(tmp_path, operation):
    q = QuoteDesk(tmp_path)
    session = live_session(q, halted=False)
    tickets = TicketStore(tmp_path / "tickets.sqlite")
    tid, version = tickets.prepare(q.request(), q.inputs(), now=q.at)
    if operation == "consume":
        approve(tickets, tid, version, q.inputs(), now=q.at)
    adapters = q.inputs()
    observe = adapters.observe

    def halt_then_lose_profile(*args):
        result = observe(*args)
        session.receive(profile_row("HALTED", "LEAD"), q.at)
        session.receive(FAULTS["error"], q.at)
        return result
    with pytest.raises(TicketError, match="SECURITY_HALTED"):
        act(q, tickets, tid, version, operation, replace(adapters, observe=halt_then_lose_profile))


def test_bridge_refuses_a_reported_halt_without_relying_on_the_trade_latch(tmp_path, monkeypatch):
    """Defence in depth: the bridge's own status check refuses even if trade() would serve."""
    q = QuoteDesk(tmp_path)
    live_session(q, halted=False)
    q.source().resolve(q.event_id, q.at)  # control: ACTIVE, fresh trade
    monkeypatch.setattr(q.quotes, "status", lambda symbol, at: ("HALTED", "PROFILE_HALTED"))
    with pytest.raises(QuoteUnavailable, match="SECURITY_HALTED"):
        q.source().resolve(q.event_id, q.at)


def test_halt_survives_reconnect_and_fresh_trades_until_active(tmp_path):
    q = QuoteDesk(tmp_path)
    live_session(q)
    q.quotes.disconnect("DXLINK_HEARTBEAT_TIMEOUT")
    session = live_session(q, halted=False)  # a new generation, new map, fresh trade
    with pytest.raises(QuoteUnavailable, match="SECURITY_HALTED"):
        q.source().resolve(q.event_id, q.at)
    assert q.quotes.status("LEAD", q.at) == ("HALTED", "HALT_RETAINED_NO_RESUMPTION_EVIDENCE")
    session.receive(profile_row("ACTIVE", "LEAD"), q.at)  # positive resumption evidence
    q.feed()
    terms = q.source().resolve(q.event_id, q.at)
    assert terms.quote_provenance.mapping_digest == record().mapping_digest


def test_superseded_session_cannot_clear_a_newer_halt(tmp_path):
    q = QuoteDesk(tmp_path)
    old = live_session(q, halted=False)
    new = live_session(q)  # newer generation observes the halt
    with pytest.raises(QuoteUnavailable, match="SESSION_SUPERSEDED"):
        old.receive(profile_row("ACTIVE", "LEAD"), q.at)
    assert q.quotes.status("LEAD", q.at)[0] == "HALTED" and new.generation == q.quotes.generation


def test_halt_is_per_symbol_and_no_profile_is_unknown():
    source = service(("SPY", "QQQ"))
    for symbol in ("SPY", "QQQ"):
        source.feed("Trade", trade_row(symbol), AT)
    assert source.status("SPY", AT) == ("UNKNOWN", "PROFILE_NO_ACCEPTED_MAP")
    source.feed("Profile", dict(eventType="Profile", eventSymbol="SPY", tradingStatus="HALTED"), AT)
    with pytest.raises(QuoteUnavailable, match="SECURITY_HALTED"):
        source.trade("SPY", AT, timedelta(seconds=60))
    assert source.trade("QQQ", AT, timedelta(seconds=60))[0].price > 0
    assert source.status("QQQ", AT)[0] == "UNKNOWN"


# ---- R4: final-attempt coverage, never earlier connections' prices ------------------------------------
class Attempts(Server):
    """Per-connection data scripts; listed connections fail once their queue empties."""

    def __init__(self, scripts, fail=(), **kwargs):
        super().__init__(**kwargs)
        self.scripts, self.fail = scripts, set(fail)

    def reply(self, msg):
        if msg["type"] == "FEED_SUBSCRIPTION" and msg.get("channel") == 3 and msg.get("reset"):
            return list(self.scripts[min(self.connections, len(self.scripts)) - 1])
        return super().reply(msg)

    def factory(self, ticks):
        make = super().factory(ticks)
        server = self

        def connect(url):
            socket = make(url)
            original = socket.recv

            def recv(timeout):
                try:
                    return original(timeout)
                except TimeoutError:
                    if server.connections in server.fail:
                        raise OSError("synthetic lost connection")
                    raise
            socket.recv = recv
            return socket
        return connect


def report(server, symbols=("SPY",), seconds=6):
    client, _ = client_fixture()
    client.resolve = lambda symbol, kind, svc: (svc.register(instrument(symbol, stock(symbol), AT)) or
                                                svc.identities[symbol])
    ticks = [0.0]

    def capture_fn(cl, svc, **kwargs):
        return capture(cl, svc, connect=server.factory(ticks), clock=lambda: AT, monotonic=lambda: ticks[0],
                       sleep=lambda s: ticks.__setitem__(0, ticks[0] + s), **kwargs)
    from desk.tastytrade_quotes import QuoteService
    return diagnostic(client, QuoteService(), list(symbols), seconds=seconds, reconnects=1,
                      capture_fn=capture_fn, clock=lambda: AT)


def test_empty_final_attempt_keeps_earlier_trade_as_history_only():
    out = report(Attempts([[compact("Trade", trade_row())], []], fail={1}))
    assert out["capture"]["stop_reason"] == "CAPTURE_COMPLETE"
    assert out["status"] == "NO_FINAL_ATTEMPT_OBSERVATIONS" and out["lag_evidence"] == "UNAVAILABLE"
    assert out["final_attempt"]["components"]["SPY"]["Trade"] is False
    # D1: rows are the final attempt's terminal view, which holds nothing earlier.
    (row,) = out["stocks"]["checks"]
    assert row["trade"]["status"] == row["quote"]["status"] == "UNAVAILABLE"
    assert "age_seconds" not in row["trade"] and "bid" not in row["quote"]
    assert out["stocks"]["timing_status"] in {"NOT_TESTED_NO_OBSERVATIONS", "NOT_TESTED_MARKET_CLOSED"}
    history = out["historical_observations"]
    assert history["eligible"] is False and history["latest"]["checks"][0]["trade"]["status"] == "AVAILABLE"
    assert out["capture"]["recoveries"][0]["recovered"] is False


def test_one_ticker_recovers_and_another_does_not():
    second = [compact("Trade", trade_row()), compact("Quote", quote_row())]
    out = report(Attempts([[compact("Trade", trade_row()), compact("Trade", trade_row("QQQ"))], second],
                          fail={1}), symbols=("SPY", "QQQ"))
    recovery = out["capture"]["recoveries"][0]
    assert recovery["by_symbol"]["SPY"] == {"Quote": True, "Trade": True, "Profile": False}
    assert recovery["by_symbol"]["QQQ"] == {"Quote": False, "Trade": False, "Profile": False}
    assert recovery["recovered"] is False
    rows = {row["symbol"]: row for row in out["stocks"]["checks"]}
    assert rows["SPY"]["trade"]["status"] == "AVAILABLE" and rows["QQQ"]["trade"]["status"] == "UNAVAILABLE"
    assert "age_seconds" not in rows["QQQ"]["trade"]  # nothing from the earlier generation


@pytest.mark.parametrize("component", ["Quote", "Trade"])
def test_single_component_recovery_is_reported_as_partial(component):
    row = compact("Quote", quote_row()) if component == "Quote" else compact("Trade", trade_row())
    out = report(Attempts([[compact("Trade", trade_row())], [row]], fail={1}))
    recovery = out["capture"]["recoveries"][0]
    other = "Trade" if component == "Quote" else "Quote"
    assert recovery["by_symbol"]["SPY"][component] and not recovery["by_symbol"]["SPY"][other]
    assert recovery["recovered"] is False
    check = out["stocks"]["checks"][0]
    assert check[component.lower()]["status"] == "AVAILABLE" and check[other.lower()]["status"] == "UNAVAILABLE"
    assert check[component.lower()].get("received_at") == AT.isoformat()


def test_profile_only_recovery_is_not_quote_or_trade_recovery():
    source = service(("SPY",))
    source.feed("Profile", dict(eventType="Profile", eventSymbol="SPY", tradingStatus="ACTIVE"), AT)
    assert source.generation_events() == {"SPY": {"Profile": 1}}
    server = Attempts([[compact("Trade", trade_row())], []], fail={1},
                      profile=[PROFILE_MAP, profile_row("ACTIVE")])
    client, _ = client_fixture()
    ticks = [0.0]
    result = capture(client, service(("SPY",)), seconds=6, reconnects=1, connect=server.factory(ticks),
                     clock=lambda: AT, monotonic=lambda: ticks[0], sleep=lambda s: ticks.__setitem__(0, ticks[0] + s),
                     profile=True)
    recovery = result["recoveries"][0]
    assert recovery["by_symbol"]["SPY"]["Profile"] and recovery["fresh_events"] > 0
    assert recovery["recovered"] is False


def test_genuine_recovery_is_reported_with_new_observations():
    both = [compact("Trade", trade_row()), compact("Quote", quote_row())]
    out = report(Attempts([[compact("Trade", trade_row())], both], fail={1}))
    assert out["capture"]["recoveries"][0]["recovered"] is True
    assert out["status"] == "OBSERVATIONS_ONLY" and out["lag_evidence"] == "AVAILABLE_WITHIN_QUOTE_POLICY"
    assert out["stocks"]["checks"][0]["generation"] == out["final_attempt"]["generation"]


def test_classification_is_part_of_the_bound_instrument_identity():
    """The provenance digest bound into a ticket changes with classification, and a
    reclassified registration withdraws the earlier evidence (IDENTITY_CHANGED)."""
    common = instrument("LEAD", stock("LEAD", **{"is-etf": False}), AT)
    etf = instrument("LEAD", stock("LEAD", **{"is-etf": True}), AT)
    renamed = instrument("LEAD", stock("LEAD", **{"is-etf": False}, description="LEAD renamed"), AT)
    assert common.identity_digest != etf.identity_digest
    assert common.identity_digest == renamed.identity_digest
    source = service(("LEAD",))
    source.register(common)
    source.feed("Trade", trade_row("LEAD"), AT)
    source.register(etf)
    with pytest.raises(QuoteUnavailable, match="IDENTITY_CHANGED|TRADE_UNAVAILABLE"):
        source.trade("LEAD", AT, timedelta(seconds=60))


def test_review_command_reports_a_busy_store_instead_of_crashing(tmp_path, monkeypatch):
    import io
    from desk import quote_mapping
    from tests.test_quote_risk import Terminal, captures
    webull, tasty = captures(tmp_path)
    path = tmp_path / "store.json"
    MappingStore(path).add(record())
    original = MappingStore.__init__

    def impatient(self, path, *, lock_timeout=0.2):  # the command's own store, with a short wait
        original(self, path, lock_timeout=lock_timeout)
    monkeypatch.setattr(MappingStore, "__init__", impatient)
    fd = hold_exclusive(path)
    out = io.StringIO()
    try:
        code = quote_mapping.main(["--store", str(path), "review", "--symbol", "LEAD", "--webull-capture", str(webull),
                                   "--tastytrade-capture", str(tasty), "--reviewer", "fixture:automated-test"],
                                  stdin=Terminal("MAP LEAD\n"), stdout=out)
    finally:
        os.close(fd)
    assert code == 1 and "QUOTE_MAPPING_STORE_BUSY" in out.getvalue() and "Nothing was changed" in out.getvalue()


@pytest.mark.parametrize("pinned,verified,code", [
    (["id:LEAD", "USD", "TEST", "ETF"], True, "QUOTE_MAPPING_CLASSIFICATION_MISMATCH"),
    (["id:OTHER", "USD", "TEST", "COMMON_STOCK"], True, "WEBULL_IDENTITY_MISMATCH"),
    (["id:LEAD", "USD", "TEST", "ETF"], False, "WEBULL_IDENTITY_HEALTH_INCONSISTENT")])
def test_pinned_webull_identity_changed_between_recheck_and_commit_refuses(tmp_path, pinned, verified, code):
    """The final fence re-verifies the mapping against the verified Webull identity, not
    only the bound digests. Synthetic: the pin is edited directly in the vendor store
    (the vendor path itself never re-pins). With ``verified`` a matching VERIFIED outcome
    is appended, so the mapping (ETF) or the signal's own instrument (OTHER) refuses;
    without it the raw edit is a pin no outcome verified (H1)."""
    from contextlib import closing
    import sqlite3
    q, tickets, tid, version = prepared(tmp_path)
    adapters = q.inputs()
    observe = adapters.observe

    def repin(*args):
        result = observe(*args)
        with closing(sqlite3.connect(q.desk.vendor.store.path)) as db, db:
            text = json.dumps(pinned)
            db.execute("UPDATE identities SET identity=? WHERE symbol='LEAD'", (text,))
            if verified:
                db.execute("INSERT INTO identity_events(host,symbol,event,identity,observed,at) "
                           "VALUES ('api.sandbox.webull.com','LEAD','VERIFIED',?,?,?)", (text, text, q.at.isoformat()))
        return result
    with pytest.raises(TicketError, match=code):
        approve(tickets, tid, version, replace(adapters, observe=repin), now=q.at)
    assert tickets.get(tid, version, now=q.at)["state"] == "pending"


def test_signal_armed_on_a_different_webull_instrument_than_the_review_refuses(tmp_path):
    """The signal's own stored Webull basis must match the review, even when the current
    pin and the review agree with each other (the signal was armed on another instrument)."""
    store = MappingStore(tmp_path / "m.json")
    store.add(record())
    identity = instrument("LEAD", stock("LEAD"), AT)
    pinned = {"instrument_id": "id:LEAD", "currency": "USD", "sub_category": "COMMON_STOCK"}
    armed_on_old = {"host": "api.sandbox.webull.com", "security_id": "id:OLD-LEAD", "currency": "USD", "symbol": "LEAD"}
    with pytest.raises(QuoteUnavailable, match="QUOTE_MAPPING_WEBULL_MISMATCH"):
        store.verify("LEAD", price_basis=armed_on_old, identity=identity, environment="production",
                     webull_identity=pinned)
    current = dict(armed_on_old, security_id="id:LEAD")
    assert store.verify("LEAD", price_basis=current, identity=identity, environment="production",
                        webull_identity=pinned).desk_symbol == "LEAD"
