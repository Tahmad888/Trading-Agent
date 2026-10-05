"""Child 3, O1: identity outcomes are ordered by when their check was opened.

An older metadata check that finishes after a newer contradiction must not clear it;
only a check opened after the failure can verify again. Synthetic fixture provider,
public ``VendorBasisSource.bars`` path, real temporary stores; no network, no order.
"""
from datetime import timedelta
from threading import Event, Thread

import pytest

from desk.risk_terms import EvidenceUnavailable
from desk.security import SecurityMetadata
from desk.tickets import TicketError
from desk.vendor_basis import VendorBasisSource, VendorHistoryStore
from tests.test_identity_health import (DONE, HOST, act, after_recheck, contradict, peer, prepared, restore,
                                        state)


class Held:
    """The provider, except that this check's metadata reply is captured, then held back."""

    def __init__(self, q, *, sees=None):
        self.q, self.sees, self.captured, self.release = q, sees, Event(), Event()

    def __getattr__(self, name):
        return getattr(self.q.desk.native, name)

    def security_metadata(self, symbols):
        rows = self.q.desk.native.security_metadata(symbols)
        for row in rows:
            row["observed_at"] = self.q.at - timedelta(seconds=1)  # observed before the newer check
            if self.sees:
                row.update(self.sees)
        self.captured.set()
        assert self.release.wait(15)
        return rows


def start_held(q, **kwargs):
    """Open check A and let it capture its (older) metadata, then hold the reply."""
    held = Held(q, **kwargs)
    source = VendorBasisSource(held, VendorHistoryStore(q.desk.vendor.store.path), host=HOST, clock_fn=lambda: q.at)
    thread = Thread(target=lambda: source.bars(["LEAD"], timespan="D", category="US_STOCK"))
    thread.start()
    assert held.captured.wait(15)
    return held, source, thread


def finish(held, thread):
    held.release.set()
    thread.join(15)
    assert not thread.is_alive()


def events(q):
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(q.desk.vendor.store.path)) as db:
        return db.execute("SELECT sequence,event,check_id FROM identity_events WHERE symbol='LEAD' "
                          "ORDER BY sequence").fetchall()


# ---- the demonstrated defect, through approval and consumption -----------------------------------
@pytest.mark.parametrize("kind", ["id", "etf"])
@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_an_older_check_finishing_after_a_newer_contradiction_cannot_clear_it(tmp_path, operation, kind):
    q, tickets, tid, version = prepared(tmp_path, approved=operation == "consume")

    def race():
        held, older, thread = start_held(q)            # check A: the original identity, held back
        contradict(q, kind)
        assert peer(q).bars(["LEAD"], timespan="D", category="US_STOCK") == {}  # newer check B: FAILED
        finish(held, thread)                           # A's VERIFIED is written after B's FAILED
        if kind == "id":  # A's own bars then carry the replacement instrument and are refused
            assert older.last_errors["LEAD"].startswith("BAR_IDENTITY_MISMATCH")
        trail = events(q)
        failed = next(e for e in trail if e[1] == "FAILED")
        late = trail[-1]
        assert late[1] == "VERIFIED" and late[0] > failed[0] and late[2] < failed[2]  # older check, later write
    with pytest.raises(TicketError, match="WEBULL_IDENTITY_FAILED:SECURITY_IDENTITY_CHANGED"):
        act(q, tickets, tid, version, operation, after_recheck(q.inputs(), race))
    assert tickets.get(tid, version, now=q.at)["state"] == {"approve": "pending", "consume": "approved"}[operation]
    # Reopened store, and an unrelated successful bar observation, leave the failure effective.
    store = VendorHistoryStore(q.desk.vendor.store.path)
    store.record(HOST, "LEAD", q.at, "CONSISTENT", "generic bar observation")
    assert store.identity_state(HOST, "LEAD")["state"] == "FAILED"
    with pytest.raises(EvidenceUnavailable, match="WEBULL_IDENTITY_FAILED"):
        q.source().resolve(q.event_id, q.at)


@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_a_check_opened_after_the_failure_recovers(tmp_path, operation):
    q, tickets, tid, version = prepared(tmp_path, approved=operation == "consume")

    def failure_then_later_check():
        held, _, thread = start_held(q)
        contradict(q, "id")
        peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")
        finish(held, thread)
        assert state(q) == "FAILED"
        restore(q)
        assert peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")  # opened after the failure
        assert state(q) == "VERIFIED"
    act(q, tickets, tid, version, operation, after_recheck(q.inputs(), failure_then_later_check))
    assert tickets.get(tid, version, now=q.at)["state"] == DONE[operation]


def test_an_older_failure_finishing_after_a_newer_verification_still_fails_closed(tmp_path):
    q = QuoteDeskLike(tmp_path)
    held, _, thread = start_held(q, sees={"instrument_id": "replacement-company"})  # A saw a contradiction
    assert peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")             # newer B: VERIFIED
    finish(held, thread)                                                          # A's FAILED written last
    assert state(q) == "FAILED"
    assert peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")             # a later-opened check
    assert state(q) == "VERIFIED"


def test_overlapping_healthy_checks_verify_in_either_completion_order(tmp_path):
    q = QuoteDeskLike(tmp_path)
    held, _, thread = start_held(q)
    assert peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")
    finish(held, thread)                     # older check completes last
    assert state(q) == "VERIFIED"
    held, _, thread = start_held(q)
    finish(held, thread)                     # older check completes first
    assert peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")
    assert state(q) == "VERIFIED"


def test_a_newer_check_still_open_withholds_the_older_outcome(tmp_path):
    q = QuoteDeskLike(tmp_path)
    held, _, thread = start_held(q)                                  # check A open
    store = VendorHistoryStore(q.desk.vendor.store.path)
    store.open_identity_checks(HOST, ["LEAD"], q.at)                 # newer check B opened, never closed
    finish(held, thread)                                             # A verifies after B was opened
    assert state(q) == "REFRESH_UNRESOLVED"


def test_an_outcome_without_a_check_never_clears_a_failure(tmp_path):
    q = QuoteDeskLike(tmp_path)
    contradict(q, "id")
    peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")
    assert state(q) == "FAILED"
    VendorHistoryStore(q.desk.vendor.store.path).pin(HOST, SecurityMetadata(
        symbol="LEAD", instrument_id="id:LEAD", name="LEAD", category="US_STOCK", sub_category="COMMON_STOCK",
        exchange_code="TEST", currency="USD", observed_at=q.at.isoformat()))  # direct call: no check ID
    assert events(q)[-1][1:] == ("VERIFIED", None)
    assert state(q) == "FAILED"


def test_a_peer_symbol_is_unaffected_by_the_overlap(tmp_path):
    q = QuoteDeskLike(tmp_path)
    store = VendorHistoryStore(q.desk.vendor.store.path)
    store.pin(HOST, SecurityMetadata(symbol="PEER", instrument_id="id:PEER", name="PEER", category="US_STOCK",
                                     sub_category="COMMON_STOCK", exchange_code="TEST", currency="USD",
                                     observed_at=q.at.isoformat()))
    held, _, thread = start_held(q)
    contradict(q, "id")
    peer(q).bars(["LEAD"], timespan="D", category="US_STOCK")
    finish(held, thread)
    assert state(q) == "FAILED" and state(q, "PEER") == "VERIFIED"


def QuoteDeskLike(tmp_path):
    from tests.quote_support import QuoteDesk
    return QuoteDesk(tmp_path)
