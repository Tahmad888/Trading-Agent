"""G5 re-audit R2b: a restart must not revive an old ticket or approval.

A check that wrote a durable START and was then overtaken by a later check's OK
before it recorded any outcome changes the evidence generation, durably. Real
competing SQLite locks against scratch stores; injected transports. Zero network,
no credentials.
"""
import json
import os
import pathlib

import pytest

from desk.tastytrade_quotes import QuoteUnavailable
from desk.tickets import TicketError
from desk.tradier_client import TradierClient
from desk.tradier_quotes import QuoteStore, digest, requested
from desk.tradier_risk import compose
from tests.test_tradier_local_store import ShortTimeoutStore, blocked, events, operational, read
from tests.test_tradier_quotes_runtime import AT, Desk, client, fetch, raw
from tests.ticket_support import approve

LEAD = [requested("LEAD", "stock")]


def abandon_check(store, rows):
    """Astra's 2026-10-10 fault: START is durable, then the store stays locked until the call fails."""
    store.__class__ = ShortTimeoutStore
    state = {}

    def lock_during_reply(method, url, headers, data):
        state["lock"] = blocked(store.path)
        state["lock"].__enter__()
        return {"quotes": {"quote": rows}}
    c = TradierClient("fixture-only", request=lock_during_reply, clock=lambda: AT)
    with pytest.raises(QuoteUnavailable, match="^LOCAL_QUOTE_STORE_BUSY$"):
        store.fetch(c, LEAD)
    state.pop("lock").__exit__(None, None, None)


def restarted(d):
    """A new process: a new store object and composer; nothing held in memory carries over."""
    store = QuoteStore(d.store.path)
    return store, compose(d.fixture.inputs(clock=lambda: d.fixture.at), price_source=d.fixture.desk.src,
                          log=d.fixture.desk.log, store=store, client=client(d.rows, d.fixture.at))


def price(store, symbol="LEAD", component="trade"):
    return read(store, symbol, component)[0]["view"]["prices"]["last"]["value"]


# ---- Astra's regressions (portable copies) and recovery -------------------------------------
@pytest.mark.parametrize("operation", ["approve", "consume"])
def test_old_ticket_and_old_approval_stay_dead_after_restart_and_healthy_refresh(tmp_path, operation):
    d = Desk(tmp_path)
    tid, ver = d.prepare()
    if operation == "consume":
        assert approve(d.tickets, tid, ver, d.inputs, now=d.fixture.at)
    abandon_check(d.store, d.rows)
    store, adapters = restarted(d)
    with store.held() as db:
        with pytest.raises(QuoteUnavailable, match="TRADIER_LATEST_CHECK_UNAVAILABLE"):
            store.read(db, "production", "LEAD", "quote", d.fixture.at)   # before recovery
    with pytest.raises(TicketError, match="changed|differ"):
        if operation == "approve":
            approve(d.tickets, tid, ver, adapters, now=d.fixture.at)
        else:
            d.tickets.consume(tid, ver, inputs=adapters, request_id="old-approval", now=d.fixture.at)
    assert not [e for e in events(store) if e["action"] in ("STOP", "RESUME")]   # local, no resume


def test_a_new_ticket_after_recovery_approves_and_consumes(tmp_path):
    d = Desk(tmp_path)
    d.prepare()
    abandon_check(d.store, d.rows)
    store, adapters = restarted(d)
    tid, ver = d.tickets.prepare(d.request, adapters, now=d.fixture.at)
    _, adapters = restarted(d)                                    # another clean restart in between
    assert approve(d.tickets, tid, ver, adapters, now=d.fixture.at)
    assert d.tickets.consume(tid, ver, inputs=adapters, request_id="new-ticket", now=d.fixture.at)


def test_failure_after_start_then_reopen_and_healthy_refresh_changes_the_generation(tmp_path):
    s = QuoteStore(tmp_path / "q.sqlite")
    fetch(s)
    s.fetch(client([raw("GOOD", at=AT)]), [requested("GOOD", "stock")])
    before = {c: read(s, "LEAD", c)[1] for c in ("trade", "quote")}
    peer = {c: read(s, "GOOD", c)[1] for c in ("trade", "quote")}
    abandon_check(s, [raw("LEAD", at=AT)])
    reopened = QuoteStore(s.path)
    for component in ("trade", "quote"):
        with pytest.raises(QuoteUnavailable, match="TRADIER_LATEST_CHECK_UNAVAILABLE"):
            read(reopened, "LEAD", component)
    fetch(reopened)
    after = {c: read(reopened, "LEAD", c)[1] for c in ("trade", "quote")}
    assert all(after[c] != before[c] for c in after)
    assert {c: read(reopened, "GOOD", c)[1] for c in ("trade", "quote")} == peer   # healthy peer untouched
    QuoteStore(s.path)                                            # survives another reopen
    assert {c: read(QuoteStore(s.path), "LEAD", c)[1] for c in ("trade", "quote")} == after
    fetch(reopened)                                               # an ordinary refresh after recovery
    assert {c: read(reopened, "LEAD", c)[1] for c in ("trade", "quote")} == after


