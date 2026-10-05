"""Child 3, H1: Webull identity health reaches final approval and consumption.

Synthetic throughout: the provider is the vendor-basis fixture (no network), the
quote mapping is the labelled fixture record, and no order is sent. A contradictory
identity is observed by a separate ``VendorBasisSource`` sharing the vendor store
through the public ``bars`` path, as in Astra's 318b877 probe.
"""
from contextlib import closing
from dataclasses import replace
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from threading import Event, Thread
import time

import pytest

from desk import vendor_basis
from desk.risk_terms import EvidenceUnavailable
from desk.security import SecurityMetadata
from desk.tickets import TicketError, TicketStore
from desk.vendor_basis import VendorBasisSource, VendorHistoryStore, event_identity, verified_identity
from tests.quote_support import QuoteDesk
from tests.ticket_support import approve

HOST = "api.sandbox.webull.com"
REPO = __import__("pathlib").Path(__file__).resolve().parents[1]
DONE = {"approve": "approved", "consume": "consumed"}


def prepared(tmp_path, *, approved=False):
    q = QuoteDesk(tmp_path)
    tickets = TicketStore(tmp_path / "tickets.sqlite")
    tid, version = tickets.prepare(q.request(), q.inputs(), now=q.at)
    if approved:
        approve(tickets, tid, version, q.inputs(), now=q.at)
    return q, tickets, tid, version


def act(q, tickets, tid, version, operation, inputs=None):
    inputs = inputs or q.inputs()
    if operation == "approve":
        approve(tickets, tid, version, inputs, now=q.at)
    else:
        tickets.consume(tid, version, request_id="fixture:h1", inputs=inputs, now=q.at)


def peer(q):
    """A separate vendor source and store object over the same file (as another run)."""
    return VendorBasisSource(q.desk.native, VendorHistoryStore(q.desk.vendor.store.path),
                             host=q.desk.vendor.host, clock_fn=lambda: q.at)


def contradict(q, kind, symbol="LEAD"):
    """Make the provider's metadata contradict the pin: another instrument, or an ETF."""
    native = q.desk.native
    if kind == "id":
        native.ids[symbol] = "replacement-company"
        return
    original = type(native).security_metadata

    def etf(self, symbols):
        rows = original(self, symbols)
        for row in rows:
            if row["symbol"] == symbol:
                row["sub_category"] = "ETF"
        return rows
    native.security_metadata = etf.__get__(native)


def restore(q, symbol="LEAD"):
    q.desk.native.ids.pop(symbol, None)
    q.desk.native.__dict__.pop("security_metadata", None)


def state(q, symbol="LEAD"):
    return VendorHistoryStore(q.desk.vendor.store.path).identity_state(HOST, symbol)["state"]


def at_commit_hook(tickets, operation, start_writer):
    """Run start_writer() at the final audit write: final checks done, COMMIT pending."""
    original, seen = tickets._audit, {}

    def hook(*args, **kwargs):
        if args[4] == DONE[operation]:
            seen["writer"] = start_writer()
        return original(*args, **kwargs)
    tickets._audit = hook
    return seen


def after_recheck(adapters, hook):
    """Run hook() after the recheck's market observation: the final fence is still ahead."""
    observe = adapters.observe

    def observed(*args):
        result = observe(*args)
        hook()
        return result
    return replace(adapters, observe=observed)


