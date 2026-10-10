"""G5 backlog repair R2: local quote-store contention is not a provider STOP.

Real competing SQLite connections against scratch stores; the provider transport is an
injected function. Zero network, no credentials.
"""
from contextlib import contextmanager
import json
import sqlite3

import pytest

from desk.tastytrade_quotes import QuoteUnavailable
from desk.tradier_client import TradierClient
from desk.tradier_quotes import QuoteStore, main, requested
from tests.test_tradier_quotes_runtime import AT, Desk, client, raw
from tests.ticket_support import approve
from desk.tickets import TicketError


class ShortTimeoutStore(QuoteStore):
    """Only the SQLite wait is shortened (Astra's 2026-10-10 probe fixture)."""
    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=.03, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


@contextmanager
def blocked(path):
    """A real competing connection holding the database's exclusive lock."""
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")
    try:
        yield
    finally:
        other.execute("ROLLBACK")
        other.close()


def operational(store, sends, rows=lambda: [raw("GOOD", at=AT)], error=None):
    def reply(method, url, headers, data):
        sends.append(url)
        if error:
            raise QuoteUnavailable(error)
        return {"quotes": {"quote": rows()}}
    return TradierClient("fixture-only", operational=True, reserve_request=store.reserve_request,
                         request=reply, clock=lambda: AT)


def read(store, symbol, component="quote"):
    with store.held() as db:
        return store.read(db, "production", symbol, component, AT)


def events(store):
    with store.held() as db:
        return [dict(r) for r in db.execute("SELECT key,action,payload FROM events ORDER BY id")]


def contend_reservation(store, c, *, through_withdrawal=False):
    """Hold a real exclusive lock while the reservation runs (and optionally after it)."""
    reserve = store.reserve_request
    holder = {}

    def contended(env, at):
        holder["lock"] = blocked(store.path)
        holder["lock"].__enter__()
        try:
            return reserve(env, at)
        finally:
            if not through_withdrawal:
                holder.pop("lock").__exit__(None, None, None)
    c.reserve_request = contended
    return lambda: holder.pop("lock").__exit__(None, None, None) if "lock" in holder else None


def test_astra_real_lock_probe_is_local_and_never_stops_the_provider(tmp_path):
    s = ShortTimeoutStore(tmp_path / "q.sqlite")
    sends = []
    c = operational(s, sends)
    s.fetch(c, [requested("GOOD", "stock")])
    assert read(s, "GOOD")
    contend_reservation(s, c)
    with pytest.raises(QuoteUnavailable, match="^LOCAL_QUOTE_STORE_BUSY$"):
        s.fetch(c, [requested("LEAD", "stock")])
    assert len(sends) == 1                                        # no dispatch on a failed reservation
    reopened = ShortTimeoutStore(s.path)
    with reopened.held() as db:
        assert reopened.global_event(db, "production") is None   # no STOP
        assert reopened.read(db, "production", "GOOD", "quote", AT)   # unaffected peer
        with pytest.raises(QuoteUnavailable, match="TRADIER_LATEST_CHECK_UNAVAILABLE"):
            reopened.read(db, "production", "LEAD", "quote", AT)  # attempted name withdrawn
    failed = [e for e in events(reopened) if e["key"].startswith("production|LEAD|")]
    assert {json.loads(e["payload"])["reason"] for e in failed} == {"LOCAL_QUOTE_STORE_BUSY"}
    assert "locked" not in json.dumps(events(reopened))         # raw SQLite text never stored


def test_repeated_contention_then_a_normal_refresh_recovers_without_resume(tmp_path):
    s = ShortTimeoutStore(tmp_path / "q.sqlite")
    sends, rows = [], [raw("GOOD", at=AT)]
    c = operational(s, sends, rows=lambda: rows)
    s.fetch(c, [requested("GOOD", "stock")])
    for _ in range(3):
        c = operational(s, sends, rows=lambda: rows)
        contend_reservation(s, c)
        with pytest.raises(QuoteUnavailable, match="^LOCAL_QUOTE_STORE_BUSY$"):
            s.fetch(c, [requested("LEAD", "stock")])
    assert len(sends) == 1 and read(s, "GOOD")
    rows[:] = [raw("LEAD", at=AT)]
    s.fetch(operational(s, sends, rows=lambda: rows), [requested("LEAD", "stock")])
    assert len(sends) == 2 and read(s, "LEAD") and read(s, "GOOD")
    assert not [e for e in events(s) if e["action"] in ("STOP", "RESUME")]