def test_control_clean_restart_and_ordinary_refreshes_keep_the_old_generation(tmp_path):
    d = Desk(tmp_path)
    tid, ver = d.prepare()
    with d.store.held() as db:
        _, generation = d.store.read(db, "production", "LEAD", "quote", d.fixture.at)
        instance = db.execute("SELECT value FROM meta WHERE key='instance'").fetchone()[0]
    assert generation == digest([instance, 0, 0])                 # same formula as before R2b
    for _ in range(3):
        fetch(QuoteStore(d.store.path), d.rows, d.fixture.at)
    store, adapters = restarted(d)
    with store.held() as db:
        assert store.read(db, "production", "LEAD", "quote", d.fixture.at)[1] == generation
    assert approve(d.tickets, tid, ver, adapters, now=d.fixture.at)


def test_the_marker_is_per_component(tmp_path):
    s = QuoteStore(tmp_path / "q.sqlite")
    fetch(s)
    with s.held() as db:                                          # a check that only recorded its quote
        start = s.add(db, "production|LEAD", "START")
        s.add(db, "production|LEAD|quote", "OK", start,
              json.loads(db.execute("SELECT payload FROM events WHERE key='production|LEAD|quote' "
                                    "AND action='OK'").fetchone()[0]))
    with s.held() as db:
        instance = db.execute("SELECT value FROM meta WHERE key='instance'").fetchone()[0]
    quote = read(s, "LEAD", "quote")[1]
    assert quote == digest([instance, 0, 0])
    with pytest.raises(QuoteUnavailable, match="TRADIER_LATEST_CHECK_UNAVAILABLE"):
        read(s, "LEAD", "trade")
    fetch(s)
    assert read(s, "LEAD", "quote")[1] == quote                   # the quote check had its outcome
    assert read(s, "LEAD", "trade")[1] != digest([instance, 0, 0])   # the trade check was overtaken


# ---- overlapping checks: no age inference, nothing written for another check ----------------
def test_active_overlap_invalidates_once_and_a_late_old_ok_changes_nothing(tmp_path):
    s = QuoteStore(tmp_path / "q.sqlite")
    fetch(s)
    before = read(s, "LEAD")[1]
    older = s.begin("production", LEAD)                           # still running in another process
    newer = s.begin("production", LEAD)
    s.finish("production", LEAD, newer, [raw(at=AT, price="51")], AT)
    middle = read(s, "LEAD")[1]
    assert middle != before and price(s) == "51"
    assert not [e for e in events(s) if e["action"] == "FAILED"]  # the running check is not marked failed
    s.finish("production", LEAD, older, [raw(at=AT, price="49")], AT)   # its late healthy result
    assert read(s, "LEAD")[1] == middle and price(s) == "51"     # newer start stays latest
    fetch(s, [raw(at=AT, price="52")])
    assert read(s, "LEAD")[1] == middle and price(s) == "52"


def test_a_late_old_failure_makes_the_name_unavailable_until_a_fresh_check(tmp_path):
    s = QuoteStore(tmp_path / "q.sqlite")
    fetch(s)
    older = s.begin("production", LEAD)
    newer = s.begin("production", LEAD)
    s.finish("production", LEAD, newer, [raw(at=AT)], AT)
    middle = read(s, "LEAD")[1]
    s.finish("production", LEAD, older, [], AT)                   # the older check failed after all
    with pytest.raises(QuoteUnavailable, match="TRADIER_LATEST_CHECK_UNAVAILABLE"):
        read(s, "LEAD")
    fetch(s)
    assert read(s, "LEAD")[1] != middle


def test_history_is_only_appended(tmp_path):
    d = Desk(tmp_path)
    d.prepare()
    first = events(d.store)
    abandon_check(d.store, d.rows)
    store, adapters = restarted(d)
    d.tickets.prepare(d.request, adapters, now=d.fixture.at)
    later = events(store)
    assert later[:len(first)] == first and len(later) > len(first)
    with store.held() as db, pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM events WHERE action='START'")


# ---- a provider STOP the store refused to record -------------------------------------------
def stop_while_locked(s, sends):
    state = {}

    def reply(method, url, headers, data):
        sends.append(url)
        state["lock"] = blocked(s.path)
        state["lock"].__enter__()
        raise QuoteUnavailable("REST_HTTP_401")
    c = TradierClient("fixture-only", operational=True, reserve_request=s.reserve_request,
                      request=reply, clock=lambda: AT)
    with pytest.raises(QuoteUnavailable, match="^REST_HTTP_401$"):
        s.fetch(c, LEAD)
    return lambda: state.pop("lock").__exit__(None, None, None)


def stop_files(path):
    return sorted(path.parent.glob(path.name + ".stop-production.*"))