# ---- committed before the final check ------------------------------------------------------------
@pytest.mark.parametrize("kind", ["id", "etf"])
@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_contradiction_committed_before_the_final_check_refuses(tmp_path, operation, kind):
    q, tickets, tid, version = prepared(tmp_path, approved=operation == "consume")
    other = peer(q)

    def refresh():
        contradict(q, kind)
        assert other.bars(["LEAD"], timespan="D", category="US_STOCK") == {}
        assert other.last_errors["LEAD"] == "SECURITY_IDENTITY_CHANGED"
    with pytest.raises(TicketError, match="WEBULL_IDENTITY_FAILED:SECURITY_IDENTITY_CHANGED"):
        act(q, tickets, tid, version, operation, after_recheck(q.inputs(), refresh))
    assert tickets.get(tid, version, now=q.at)["state"] == {"approve": "pending", "consume": "approved"}[operation]
    # The prior pin is kept as evidence; the contradictory observation is recorded beside it.
    health = VendorHistoryStore(q.desk.vendor.store.path).identity_state(HOST, "LEAD")
    assert health["identity"]["instrument_id"] == "id:LEAD" and health["identity"]["sub_category"] == "COMMON_STOCK"
    assert health["observed"] == ({"instrument_id": "replacement-company", "currency": "USD", "exchange_code": "TEST",
                                   "sub_category": "COMMON_STOCK"} if kind == "id" else
                                  {"instrument_id": "id:LEAD", "currency": "USD", "exchange_code": "TEST",
                                   "sub_category": "ETF"})


def test_the_generic_event_source_refuses_too(tmp_path):
    """The base EventRiskSource (no quote bridge) carries the same identity check."""
    q = QuoteDesk(tmp_path)
    q.desk.price = float(q.limit)
    tickets = TicketStore(tmp_path / "tickets.sqlite")
    request = q.request(quote_source="fixture")
    q.desk.snapshot()
    tid, version = tickets.prepare(request, q.desk.adapters(), now=q.at)
    assert tickets.get(tid, version, now=q.at)["state"] == "pending"

    def refresh():
        contradict(q, "id")
        peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")
    with pytest.raises(TicketError, match="WEBULL_IDENTITY_FAILED"):
        approve(tickets, tid, version, after_recheck(q.desk.adapters(), refresh), now=q.at)
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_FAILED"):
        q.desk.adapters().terms_source.resolve(q.event_id, q.at)


# ---- a writer arriving during the fence waits for the ticket COMMIT --------------------------------
@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_writer_thread_waits_until_the_ticket_commits(tmp_path, operation):
    q, tickets, tid, version = prepared(tmp_path, approved=operation == "consume")
    other, done, errors = peer(q), Event(), []

    def start():
        def write():
            try:
                contradict(q, "id")
                other.bars(["LEAD"], timespan="D", category="US_STOCK")
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))
            finally:
                done.set()
        Thread(target=write).start()
        waiting = not done.wait(0.5)
        return waiting, state(q)  # the outcome is not committed while the fence is held
    seen = at_commit_hook(tickets, operation, start)
    act(q, tickets, tid, version, operation)
    assert seen["writer"] == (True, "VERIFIED")
    assert done.wait(15) and errors == [] and other.last_errors["LEAD"] == "SECURITY_IDENTITY_CHANGED"
    assert tickets.get(tid, version, now=q.at)["state"] == DONE[operation]
    assert state(q) == "FAILED"
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_FAILED:SECURITY_IDENTITY_CHANGED"):
        q.source().resolve(q.event_id, q.at)  # every later use sees it


WRITER = """
import json, pathlib, sys
from desk.security import SecurityMetadata
from desk.vendor_basis import VendorHistoryStore
from desk.bars import BarDataError
store = VendorHistoryStore(sys.argv[1])
meta = SecurityMetadata.model_validate_json(sys.argv[2])
pathlib.Path(sys.argv[3]).write_text("starting")
checks = store.open_identity_checks("api.sandbox.webull.com", [meta.symbol], meta.observed_at)
try:
    store.pin("api.sandbox.webull.com", meta, check=checks[meta.symbol])
except BarDataError as exc:
    print("RECORDED", exc)
"""


