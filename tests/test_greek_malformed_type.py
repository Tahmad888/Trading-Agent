"""Malformed Greek starts must not publish tails; synthetic, zero network."""
from datetime import timedelta

import pytest

from desk import quote_measure as qm
from desk.tastytrade_quotes import QuoteUnavailable
from tests.test_greek_dropped_start import recorder, send
from tests.test_tastytrade_greeks import (AT, CALL, PUT, TX, BEGIN, END, SNIP,
    setup, feed, get, row, control, refused)
from tests.test_tastytrade_quotes import AGE


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("flags", [TX, BEGIN, 0, None, True, 128])
@pytest.mark.parametrize("path", ["recorder", "direct"])
def test_malformed_type_never_publishes_tail(existing, flags, path):
    if path == "recorder":
        _, rec = recorder()
        state = rec.greek_state
        deliver = lambda data: send(rec, data)
    else:
        _, state = setup()
        deliver = lambda data: feed(state, data)
    if existing:
        deliver(row())
    deliver(row(eventType="Quote", eventFlags=flags, index=2**63, delta="0.99"))
    assert CALL in state._states
    assert not state._states[CALL].rows and not state._states[CALL].pending
    assert state._states[CALL].highest_index is None or existing
    # The purported flags cannot tell us whether a BEGIN was missed.
    for data in (row(eventFlags=TX), row(delta="0.63"), control(END), row()):
        deliver(data)
        with pytest.raises(QuoteUnavailable):
            get(state)
    deliver(row(eventFlags=BEGIN | END | TX, delta="0.4"))
    with pytest.raises(QuoteUnavailable):
        get(state)
    deliver(control(0))
    assert get(state)["fields"]["delta"]["value"] == "0.4"


def test_known_malformed_type_isolates_peer_and_preserves_floor():
    _, state = setup()
    feed(state, row(AT - timedelta(seconds=30)))
    head = state._states[CALL].highest_index
    feed(state, row(eventSymbol=".SPY261009P770"), symbol=PUT)
    feed(state, row(eventType="Quote", eventFlags=TX, index=2**63))
    assert state._states[CALL].highest_index == head
    assert get(state, symbol=PUT)["status"] == "CURRENT_CALCULATION"
    feed(state, row(delta="0.3", eventSymbol=".SPY261009P770"), symbol=PUT)
    assert get(state, symbol=PUT)["fields"]["delta"]["value"] == "0.3"


def test_unmapped_malformed_type_marks_registered_options_only():
    _, rec = recorder()
    send(rec, row())
    send(rec, row(eventType="Quote", eventSymbol=".UNMAPPED", eventFlags=TX))
    send(rec, row())
    assert ".UNMAPPED" not in rec.greek_state._states
    assert set(rec.greek_state._states) == {CALL}
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "UNAVAILABLE"


def test_malformed_type_survives_maps_and_snip_but_reconnect_recovers():
    source, rec = recorder()
    send(rec, row(eventType="Quote", eventFlags=TX))
    full = dict(eventFields={"Greeks": list(qm.MEASURE_FIELDS["Greeks"])})
    rec.configure(dict(eventFields={"Greeks": ["eventType"]}), AT)
    rec.configure(full, AT)
    rec.configure(full, AT)
    send(rec, row())
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "UNAVAILABLE"
    send(rec, row(eventFlags=BEGIN | SNIP))
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "UNAVAILABLE"
    source.disconnect("DISCONNECTED")
    source.begin(AT + AGE)
    source.ready(AT)
    rec.begin(source.generation, AT)
    rec.configure(full, AT)
    send(rec, row())
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "CURRENT_CALCULATION"


def test_empty_full_snapshot_recovers_to_empty_not_previous_calculation():
    _, rec = recorder()
    send(rec, row())
    send(rec, row(eventType="Quote", eventFlags=TX))
    send(rec, control(BEGIN | END))
    refused(rec.greek_state, "GREEKS_EMPTY")
    send(rec, row())
    assert get(rec.greek_state)["status"] == "CURRENT_CALCULATION"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("malformed", ["non-dict", "misbound"])
def test_direct_malformed_row_or_identity_cannot_hide_start(existing, malformed):
    _, state = setup()
    if existing:
        feed(state)
    data = [] if malformed == "non-dict" else row(eventSymbol=".UNMAPPED", eventFlags=TX)
    assert not feed(state, data)
    assert not feed(state, row(eventFlags=TX))
    assert not feed(state, row())
    with pytest.raises(QuoteUnavailable):
        get(state)
    feed(state, row(eventFlags=BEGIN | END))
    assert get(state)["status"] == "CURRENT_CALCULATION"


def test_wrong_type_with_stock_wire_cannot_masquerade_as_valid_greek_identity():
    _, rec = recorder()
    send(rec, row())
    send(rec, row(eventType="Quote", eventSymbol="SPY", eventFlags=TX))
    send(rec, row())
    assert set(rec.greek_state._states) == {CALL}
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("flags", [None, True, -1, 128, "4", 1.0])
def test_mapped_invalid_flags_cannot_hide_transaction_start(existing, flags):
    _, state = setup()
    if existing:
        feed(state)
    assert not feed(state, row(eventFlags=flags))
    for data in (row(eventFlags=TX), row(), control(END), row()):
        assert not feed(state, data)
        refused(state, "FLAGS_INVALID")
    feed(state, row(eventFlags=BEGIN | END))
    assert get(state)["status"] == "CURRENT_CALCULATION"


def test_rejected_first_tx_with_invalid_receipt_never_loses_snapshot_requirement():
    _, state = setup()
    assert not feed(state, row(eventFlags=TX), at=AT.replace(tzinfo=None))
    assert CALL in state._states
    for data in (row(eventFlags=TX), row()):
        assert not feed(state, data)
        with pytest.raises(QuoteUnavailable):
            get(state)
    feed(state, row(eventFlags=BEGIN | END))
    assert get(state)["status"] == "CURRENT_CALCULATION"
