"""Regression evidence for Claude F1/F2; synthetic calculation events, no network."""
from datetime import timedelta

import pytest

from desk import quote_measure as qm
from desk.tastytrade_quotes import instrument, QuoteUnavailable
from tests.test_tastytrade_quotes import option, AGE
from tests.test_quote_measure import open_measure, feed_measure, with_option
from tests.test_tastytrade_greeks import (AT, CALL, PUT, TX, BEGIN, END, SNIP, REMOVE,
    setup, feed, get, row, index, refused, control)


@pytest.mark.parametrize("recovery_age", [30, 10])
def test_f1_rejected_update_cannot_promote_older_history(recovery_age):
    _, state = setup()
    feed(state, row(AT - timedelta(seconds=30)))
    assert not feed(state, row(AT - timedelta(seconds=20), sequence=1))
    assert feed(state, row(AT - timedelta(seconds=300), delta="0.1"))
    refused(state, "NEWER_CALCULATION_REQUIRED")
    feed(state, row(AT - timedelta(seconds=recovery_age), delta="0.7"))
    assert get(state)["fields"]["delta"]["value"] == "0.7"
    assert get(state)["age_seconds"] == str(recovery_age)


def test_f1_repeated_reject_keeps_newest_validated_floor():
    _, state = setup()
    feed(state, row(AT - timedelta(seconds=30)))
    feed(state, row(index=1))
    feed(state, row(AT - timedelta(seconds=300)))
    feed(state, row(sequence=1))
    feed(state, row(AT - timedelta(seconds=200)))
    refused(state, "NEWER_CALCULATION_REQUIRED")


@pytest.mark.parametrize("empty", [False, True])
def test_f1_completed_snapshot_can_replace_floor_with_older_or_empty_evidence(empty):
    _, state = setup()
    feed(state, row(AT - timedelta(seconds=30)))
    feed(state, row(sequence=1))
    data = control(BEGIN | END) if empty else row(eventFlags=BEGIN | END, delta="0.2")
    feed(state, data)
    if empty:
        refused(state, "GREEKS_EMPTY")
        feed(state, row())
    assert get(state)["age_seconds"] == "120"


def test_f1_invalid_large_index_cannot_poison_recovery_floor():
    _, state = setup()
    feed(state)
    assert not feed(state, row(index=index(AT + timedelta(days=100))))
    feed(state, row())
    assert get(state)["status"] == "CURRENT_CALCULATION"


def test_f1_calculation_added_then_removed_does_not_revive_after_reset():
    _, state = setup()
    feed(state)
    recent = AT - timedelta(seconds=20)
    feed(state, row(recent, eventFlags=TX))
    feed(state, row(recent, eventFlags=REMOVE))
    refused(state, "LATEST_GREEK_CALCULATION_REMOVED")
    state.schema(changed=True)
    feed(state)
    refused(state, "NEWER_CALCULATION_REQUIRED")
    feed(state, row(recent))
    assert get(state)["age_seconds"] == "20"


def test_f1_latest_removal_inside_snapshot_keeps_floor_after_reset():
    _, state = setup()
    recent = AT - timedelta(seconds=20)
    feed(state, row(recent, eventFlags=BEGIN))
    feed(state)
    feed(state, row(recent, eventFlags=REMOVE | END))
    refused(state, "LATEST_GREEK_CALCULATION_REMOVED")
    state.schema(changed=True)
    feed(state)
    refused(state, "NEWER_CALCULATION_REQUIRED")


def test_changed_instrument_does_not_inherit_old_calculation_floor():
    source, state = setup()
    feed(state, row(AT - timedelta(seconds=20)))
    source.register(instrument(CALL, option(**{"streamer-symbol": ".NEW261009C770"}), AT))
    feed(state, row(eventSymbol=".NEW261009C770"))
    assert get(state)["age_seconds"] == "120"


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("reset", ["identity", "schema", "withdrawn"])
def test_f2_reset_discards_interrupted_transaction_through_closing_boundary(reset, observed):
    source, state = setup()
    feed(state, row(eventSymbol=".SPY261009P770"), symbol=PUT)
    feed(state, row(eventFlags=TX))
    if reset == "identity":
        source.invalidate(CALL)
        if observed:
            refused(state, "IDENTITY_REFRESH_FAILED")
        assert not feed(state, row(AT - timedelta(seconds=100), eventFlags=TX))
        source.register(instrument(CALL, option(), AT))
        assert get(state, symbol=PUT)["status"] == "CURRENT_CALCULATION"
    elif reset == "schema":
        state.schema(changed=True)
        assert not feed(state, row(AT - timedelta(seconds=100), eventFlags=TX))
    else:
        state.down("GREEK_SCHEMA_INVALID")
        assert not feed(state, row(AT - timedelta(seconds=100), eventFlags=TX))
        state.schema(changed=True)
    assert not feed(state, row(AT - timedelta(seconds=90), delta="0.9"))
    refused(state, "RESET_BOUNDARY_DISCARDED")
    feed(state, row(AT - timedelta(seconds=80), delta="0.4"))
    assert get(state)["fields"]["delta"]["value"] == "0.4"


def test_f2_unobserved_epoch_change_cannot_erase_pending_update():
    source, state = setup()
    feed(state, row(eventFlags=TX))
    source.invalidate(CALL)
    source.register(instrument(CALL, option(), AT))
    assert not feed(state, row(AT - timedelta(seconds=90)))
    refused(state, "RESET_BOUNDARY_DISCARDED")
    feed(state, row(AT - timedelta(seconds=80)))
    assert get(state)["age_seconds"] == "80"


