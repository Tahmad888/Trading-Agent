"""First-record loss during identity/schema withdrawal; synthetic, no network."""
from datetime import timedelta

import pytest

from desk import quote_measure as qm
from desk.tastytrade_quotes import instrument
from tests.test_tastytrade_quotes import option, AGE
from tests.test_quote_measure import open_measure, with_option
from tests.test_tastytrade_greeks import (AT, CALL, PUT, TX, BEGIN, END, SNIP,
    setup, feed, get, row, refused, control)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("first", [TX, BEGIN, BEGIN | TX])
@pytest.mark.parametrize("failure", ["identity", "schema"])
def test_decoded_first_record_dropped_cannot_publish_tail(existing, first, failure):
    source, state = setup()
    if existing:
        feed(state)
    if failure == "identity":
        source.invalidate(CALL)
    else:
        state.down("GREEK_SCHEMA_MISSING_INDEX_FIELDS")
    assert not feed(state, row(eventFlags=first, delta="0.99"))
    assert CALL in state._states
    assert not state._states[CALL].rows and not state._states[CALL].pending
    if failure == "identity":
        source.register(instrument(CALL, option(), AT))
    else:
        state.schema(changed=True)
    assert not feed(state, row(AT - timedelta(seconds=100), eventFlags=TX))
    assert not feed(state, row(AT - timedelta(seconds=90), eventFlags=END, delta="0.63"))
    refused(state, "RESET_BOUNDARY_DISCARDED")
    assert feed(state, row(AT - timedelta(seconds=80), delta="0.4"))
    assert get(state)["fields"]["delta"]["value"] == "0.4"


@pytest.mark.parametrize("defer", [0, TX])
def test_first_dropped_begin_waits_for_end_even_when_snapshot_body_has_no_tx(defer):
    source, state = setup()
    source.invalidate(CALL)
    assert not feed(state, row(eventFlags=BEGIN))
    source.register(instrument(CALL, option(), AT))
    assert not feed(state, row(delta="0.9"))
    refused(state, "RESET_TRANSACTION_INCOMPLETE")
    state.schema(changed=True)
    assert not feed(state, control(END | defer))
    if defer:
        assert not feed(state, row())
    refused(state, "RESET_BOUNDARY_DISCARDED")
    assert feed(state)
    assert get(state)["status"] == "CURRENT_CALCULATION"


@pytest.mark.parametrize("first", [TX, BEGIN])
def test_decoded_end_dropped_in_same_window_allows_next_complete_update(first):
    source, state = setup()
    source.invalidate(CALL)
    assert not feed(state, row(eventFlags=first))
    assert not feed(state, control(END))
    source.register(instrument(CALL, option(), AT))
    assert feed(state)
    assert get(state)["status"] == "CURRENT_CALCULATION"


def test_dropped_start_isolated_from_healthy_peer_and_newest_floor_retained():
    source, state = setup()
    feed(state, row(eventSymbol=".SPY261009P770"), symbol=PUT)
    feed(state, row(AT - timedelta(seconds=30)))
    source.invalidate(CALL)
    assert not feed(state, row(eventFlags=TX))
    source.register(instrument(CALL, option(), AT))
    assert get(state, symbol=PUT)["status"] == "CURRENT_CALCULATION"
    assert not feed(state, row())
    feed(state, row(AT - timedelta(seconds=100)))
    refused(state, "NEWER_CALCULATION_REQUIRED")


def recorder():
    source = with_option()
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE)
    open_measure(source, rec)
    return source, rec


def send(rec, data, names=None):
    names = names or qm.MEASURE_FIELDS["Greeks"]
    rec.data(dict(data=["Greeks", [data.get(n) for n in names]]), AT)


@pytest.mark.parametrize("first", [TX, BEGIN])
def test_recorder_limited_map_retains_decoded_start(first):
    _, rec = recorder()
    limited = ["eventType", "eventSymbol", "time", "eventFlags", "delta"]
    rec.configure(dict(eventFields={"Greeks": limited}), AT)
    send(rec, row(eventFlags=first), limited)
    rec.configure(dict(eventFields={"Greeks": list(qm.MEASURE_FIELDS["Greeks"])}), AT)
    send(rec, row(eventFlags=TX))
    send(rec, row(eventFlags=END))
    assert rec.greek_report(AT)["checks"][CALL]["reason"] == "GREEK_RESET_BOUNDARY_DISCARDED"
    send(rec, row(delta="0.4"))
    assert rec.greek_report(AT)["checks"][CALL]["fields"]["delta"]["value"] == "0.4"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("first", [TX, BEGIN, 0])
def test_undecodable_loss_requires_full_snapshot_or_generation(existing, first):
    _, rec = recorder()
    if existing:
        send(rec, row())
    rec.configure(dict(eventFields={"Greeks": ["eventType"]}), AT)
    # The old sender's row cannot be decoded by the withdrawn map.
    send(rec, row(eventFlags=first))
    assert not rec.greek_state._states[CALL].rows
    assert not rec.greek_state._states[CALL].pending
    rec.configure(dict(eventFields={"Greeks": list(qm.MEASURE_FIELDS["Greeks"])}), AT)
    for data in (row(eventFlags=TX), row(), control(END), row()):
        send(rec, data)
        assert rec.greek_report(AT)["checks"][CALL]["status"] == "UNAVAILABLE"
    send(rec, row(eventFlags=BEGIN | END | TX, delta="0.4"))
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "UNAVAILABLE"
    send(rec, control(0))
    assert rec.greek_report(AT)["checks"][CALL]["fields"]["delta"]["value"] == "0.4"