@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_separate_process_writer_waits_until_the_ticket_commits(tmp_path, operation):
    q, tickets, tid, version = prepared(tmp_path, approved=operation == "consume")
    meta = SecurityMetadata(symbol="LEAD", instrument_id="replacement-company", name="LEAD", category="US_STOCK",
                            sub_category="COMMON_STOCK", exchange_code="TEST", currency="USD",
                            observed_at=q.at.isoformat())
    marker = tmp_path / "writer-started"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(REPO / "src"), str(REPO)]))
    procs = []

    def start():
        proc = subprocess.Popen([sys.executable, "-c", WRITER, str(q.desk.vendor.store.path),
                                 meta.model_dump_json(), str(marker)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(proc)
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.5)
        return marker.exists(), proc.poll() is None, state(q)
    seen = at_commit_hook(tickets, operation, start)
    act(q, tickets, tid, version, operation)
    out, err = procs[0].communicate(timeout=30)
    assert seen["writer"] == (True, True, "VERIFIED"), err
    assert procs[0].returncode == 0 and "RECORDED SECURITY_IDENTITY_CHANGED" in out, err
    assert tickets.get(tid, version, now=q.at)["state"] == DONE[operation]
    assert state(q) == "FAILED"


# ---- persistence, missing, legacy, unresolved and unpersistable evidence ---------------------------
READER = """
import sys
from desk.vendor_basis import VendorHistoryStore
print(VendorHistoryStore(sys.argv[1]).identity_state("api.sandbox.webull.com", "LEAD")["state"])
"""


def test_failure_survives_reopened_stores_and_another_process(tmp_path):
    q = QuoteDesk(tmp_path)
    contradict(q, "id")
    peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")
    restore(q)
    q.desk.restart()  # new signal, account and ticket store objects
    reopened = VendorBasisSource(q.desk.native, VendorHistoryStore(q.desk.vendor.store.path), host=HOST,
                                 clock_fn=lambda: q.at)
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_FAILED:SECURITY_IDENTITY_CHANGED"):
        verified_identity(reopened, HOST, "LEAD")
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_FAILED"):
        q.source().resolve(q.event_id, q.at)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(REPO / "src"), str(REPO)]))
    out = subprocess.run([sys.executable, "-c", READER, str(q.desk.vendor.store.path)], env=env,
                         capture_output=True, text=True, timeout=30, check=True).stdout
    assert out.strip() == "FAILED"


def raw(q, *statements):
    with closing(sqlite3.connect(q.desk.vendor.store.path)) as db, db:
        for statement in statements:
            db.execute(statement)


def test_legacy_pin_without_health_is_unverified_until_a_normal_refresh(tmp_path):
    """Migration: an old store has pins and no identity health. The next bounded vendor
    refresh (the scan's own metadata request) verifies it; no review or manual entry."""
    q = QuoteDesk(tmp_path)
    raw(q, "DROP TABLE identity_events")
    VendorHistoryStore(q.desk.vendor.store.path)  # reopening adds the table, empty
    assert state(q) == "UNVERIFIED"
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_UNVERIFIED"):
        q.source().resolve(q.event_id, q.at)
    assert peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")
    assert state(q) == "VERIFIED"
    q.source().resolve(q.event_id, q.at)


def test_absent_or_unreadable_identity_evidence_refuses(tmp_path):
    q = QuoteDesk(tmp_path)
    raw(q, "DELETE FROM identities")
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_NOT_PINNED"):
        q.source().resolve(q.event_id, q.at)
    other = QuoteDesk(tmp_path / "corrupt")
    raw(other, "UPDATE identities SET identity='not json'")
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_HEALTH_INCONSISTENT"):
        other.source().resolve(other.event_id, other.at)
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_STORE_UNAVAILABLE"):
        verified_identity(other.desk.src, "api.webull.com", "LEAD")  # another host's evidence
    basis = dict(other.event()["signal"]["price_basis"], security_id="id:OLD-LEAD")
    restore(other)
    raw(other, "UPDATE identities SET identity='[\"id:LEAD\", \"USD\", \"TEST\", \"COMMON_STOCK\"]'")
    peer(other).bars(["LEAD"], timespan="D", category="US_STOCK")
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_MISMATCH"):
        event_identity(other.desk.src, basis, "LEAD")  # armed on another instrument


def test_an_open_check_withholds_until_a_later_check_resolves_it(tmp_path):
    """A process that stopped after sending the metadata request left no outcome."""
    q = QuoteDesk(tmp_path)
    VendorHistoryStore(q.desk.vendor.store.path).open_identity_checks(HOST, ["LEAD"], q.at)
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_REFRESH_UNRESOLVED"):
        q.source().resolve(q.event_id, q.at)
    assert peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")
    assert state(q) == "VERIFIED"