def test_unwritable_withdrawal_is_held_in_process_and_written_before_the_next_check(tmp_path):
    s = ShortTimeoutStore(tmp_path / "q.sqlite")
    sends, rows = [], [raw("LEAD", at=AT)]
    s.fetch(operational(s, sends, rows=lambda: rows), [requested("LEAD", "stock")])
    before = read(s, "LEAD")[1]
    c = operational(s, sends, rows=lambda: rows)
    release = contend_reservation(s, c, through_withdrawal=True)
    with pytest.raises(QuoteUnavailable, match="^LOCAL_QUOTE_STORE_BUSY$"):
        s.fetch(c, [requested("LEAD", "stock")])
    release()
    with pytest.raises(QuoteUnavailable, match="TRADIER_LATEST_CHECK_UNAVAILABLE|LOCAL_QUOTE_EVIDENCE_WITHDRAWN"):
        read(s, "LEAD")                                           # the started check stays open
    with pytest.raises(QuoteUnavailable, match="TRADIER_LATEST_CHECK_UNAVAILABLE"):
        read(ShortTimeoutStore(s.path), "LEAD")                   # after a reopen too
    s.fetch(operational(s, sends, rows=lambda: rows), [requested("LEAD", "stock")])
    after = read(s, "LEAD")[1]
    assert after != before                                        # the withdrawal reached the store
    reasons = [json.loads(e["payload"])["reason"] for e in events(s)
               if e["action"] == "FAILED" and e["key"].startswith("production|LEAD|")]
    assert reasons and set(reasons) == {"LOCAL_QUOTE_STORE_BUSY"}


def test_a_lock_before_the_check_starts_dispatches_nothing_and_withdraws_in_process(tmp_path):
    s = ShortTimeoutStore(tmp_path / "q.sqlite")
    sends = []
    s.fetch(operational(s, sends, rows=lambda: [raw("LEAD", at=AT)]), [requested("LEAD", "stock")])
    s.fetch(operational(s, sends, rows=lambda: [raw("GOOD", at=AT)]), [requested("GOOD", "stock")])
    with blocked(s.path):
        with pytest.raises(QuoteUnavailable, match="^LOCAL_QUOTE_STORE_BUSY$"):
            s.fetch(operational(s, sends), [requested("LEAD", "stock")])
    assert len(sends) == 2
    with pytest.raises(QuoteUnavailable, match="LOCAL_QUOTE_EVIDENCE_WITHDRAWN"):
        read(s, "LEAD")                                           # attempted: no cached fallback
    assert read(s, "GOOD")
    s.fetch(operational(s, sends, rows=lambda: [raw("LEAD", at=AT)]), [requested("LEAD", "stock")])
    assert read(s, "LEAD")


def test_a_lock_while_saving_a_received_reply_is_local_and_keeps_the_name_unavailable(tmp_path):
    s = ShortTimeoutStore(tmp_path / "q.sqlite")
    sends = []
    s.fetch(operational(s, sends), [requested("GOOD", "stock")])
    state = {}

    def reply(method, url, headers, data):
        sends.append(url)
        state["lock"] = blocked(s.path)
        state["lock"].__enter__()                                 # the store locks during the request
        return {"quotes": {"quote": [raw("LEAD", at=AT)]}}
    c = TradierClient("fixture-only", operational=True, reserve_request=s.reserve_request,
                      request=reply, clock=lambda: AT)
    with pytest.raises(QuoteUnavailable, match="^LOCAL_QUOTE_STORE_BUSY$"):
        s.fetch(c, [requested("LEAD", "stock")])
    state.pop("lock").__exit__(None, None, None)
    reopened = ShortTimeoutStore(s.path)
    with pytest.raises(QuoteUnavailable, match="TRADIER_LATEST_CHECK_UNAVAILABLE"):
        read(reopened, "LEAD")
    assert read(reopened, "GOOD") and not [e for e in events(reopened) if e["action"] == "STOP"]


def test_old_ticket_is_invalidated_after_local_failure_and_fresh_recovery(tmp_path):
    d = Desk(tmp_path)
    tid, ver = d.prepare()
    short = ShortTimeoutStore(d.store.path)
    sends = []
    c = operational(short, sends, rows=lambda: d.rows)
    contend_reservation(short, c)
    with pytest.raises(QuoteUnavailable, match="^LOCAL_QUOTE_STORE_BUSY$"):
        short.fetch(c, [requested("LEAD", "stock")])
    assert not sends
    assert any("LOCAL_QUOTE_STORE" in p or "TRADIER_LATEST_CHECK_UNAVAILABLE" in p
               for p in recheck_problems(d, refresh=False))
    assert not d.check().problems                                 # fresh normal check, no resume
    with pytest.raises(TicketError, match="changed|differ"):
        approve(d.tickets, tid, ver, d.inputs, now=d.fixture.at)


def recheck_problems(d, refresh):
    from desk.tickets import recheck
    from dataclasses import replace
    inputs = d.inputs if refresh else replace(d.inputs, refresh_quotes=lambda request, at: None)
    return recheck("x", 1, d.request, inputs, d.fixture.at).problems