def test_missing_flags_in_decoded_withdrawal_cannot_guess_boundary():
    _, rec = recorder()
    names = ["eventType", "eventSymbol", "time", "delta"]
    rec.configure(dict(eventFields={"Greeks": names}), AT)
    send(rec, row(eventFlags=BEGIN), names)
    rec.configure(dict(eventFields={"Greeks": list(qm.MEASURE_FIELDS["Greeks"])}), AT)
    send(rec, row())
    assert rec.greek_report(AT)["checks"][CALL]["reason"] == "GREEK_LOST_BOUNDARY_UNKNOWN"
    send(rec, row(eventFlags=BEGIN | END))
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "CURRENT_CALCULATION"


def test_map_withdrawal_without_data_does_not_invent_lost_snapshot():
    _, rec = recorder()
    rec.configure(dict(eventFields={"Greeks": ["eventType"]}), AT)
    rec.data(dict(data=["Greeks", []]), AT)
    rec.configure(dict(eventFields={"Greeks": list(qm.MEASURE_FIELDS["Greeks"])}), AT)
    send(rec, row())
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "CURRENT_CALCULATION"


def test_unknown_gap_survives_repeated_maps_but_new_generation_starts_clean():
    source, rec = recorder()
    rec.configure(dict(eventFields={"Greeks": ["eventType"]}), AT)
    send(rec, row(eventFlags=BEGIN))
    full = dict(eventFields={"Greeks": list(qm.MEASURE_FIELDS["Greeks"])})
    rec.configure(full, AT)
    rec.configure(full, AT)
    rec.greek_state.down("DISCONNECTED")
    rec.configure(full, AT)
    send(rec, row())
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "UNAVAILABLE"
    source.disconnect("DISCONNECTED")
    source.begin(AT + AGE)
    source.ready(AT)
    rec.begin(source.generation, AT)
    rec.configure(full, AT)
    send(rec, row())
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "CURRENT_CALCULATION"


def test_unknown_gap_and_lost_snip_cannot_recover_with_truncated_snapshot():
    source, state = setup()
    source.invalidate(CALL)
    feed(state, row(eventFlags=BEGIN))
    feed(state, control(SNIP))
    source.register(instrument(CALL, option(), AT))
    assert not feed(state, row())
    refused(state, "SNAPSHOT_TRUNCATED")
    feed(state, row(eventFlags=BEGIN | SNIP))
    refused(state, "SNAPSHOT_TRUNCATED")
    feed(state, row(eventFlags=BEGIN | END))
    assert get(state)["status"] == "CURRENT_CALCULATION"


@pytest.mark.parametrize("flags", [None, True, -1, 32])
def test_invalid_dropped_flags_require_snapshot_without_poisoning_index(flags):
    source, state = setup()
    source.invalidate(CALL)
    assert not feed(state, row(eventFlags=flags, index=2**63))
    source.register(instrument(CALL, option(), AT))
    assert not feed(state)
    refused(state, "LOST_BOUNDARY_UNKNOWN")
    feed(state, row(eventFlags=BEGIN | END))
    assert get(state)["status"] == "CURRENT_CALCULATION"


def test_unknown_symbol_cannot_allocate_dropped_recovery_state():
    source, state = setup()
    source.invalidate(CALL)
    assert not feed(state, row(eventFlags=TX), symbol="UNKNOWN")
    assert "UNKNOWN" not in state._states


@pytest.mark.parametrize("payload", [None, 0, {"data": "unusable"}, ["truncated"]])
def test_bad_payload_shape_cannot_hide_start(payload):
    _, rec = recorder()
    if isinstance(payload, list):
        with pytest.raises(qm.QuoteUnavailable, match="MEASURE_DATA_INVALID"):
            rec.data(dict(data=["Greeks", payload]), AT)
    else:
        rec.data(dict(data=["Greeks", payload]), AT)
    rec.configure(dict(eventFields={"Greeks": list(qm.MEASURE_FIELDS["Greeks"])}), AT)
    send(rec, row())
    assert rec.greek_report(AT)["checks"][CALL]["reason"] == "GREEK_LOST_BOUNDARY_UNKNOWN"


def test_undecodable_loss_affects_registered_options_but_not_stock_quote_trade():
    from tests.test_tastytrade_quotes import quote_row, trade_row
    source, state = setup()
    from tests.test_tastytrade_quotes import stock
    source.register(instrument("SPY", stock(), AT))
    assert source.feed("Quote", quote_row(), AT)
    assert source.feed("Trade", trade_row(), AT)
    before_quote, before_trade = source.quote("SPY", AT, AGE), source.trade("SPY", AT, AGE)
    feed(state)
    feed(state, row(eventSymbol=".SPY261009P770"), symbol=PUT)
    state.unknown_gap()
    assert set(state._states) == {CALL, PUT}
    for symbol, wire in ((CALL, ".SPY261009C770"), (PUT, ".SPY261009P770")):
        assert not feed(state, row(eventSymbol=wire), symbol=symbol)
        refused(state, "LOST_BOUNDARY_UNKNOWN", symbol=symbol)
    assert source.quote("SPY", AT, AGE) == before_quote
    assert source.trade("SPY", AT, AGE) == before_trade


def test_old_generation_loss_cannot_poison_new_generation_metadata():
    source, state = setup()
    source.disconnect("DISCONNECTED")
    source.begin(AT + AGE)
    source.ready(AT)
    assert not feed(state, row(eventFlags=BEGIN))
    state.unknown_gap()
    assert not state._states
    state.begin(source.generation)
    state.schema()
    assert feed(state)
    assert get(state)["status"] == "CURRENT_CALCULATION"