def test_an_unpersistable_outcome_leaves_the_check_open(tmp_path, monkeypatch):
    q = QuoteDesk(tmp_path)
    other = peer(q)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(other.store, "pin", broken)
    contradict(q, "id")
    assert other.bars(["LEAD"], timespan="D", category="US_STOCK") == {}
    assert other.last_errors["LEAD"] == "IDENTITY_STORE_UNAVAILABLE"
    assert state(q) == "REFRESH_UNRESOLVED"  # the contradiction was seen but not recorded
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_REFRESH_UNRESOLVED"):
        q.source().resolve(q.event_id, q.at)


def test_checks_that_cannot_be_opened_send_no_metadata_request(tmp_path, monkeypatch):
    q = QuoteDesk(tmp_path)
    other = peer(q)
    calls = []
    original = type(q.desk.native).security_metadata
    q.desk.native.security_metadata = lambda symbols: calls.append(symbols) or original(q.desk.native, symbols)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(other.store, "open_identity_checks", broken)
    assert other.bars(["LEAD"], timespan="D", category="US_STOCK") == {}
    assert calls == [] and other.last_errors["LEAD"].startswith("IDENTITY_STORE_UNAVAILABLE")
    assert state(q) == "VERIFIED"  # nothing was observed, so nothing changed


# ---- controls: unchanged refresh, healthy peer, recovery, unrelated evidence ------------------------
@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_unchanged_refresh_and_a_failing_peer_still_succeed(tmp_path, operation):
    from tests.test_vendor_basis import Native
    q, tickets, tid, version = prepared(tmp_path, approved=operation == "consume")
    other = peer(q)
    elsewhere = Native()  # another watchlist name through the same store
    elsewhere.now = q.at
    peers = VendorBasisSource(elsewhere, VendorHistoryStore(q.desk.vendor.store.path), host=HOST,
                              clock_fn=lambda: q.at)

    def refresh():
        assert other.bars(["LEAD"], timespan="D", category="US_STOCK")
        assert peers.bars(["PEER"], timespan="D", category="US_STOCK")  # first pin of PEER
        elsewhere.ids["PEER"] = "replacement-company"
        assert peers.bars(["PEER"], timespan="D", category="US_STOCK") == {}
        assert state(q, "PEER") == "FAILED" and state(q) == "VERIFIED"
    act(q, tickets, tid, version, operation, after_recheck(q.inputs(), refresh))
    assert tickets.get(tid, version, now=q.at)["state"] == DONE[operation]


@pytest.mark.parametrize("kind", ["id", "etf"])
@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_a_successful_refresh_of_the_original_identity_recovers_without_review(tmp_path, operation, kind):
    q, tickets, tid, version = prepared(tmp_path, approved=operation == "consume")
    other = peer(q)
    contradict(q, kind)
    other.bars(["LEAD"], timespan="D", category="US_STOCK")
    assert state(q) == "FAILED"
    mapping = q.store.records()[0].mapping_digest
    restore(q)
    assert other.bars(["LEAD"], timespan="D", category="US_STOCK")
    assert state(q) == "VERIFIED"
    act(q, tickets, tid, version, operation)
    assert tickets.get(tid, version, now=q.at)["state"] == DONE[operation]
    assert q.store.records()[0].mapping_digest == mapping  # the same reviewed record


def test_unrelated_successful_evidence_never_clears_a_failure(tmp_path):
    q = QuoteDesk(tmp_path)
    store = VendorHistoryStore(q.desk.vendor.store.path)
    contradict(q, "id")
    peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")
    with closing(sqlite3.connect(store.path)) as db:
        snapshot = json.loads(db.execute("SELECT payload FROM snapshots").fetchone()[0])
    from desk.data_basis import VendorPriceBasis
    store.record(HOST, "LEAD", q.at, "CONSISTENT", "generic bar observation", VendorPriceBasis(**snapshot))
    store.record(HOST, "LEAD", q.at, "CONSISTENT", "generic bar observation")
    store.record_defects(HOST, "LEAD", [{"reason": "x", "status": "EXCLUDED_OUTSIDE_SCOPE"}], instrument_id="id:LEAD",
                         scope="discovery", scope_digest="d", timeframe="D", received_at=None, requested={},
                         required={}, at=q.at)
    assert state(q) == "FAILED"