def test_ticket_refusals_name_the_local_cause_at_refresh_and_at_the_final_fence(tmp_path):
    d = Desk(tmp_path)
    tid, ver = d.prepare()
    d.store.__class__ = ShortTimeoutStore
    with blocked(d.store.path):                                   # the approval's fresh check is local-blocked
        with pytest.raises(TicketError, match=r"quote refresh unavailable \(LOCAL_QUOTE_STORE_BUSY\)"):
            approve(d.tickets, tid, ver, d.inputs, now=d.fixture.at)
    with pytest.raises(TicketError, match="changed"):            # the attempted name was withdrawn
        approve(d.tickets, tid, ver, d.inputs, now=d.fixture.at)
    tid, ver = d.prepare()                                        # a fresh normal check, no resume
    signals = d.fixture.desk.log.signals
    fence = signals.held_event

    @contextmanager
    def locked_fence(event_id):                                   # contention appears at the final fence
        with blocked(d.store.path), fence(event_id) as status:
            yield status
    signals.held_event = locked_fence
    with pytest.raises(TicketError, match=r"Final check unavailable \(LOCAL_QUOTE_STORE_BUSY\)"):
        approve(d.tickets, tid, ver, d.inputs, now=d.fixture.at)
    del signals.held_event
    assert approve(d.tickets, tid, ver, d.inputs, now=d.fixture.at)


# ---- controls: local limits and real provider refusals keep their policies -------------------
def test_control_local_rate_exhaustion_keeps_its_code_and_no_stop(tmp_path):
    s = QuoteStore(tmp_path / "q.sqlite")
    sends = []
    for i in range(120):
        s.reserve_request("production", AT)
    with pytest.raises(QuoteUnavailable, match="LOCAL_MARKET_DATA_RATE_LIMIT"):
        s.fetch(operational(s, sends), [requested("GOOD", "stock")])
    assert not sends and not [e for e in events(s) if e["action"] == "STOP"]


@pytest.mark.parametrize("error", ["REST_HTTP_401", "REST_HTTP_403", "REST_HTTP_429", "REST_TRANSPORT_FAILURE"])
def test_control_real_provider_refusals_still_stop_until_resume(tmp_path, error):
    s = QuoteStore(tmp_path / "q.sqlite")
    sends = []
    s.fetch(operational(s, sends), [requested("GOOD", "stock")])
    with pytest.raises(QuoteUnavailable, match=error):
        s.fetch(operational(s, sends, error=error), [requested("LEAD", "stock")])
    reopened = QuoteStore(s.path)
    with pytest.raises(QuoteUnavailable, match="EXPLICIT_RESUME_REQUIRED"):
        read(reopened, "GOOD")
    blocked_client = operational(reopened, sends)
    with pytest.raises(QuoteUnavailable, match="EXPLICIT_RESUME_REQUIRED"):
        reopened.fetch(blocked_client, [requested("GOOD", "stock")])
    assert len(sends) == 2
    reopened.resume("production")
    reopened.fetch(operational(reopened, sends), [requested("GOOD", "stock")])
    assert read(reopened, "GOOD")


def test_control_a_provider_stop_that_cannot_be_written_is_held_and_persisted(tmp_path):
    s = ShortTimeoutStore(tmp_path / "q.sqlite")
    sends, state = [], {}

    def reply(method, url, headers, data):
        sends.append(url)
        state["lock"] = blocked(s.path)
        state["lock"].__enter__()
        raise QuoteUnavailable("REST_HTTP_401")
    c = TradierClient("fixture-only", operational=True, reserve_request=s.reserve_request,
                      request=reply, clock=lambda: AT)
    with pytest.raises(QuoteUnavailable, match="^REST_HTTP_401$"):
        s.fetch(c, [requested("LEAD", "stock")])
    state.pop("lock").__exit__(None, None, None)
    with pytest.raises(QuoteUnavailable, match="EXPLICIT_RESUME_REQUIRED"):
        s.fetch(operational(s, sends), [requested("GOOD", "stock")])
    assert len(sends) == 1                                        # never retried
    with pytest.raises(QuoteUnavailable, match="EXPLICIT_RESUME_REQUIRED"):
        read(ShortTimeoutStore(s.path), "GOOD")                   # the STOP reached the store
    s.resume("production")
    s.fetch(operational(s, sends), [requested("GOOD", "stock")])
    assert read(s, "GOOD")


def test_refresh_command_names_a_local_cause(tmp_path, monkeypatch, capsys):
    import io
    path = tmp_path / "q.sqlite"
    QuoteStore(path)
    monkeypatch.setenv("TRADIER_ACCESS_TOKEN", "fixture-only")
    monkeypatch.setattr("desk.tradier_quotes.QuoteStore", ShortTimeoutStore)
    def no_provider(self, route, query):
        raise AssertionError("no provider call in this test")
    monkeypatch.setattr(TradierClient, "get", no_provider)
    with blocked(path):
        code = main(["--store", str(path), "--environment", "production", "refresh", "--symbols", "LEAD",
                     "--output", str(tmp_path / "out.json")], stdin=io.StringIO(), stdout=__import__("sys").stdout)
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["reason"] == "LOCAL_QUOTE_STORE_BUSY" and out["cause"] == "local"
    assert not (tmp_path / "out.json").exists()
