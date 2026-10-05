"""Real local signal/account/ticket pipeline; all provider data is synthetic."""
from dataclasses import replace
from datetime import timedelta
from threading import Event, Thread

import pytest

from desk.quote_risk import TastytradeRiskSource
from desk.tastytrade_quotes import SOURCE, instrument
from desk.tickets import TicketError, TicketStore
from tests.test_tastytrade_quotes import AGE, service, stock, trade_row
from tests.test_tickets import _event_path
from tests.ticket_support import approve


def path(tmp_path):
    at, log, event, _, inputs, request = _event_path(tmp_path)
    quotes = service(("LEAD",), at=at)
    quotes.feed("Trade", trade_row("LEAD", at, price="101.5"), at)
    terms = TastytradeRiskSource(inputs.terms_source.source, log, quotes)
    inputs = replace(inputs, terms_source=terms)
    request = request.model_copy(update={"quote_source":SOURCE})
    return at, log, event, quotes, inputs, request


def test_quote_to_signal_to_ticket_single_use(tmp_path):
    at, log, event, quotes, inputs, request = path(tmp_path)
    store = TicketStore(tmp_path/"tickets.sqlite")
    tid, version = store.prepare(request, inputs, now=at)
    view = store.get(tid, version, now=at)
    assert view["binding"]["quote_provenance"]["instrument_id"] == "synthetic:LEAD"
    assert view["binding"]["legs"][0]["final_qty"] == 10
    approve(store, tid, version, inputs, now=at)
    result = store.consume(tid, version, request_id="fixture-request", inputs=inputs, now=at)
    assert result["order_submitted"] is False and result["broker_action"] == "none"
    assert view["request"]["quote_source"] == SOURCE
    assert store.consume(tid, version, request_id="fixture-request", inputs=inputs, now=at)["replay"] is True
    with pytest.raises(TicketError, match="already consumed"):
        store.consume(tid, version, request_id="different", inputs=inputs, now=at)


def test_caller_source_claim_does_not_become_provenance(tmp_path):
    at, _, _, _, inputs, request = path(tmp_path)
    store = TicketStore(tmp_path/"tickets.sqlite")
    tid, version = store.prepare(request.model_copy(update={"quote_source":"fake source"}), inputs, now=at)
    assert "quote_source_matches" in store.get(tid, version, now=at)["binding"]["failed_checks"]
    with pytest.raises(TicketError, match="blocking checks"):
        approve(store, tid, version, inputs, now=at)


def test_new_quote_timestamp_same_generation_does_not_churn_ticket(tmp_path):
    at, _, _, quotes, inputs, request = path(tmp_path)
    store = TicketStore(tmp_path/"tickets.sqlite")
    tid, version = store.prepare(request, inputs, now=at)
    later = at+timedelta(seconds=1)
    quotes.feed("Trade", trade_row("LEAD", later, price="101.5"), later)
    approve(store, tid, version, inputs, now=later)
    assert store.get(tid, version, now=later)["state"] == "approved"


@pytest.mark.parametrize("fault", ["disconnect", "identity", "reconnect", "expiry", "price"])
def test_changed_health_after_recheck_refuses_final_write(tmp_path, fault):
    at, _, _, quotes, inputs, request = path(tmp_path)
    store = TicketStore(tmp_path/"tickets.sqlite")
    tid, version = store.prepare(request, inputs, now=at)
    observe = inputs.observe
    def race(*args):
        observation = observe(*args)
        if fault == "disconnect":
            quotes.disconnect()
        elif fault == "identity":
            quotes.register(instrument("LEAD", stock("LEAD", cusip="changed-id"), at))
            quotes.feed("Trade", trade_row("LEAD", at, price="101.5"), at)
        elif fault == "reconnect":
            quotes.begin(at+AGE)
            quotes.ready(at)
            quotes.feed("Trade", trade_row("LEAD", at, price="101.5"), at)
        elif fault == "expiry":
            # Token already expired at the final clock; no quote can survive it.
            quotes.begin(at)
        else:
            quotes.feed("Trade", trade_row("LEAD", at, price="97"), at)
        return observation
    with pytest.raises(TicketError):
        approve(store, tid, version, replace(inputs, observe=race), now=at)
    assert store.get(tid, version, now=at)["state"] == "pending"


def test_disconnect_after_approval_refuses_consumption(tmp_path):
    at, _, _, quotes, inputs, request = path(tmp_path)
    store = TicketStore(tmp_path/"tickets.sqlite")
    tid, version = store.prepare(request, inputs, now=at)
    approve(store, tid, version, inputs, now=at)
    observe = inputs.observe
    def race(*args):
        result = observe(*args)
        quotes.disconnect()
        return result
    with pytest.raises(TicketError):
        store.consume(tid, version, request_id="fixture", inputs=replace(inputs, observe=race), now=at)
    assert store.get(tid, version, now=at)["state"] == "approved"


def test_normal_live_price_movement_is_rechecked_without_new_version(tmp_path):
    at, _, _, quotes, inputs, request = path(tmp_path)
    store = TicketStore(tmp_path/"tickets.sqlite")
    tid, version = store.prepare(request, inputs, now=at)
    observe = inputs.observe
    def update(*args):
        result = observe(*args)
        quotes.feed("Trade", trade_row("LEAD", at, price="101.6"), at)
        return result
    approve(store, tid, version, replace(inputs, observe=update), now=at)
    assert store.get(tid, version, now=at)["state"] == "approved"


def test_local_health_writer_waits_for_final_ticket_commit(tmp_path, monkeypatch):
    at, _, _, quotes, inputs, request = path(tmp_path)
    store = TicketStore(tmp_path/"tickets.sqlite")
    tid, version = store.prepare(request, inputs, now=at)
    entering, completed = Event(), Event()
    threads = []
    original = store._audit
    # If the exact audit hook changes, this test must follow the real final write.
    def write(*args, **kwargs):
        def disconnect():
            entering.set()
            quotes.disconnect()
            completed.set()
        thread = Thread(target=disconnect)
        threads.append(thread)
        thread.start()
        assert entering.wait(2)
        assert not completed.wait(.05)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "_audit", write)
    approve(store, tid, version, inputs, now=at)
    for thread in threads:
        thread.join(2)
    assert completed.is_set() and not quotes.connected