def test_an_unrecorded_provider_stop_survives_a_restart_and_sends_nothing(tmp_path):
    s = ShortTimeoutStore(tmp_path / "q.sqlite")
    sends = []
    s.fetch(operational(s, sends), [requested("GOOD", "stock")])
    release = stop_while_locked(s, sends)
    release()
    held, = stop_files(s.path)
    assert held.stat().st_mode & 0o777 == 0o600 and json.loads(held.read_text()) == {"reason": "REST_HTTP_401"}
    assert not s._stopped                                         # not an in-memory claim
    new = ShortTimeoutStore(s.path)                               # restart before anything recorded it
    with pytest.raises(QuoteUnavailable, match="EXPLICIT_RESUME_REQUIRED"):
        read(new, "GOOD")
    with pytest.raises(QuoteUnavailable, match="EXPLICIT_RESUME_REQUIRED"):
        new.fetch(operational(new, sends), [requested("GOOD", "stock")])
    assert len(sends) == 2 and not stop_files(s.path)             # nothing sent; STOP now in the store
    stops = [json.loads(e["payload"]) for e in events(new) if e["action"] == "STOP"]
    assert stops == [{"reason": "REST_HTTP_401", "recorded_from": held.name}]
    with pytest.raises(QuoteUnavailable, match="EXPLICIT_RESUME_REQUIRED"):
        read(new, "GOOD")
    new.resume("production")
    new.fetch(operational(new, sends), [requested("GOOD", "stock")])
    assert read(new, "GOOD") and len(sends) == 3


def test_resume_records_a_pending_stop_file_before_its_resume(tmp_path):
    s = ShortTimeoutStore(tmp_path / "q.sqlite")
    sends = []
    stop_while_locked(s, sends)()
    new = ShortTimeoutStore(s.path)
    new.resume("production")
    assert [e["action"] for e in events(new) if e["key"] == "production|*"] == ["STOP", "RESUME"]
    assert not stop_files(s.path)
    with pytest.raises(QuoteUnavailable, match="TRADIER_LATEST_CHECK_UNAVAILABLE"):
        read(new, "LEAD")                                         # resume revives nothing


def test_a_stop_file_is_recorded_once_and_a_bad_one_still_stops(tmp_path, monkeypatch):
    s = QuoteStore(tmp_path / "q.sqlite")
    fetch(s)
    bad = s.path.with_name(s.path.name + ".stop-production.manual")
    bad.write_text("not json")
    odd = s.path.with_name(s.path.name + ".stop-production.text")
    odd.write_text(json.dumps({"reason": "free text from the file"}))   # only fixed codes are recorded
    with pytest.raises(QuoteUnavailable, match="EXPLICIT_RESUME_REQUIRED"):
        read(s, "LEAD")
    sandbox = s.path.with_name(s.path.name + ".stop-sandbox.other")
    sandbox.write_text(json.dumps({"reason": "REST_HTTP_403"}))
    with monkeypatch.context() as m:                              # a copier that died before tidying up
        m.setattr(pathlib.Path, "unlink", lambda self, missing_ok=False: None)
        s.resume("production")
    QuoteStore(s.path).resume("production")
    stops = [json.loads(e["payload"]) for e in events(s) if e["key"] == "production|*" and e["action"] == "STOP"]
    assert stops == [{"reason": "TRADIER_FETCH_FAILURE", "recorded_from": bad.name},
                     {"reason": "TRADIER_FETCH_FAILURE", "recorded_from": odd.name}]
    assert not bad.exists() and not odd.exists() and sandbox.exists()                  # environments stay separate


def test_bound_when_neither_the_row_nor_the_stop_file_can_be_written(tmp_path, monkeypatch):
    """The documented limit: held by this process only; a restart may send once more."""
    s = ShortTimeoutStore(tmp_path / "q.sqlite")
    sends = []
    s.fetch(operational(s, sends), [requested("GOOD", "stock")])
    real = os.open

    def no_stop_file(path, *args, **kwargs):
        if ".stop-" in str(path):
            raise OSError(28, "No space left on device")
        return real(path, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(os, "open", no_stop_file)
        release = stop_while_locked(s, sends)
    release()
    assert not stop_files(s.path) and s._stopped == {"production": "REST_HTTP_401"}
    with pytest.raises(QuoteUnavailable, match="EXPLICIT_RESUME_REQUIRED"):
        read(s, "GOOD")                                           # this process holds it
    new = ShortTimeoutStore(s.path)
    with pytest.raises(QuoteUnavailable, match="TRADIER_LATEST_CHECK_UNAVAILABLE"):
        read(new, "LEAD")                                         # the open START still refuses LEAD
    with pytest.raises(QuoteUnavailable, match="^REST_HTTP_401$"):
        new.fetch(operational(new, sends, error="REST_HTTP_401"), [requested("GOOD", "stock")])
    assert len(sends) == 3                                        # one more request, then a recorded STOP
    with pytest.raises(QuoteUnavailable, match="EXPLICIT_RESUME_REQUIRED"):
        read(QuoteStore(s.path), "GOOD")