def test_a_history_row_defect_does_not_touch_identity(tmp_path):
    q, tickets, tid, version = prepared(tmp_path)
    VendorHistoryStore(q.desk.vendor.store.path).record_defects(
        HOST, "LEAD", [{"reason": "older excluded row", "status": "EXCLUDED_OUTSIDE_SCOPE"}],
        instrument_id="id:LEAD", scope="discovery", scope_digest="d", timeframe="D", received_at=None,
        requested={}, required={}, at=q.at)
    assert state(q) == "VERIFIED"
    act(q, tickets, tid, version, "approve")


def test_wrong_category_or_unsupported_type_never_creates_a_pin(tmp_path):
    store = VendorHistoryStore(tmp_path / "vendor.sqlite")
    meta = dict(symbol="NEW", instrument_id="id:NEW", name="NEW", category="US_STOCK", exchange_code="TEST",
                currency="USD", observed_at="2026-09-29T10:00:00-04:00")
    checks = store.open_identity_checks(HOST, ["NEW"], "2026-09-29T10:00:00-04:00")
    store.pin(HOST, SecurityMetadata(sub_category="WARRANT", **meta), check=checks["NEW"], accept=False)
    assert store.pinned(HOST, "NEW") is None and store.identity_state(HOST, "NEW")["state"] == "NOT_PINNED"
    store.pin(HOST, SecurityMetadata(sub_category="COMMON_STOCK", **meta))
    with pytest.raises(Exception, match="SECURITY_IDENTITY_CHANGED"):
        store.pin(HOST, SecurityMetadata(sub_category="WARRANT", **meta), accept=False)
    assert store.identity_state(HOST, "NEW")["state"] == "FAILED"


# ---- the fence: busy, missing or corrupt stores refuse without leaking a lock ----------------------
def test_busy_identity_fence_refuses_then_releases(tmp_path, monkeypatch):
    q, tickets, tid, version = prepared(tmp_path)
    monkeypatch.setattr(vendor_basis, "FENCE_TIMEOUT_SECONDS", 0.2)
    holder = sqlite3.connect(q.desk.vendor.store.path, isolation_level=None)

    def hold():
        holder.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    with pytest.raises(TicketError, match="WEBULL_IDENTITY_STORE_BUSY"):
        approve(tickets, tid, version, after_recheck(q.inputs(), hold), now=q.at)
    assert time.monotonic() - started < 30
    holder.execute("ROLLBACK")
    holder.close()
    probe = sqlite3.connect(q.desk.vendor.store.path, timeout=0, isolation_level=None)
    probe.execute("BEGIN IMMEDIATE")  # nothing left holding the reservation
    probe.execute("ROLLBACK")
    probe.close()
    approve(tickets, tid, version, q.inputs(), now=q.at)
    assert tickets.get(tid, version, now=q.at)["state"] == "approved"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_store_refuses_without_creating_or_leaking(tmp_path, damage):
    q, tickets, tid, version = prepared(tmp_path)
    path = q.desk.vendor.store.path
    backup = tmp_path / "vendor-backup.sqlite"
    shutil.copy(path, backup)

    def break_store():
        path.unlink()
        if damage == "corrupt":
            path.write_bytes(b"not a database" * 100)
    with pytest.raises(TicketError, match="WEBULL_IDENTITY_STORE_UNAVAILABLE"):
        approve(tickets, tid, version, after_recheck(q.inputs(), break_store), now=q.at)
    assert path.exists() is (damage == "corrupt")  # a missing store is never created
    assert tickets.get(tid, version, now=q.at)["state"] == "pending"
    if path.exists():
        path.unlink()
    shutil.copy(backup, path)
    approve(tickets, tid, version, q.inputs(), now=q.at)  # signal/account/ticket locks were released
    assert tickets.get(tid, version, now=q.at)["state"] == "approved"