@pytest.mark.parametrize("first", [TX, BEGIN])
@pytest.mark.parametrize("boundary", ["calculation", "removal"])
def test_f2_repeated_schema_resets_preserve_lost_transaction_or_snapshot(first, boundary):
    _, state = setup()
    feed(state, row(eventFlags=first))
    state.down("GREEK_SCHEMA_INVALID")
    state.schema(changed=True)
    state.schema(changed=True)
    assert not feed(state, row(eventFlags=TX))
    closing = control(END) if boundary == "removal" else row(eventFlags=END)
    assert not feed(state, closing)
    refused(state, "RESET_BOUNDARY_DISCARDED")
    feed(state, row(delta="0.3"))
    assert get(state)["fields"]["delta"]["value"] == "0.3"


def test_f2_new_begin_recovers_without_publishing_old_tail():
    _, state = setup()
    feed(state, row(eventFlags=TX))
    state.schema(changed=True)
    feed(state, row(eventFlags=BEGIN | END | TX, delta="0.4"))
    refused(state, "TRANSACTION_INCOMPLETE")
    feed(state, control(0))
    assert get(state)["fields"]["delta"]["value"] == "0.4"


@pytest.mark.parametrize("ending", [END, SNIP])
@pytest.mark.parametrize("defer", [0, TX])
def test_f2_lost_snapshot_discards_plain_records_until_end_and_tx_clear(ending, defer):
    _, state = setup()
    feed(state, row(eventFlags=BEGIN))
    state.schema(changed=True)
    assert not feed(state, row(AT - timedelta(seconds=90), delta="0.9"))
    refused(state, "RESET_TRANSACTION_INCOMPLETE")
    state.schema(changed=True)  # a second reset must not lose the snapshot marker
    assert not feed(state, control(ending | defer))
    if ending == SNIP:
        refused(state, "SNAPSHOT_TRUNCATED")
        assert not feed(state, row(AT - timedelta(seconds=80)))
        refused(state, "SNAPSHOT_TRUNCATED")
        feed(state, row(eventFlags=BEGIN | END))
        assert get(state)["status"] == "CURRENT_CALCULATION"
        return
    if defer:
        refused(state, "RESET_TRANSACTION_INCOMPLETE")
        assert not feed(state, row(AT - timedelta(seconds=80)))
    refused(state, "RESET_BOUNDARY_DISCARDED")
    assert feed(state, row(AT - timedelta(seconds=70), delta="0.3"))
    assert get(state)["fields"]["delta"]["value"] == "0.3"


def test_f2_new_generation_keeps_supported_first_flag_zero_delivery():
    source, state = setup()
    feed(state, row(eventFlags=TX))
    state.down("DISCONNECTED")
    source.disconnect("DISCONNECTED")
    source.begin(AT + AGE)
    source.ready(AT)
    state.begin(source.generation)
    state.schema(changed=True)
    assert feed(state)
    assert get(state)["status"] == "CURRENT_CALCULATION"


@pytest.mark.parametrize("withdraw", [False, True])
def test_f2_recorder_changed_or_reaccepted_map_discards_tail(withdraw):
    source = with_option()
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE)
    session = open_measure(source, rec)
    feed_measure(session, "Greeks", [row(eventFlags=TX)], AT)
    if withdraw:
        rec.configure(dict(eventFields={"Greeks": ["eventType", "eventSymbol", "time"]}), AT)
    names = list(reversed(qm.MEASURE_FIELDS["Greeks"]))
    rec.configure(dict(eventFields={"Greeks": names}), AT)
    rec.data(dict(data=["Greeks", [row()[n] for n in names]]), AT)
    assert rec.greek_report(AT)["checks"][CALL]["reason"] == "GREEK_RESET_BOUNDARY_DISCARDED"
    update = row(AT - timedelta(seconds=100))
    rec.data(dict(data=["Greeks", [update[n] for n in names]]), AT)
    assert rec.greek_report(AT)["checks"][CALL]["age_seconds"] == "100"


def test_f1_schema_reset_keeps_floor_but_clear_atomic_reset_accepts_newer_update():
    _, state = setup()
    feed(state, row(AT - timedelta(seconds=30)))
    state.schema(changed=True)
    feed(state, row())
    refused(state, "NEWER_CALCULATION_REQUIRED")
    feed(state, row(AT - timedelta(seconds=20)))
    assert get(state)["age_seconds"] == "20"


def test_f2_corrupt_transaction_keeps_snapshot_requirement_through_reset():
    _, state = setup()
    feed(state, row(eventFlags=TX))
    feed(state, row(index=1))
    state.schema(changed=True)
    assert not feed(state, row())
    with pytest.raises(QuoteUnavailable):
        get(state)
    feed(state, row(eventFlags=BEGIN | END))
    assert get(state)["status"] == "CURRENT_CALCULATION"


@pytest.mark.parametrize("reset", ["identity", "schema", "withdrawn"])
def test_same_generation_reset_cannot_waive_a_truncated_snapshot(reset):
    source, state = setup()
    feed(state, row(eventFlags=BEGIN | SNIP))
    refused(state, "SNAPSHOT_TRUNCATED")
    if reset == "identity":
        source.invalidate(CALL)
        source.register(instrument(CALL, option(), AT))
    elif reset == "schema":
        state.schema(changed=True)
    else:
        state.down("GREEK_SCHEMA_INVALID")
        state.schema(changed=True)
    assert not feed(state)
    with pytest.raises(QuoteUnavailable):
        get(state)
    feed(state, row(eventFlags=BEGIN | END))
    assert get(state)["status"] == "CURRENT_CALCULATION"
