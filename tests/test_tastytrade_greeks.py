"""Indexed Greek state and negotiated transport; synthetic data, zero provider calls."""
from datetime import timedelta
from decimal import Decimal
import json

import pytest

from desk import quote_measure as qm
from desk.tastytrade_greeks import GreekState, TX, REMOVE, BEGIN, END, SNIP, MODE, MAX_SEQUENCE
from desk.tastytrade_quotes import QuoteService, QuoteUnavailable, instrument
from desk.tastytrade_transport import MEASURE_CHANNEL, json_read
from tests.test_tastytrade_quotes import AT, AGE, ms, option
from tests.test_quote_measure import open_measure, feed_measure, with_option

CALL = "SPY261009C00770000"
PUT = "SPY261009P00770000"
WIRE = ".SPY261009C770"


def index(at, sequence=0):
    time = ms(at)
    return ((time // 1000) << 32) | ((time % 1000) << 22) | sequence


def row(at=None, **changes):
    at = at or AT - timedelta(seconds=120)
    data = dict(eventType="Greeks", eventSymbol=WIRE, eventFlags=0, index=index(at),
                time=ms(at), sequence=0, price="5.1", volatility="0.21", delta="0.52",
                gamma="0.04", theta="-0.31", rho="0.05", vega="0.44")
    data.update(changes)
    return data


def setup(*, max_entries=4096):
    service = QuoteService()
    for symbol, fields in ((CALL, option()), (PUT, option(symbol="SPY   261009P00770000",
                                   **{"streamer-symbol": ".SPY261009P770", "option-type": "P"}))):
        service.register(instrument(symbol, fields, AT - timedelta(seconds=1)))
    service.begin(AT + timedelta(hours=1))
    service.ready(AT)
    state = GreekState(service, max_entries=max_entries)
    state.begin(service.generation)
    state.schema()
    return service, state


def feed(state, data=None, at=AT, symbol=CALL):
    return state.feed(symbol, row() if data is None else data, at)


def get(state, at=AT, symbol=CALL):
    return state.current(symbol, at)


def refused(state, code, at=AT, symbol=CALL):
    with pytest.raises(QuoteUnavailable, match=code):
        get(state, at, symbol)


def control(flags, **changes):
    return row(eventFlags=flags | REMOVE, index=0, time=0, sequence=0,
               price=None, volatility=None, delta=None, gamma=None, theta=None, rho=None, vega=None, **changes)


def test_atomic_plain_event_age_and_documented_units():
    _, state = setup()
    assert feed(state)
    result = get(state)
    assert result["status"] == "CURRENT_CALCULATION"
    assert result["age_seconds"] == "120" and result["receipt_age_seconds"] == "120"
    assert get(state, AT + timedelta(seconds=7))["age_seconds"] == "127"
    assert result["source_at"] != result["received_at"]
    assert result["fields"]["vega"]["unit"] == "$ per share per IV point"
    assert result["fields"]["theta"]["value"] == "-0.31"
    assert result["fields"]["rho"]["status"] == "DOCUMENTED"
    assert result["freshness_policy"] == "NO_APPROVED_GREEK_AGE_LIMIT"
    assert result["stop_valuation"] == result["position_exposure"] == "NOT_COMPUTED"


def test_large_index_nonzero_milliseconds_sequence_and_wire_decimal_preserved():
    _, state = setup()
    at = AT - timedelta(milliseconds=120123)
    data = row(at, index=index(at, MAX_SEQUENCE), sequence=MAX_SEQUENCE,
               delta=Decimal("0.52123456789123456789"))
    assert data["index"] > 2**53
    wire = json.dumps(data, default=str)
    assert feed(state, json_read(wire))
    result = get(state)
    assert result["index"] == data["index"]
    assert result["fields"]["delta"]["value"] == "0.52123456789123456789"
    assert result["age_seconds"] == "120.123"


def test_older_arrival_does_not_replace_newest_calculation_and_same_index_corrects():
    _, state = setup()
    feed(state)
    feed(state, row(AT - timedelta(seconds=180), delta="0.1"), AT + timedelta(seconds=1))
    assert get(state, AT + timedelta(seconds=1))["fields"]["delta"]["value"] == "0.52"
    feed(state, row(delta="0.49"), AT + timedelta(seconds=2))
    result = get(state, AT + timedelta(seconds=3))
    assert result["fields"]["delta"]["value"] == "0.49" and result["age_seconds"] == "123"


def test_transaction_publishes_only_after_final_event():
    _, state = setup()
    feed(state)
    feed(state, row(AT - timedelta(seconds=110), eventFlags=TX, delta="0.6"))
    refused(state, "TRANSACTION_INCOMPLETE")
    feed(state, row(AT - timedelta(seconds=100), delta="0.7"))
    assert get(state)["fields"]["delta"]["value"] == "0.7"


def test_snapshot_end_pending_waits_for_transaction_clear():
    _, state = setup()
    feed(state, row(eventFlags=BEGIN | MODE))
    refused(state, "TRANSACTION_INCOMPLETE")
    feed(state, control(END | TX))
    refused(state, "TRANSACTION_INCOMPLETE")
    feed(state, row(AT - timedelta(seconds=100)))
    assert get(state)["age_seconds"] == "100"


def test_repeated_begin_discards_residual_newer_calculation():
    _, state = setup()
    feed(state, row(AT - timedelta(seconds=10), eventFlags=BEGIN | TX, delta="0.9"))
    feed(state, row(eventFlags=BEGIN, delta="0.4"))
    feed(state, control(END))
    assert get(state)["fields"]["delta"]["value"] == "0.4"


@pytest.mark.parametrize("flags", [BEGIN | END, BEGIN | END | TX])
def test_empty_snapshot_clears_old_values(flags):
    _, state = setup()
    feed(state)
    feed(state, control(flags))
    if flags & TX:
        refused(state, "TRANSACTION_INCOMPLETE")
        feed(state, control(0))
    refused(state, "GREEKS_EMPTY")


def test_snapshot_replaces_previous_history_and_end_residue_does_not_clear():
    _, state = setup()
    feed(state, row(AT - timedelta(seconds=5), delta="0.9"))
    feed(state, row(eventFlags=BEGIN | END, delta="0.4"))
    assert get(state)["fields"]["delta"]["value"] == "0.4"
    feed(state, control(END))
    assert get(state)["fields"]["delta"]["value"] == "0.4"


@pytest.mark.parametrize("defer", [0, TX])
def test_snipped_snapshot_stays_incomplete_until_full_snapshot(defer):
    _, state = setup()
    feed(state, row(eventFlags=BEGIN | SNIP | defer))
    if defer:
        refused(state, "TRANSACTION_INCOMPLETE")
        feed(state, control(0))
    refused(state, "SNAPSHOT_TRUNCATED")
    feed(state, row())
    refused(state, "SNAPSHOT_TRUNCATED")
    feed(state, row(eventFlags=BEGIN | END))
    assert get(state)["status"] == "CURRENT_CALCULATION"


def test_remove_latest_never_revives_older_calculation():
    _, state = setup()
    feed(state, row(AT - timedelta(seconds=180)))
    feed(state)
    feed(state, row(eventFlags=REMOVE, time=None, sequence=None, delta=None))
    refused(state, "LATEST_GREEK_CALCULATION_REMOVED")
    feed(state, row(AT - timedelta(seconds=180), delta="0.4"))
    refused(state, "LATEST_GREEK_CALCULATION_REMOVED")
    feed(state, row(delta="0.5"))
    assert get(state)["fields"]["delta"]["value"] == "0.5"


def test_remove_older_index_does_not_withhold_latest_and_queued_correction_is_atomic():
    _, state = setup()
    old = AT - timedelta(seconds=180)
    feed(state, row(old))
    feed(state)
    feed(state, row(old, eventFlags=REMOVE | TX))
    refused(state, "TRANSACTION_INCOMPLETE")
    feed(state, row(delta="0.51"))
    assert get(state)["fields"]["delta"]["value"] == "0.51"


@pytest.mark.parametrize("field,value", [("index", True), ("index", float(2**60)), ("index", -1),
    ("sequence", True), ("sequence", MAX_SEQUENCE+1), ("time", True), ("time", 0),
    ("eventFlags", True), ("eventFlags", 32), ("eventFlags", -1), ("index", 123)])
def test_bad_protocol_field_cannot_leave_prior_current_value(field, value):
    _, state = setup()
    feed(state)
    assert not feed(state, row(**{field: value}))
    with pytest.raises(QuoteUnavailable):
        get(state)
    feed(state)
    if field == "eventFlags":
        # An invalid flag cannot establish that the dropped row was atomic.
        refused(state, "FLAGS_INVALID")
        feed(state, row(eventFlags=BEGIN | END))
    assert get(state)["status"] == "CURRENT_CALCULATION"


def test_future_and_zero_source_times_never_freshen_cached_calculation():
    _, state = setup()
    feed(state)
    assert not feed(state, row(AT + timedelta(milliseconds=1)))
    refused(state, "FUTURE_SOURCE_TIME")
    assert not feed(state, row(time=0, index=0))
    refused(state, "SOURCE_TIME_UNAVAILABLE")


def test_clock_backwards_rejected_at_receive_and_use():
    _, state = setup()
    feed(state)
    refused(state, "CURRENT_CLOCK_INVALID", AT - timedelta(milliseconds=1))
    assert not feed(state, row(), AT - timedelta(milliseconds=1))
    refused(state, "RECEIPT_MOVED_BACKWARDS")


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", {}, "broken"])
def test_bad_numeric_field_stays_partial_without_borrowing_previous(value):
    _, state = setup()
    feed(state)
    feed(state, row(delta=value))
    result = get(state)
    assert result["status"] == "PARTIAL_CALCULATION"
    assert result["fields"]["delta"]["state"] != "VALUE"
    assert result["fields"]["vega"]["value"] == "0.44"


@pytest.mark.parametrize("field", ["price", "volatility"])
def test_negative_input_is_partial_not_an_executable_quote(field):
    _, state = setup()
    feed(state, row(**{field: "-1"}))
    assert get(state)["status"] == "PARTIAL_CALCULATION"


def test_failure_isolated_to_symbol():
    _, state = setup()
    feed(state)
    feed(state, row(eventSymbol=".SPY261009P770"), symbol=PUT)
    feed(state, row(index=1))
    with pytest.raises(QuoteUnavailable):
        get(state)
    assert get(state, symbol=PUT)["status"] == "CURRENT_CALCULATION"


def test_schema_change_and_reconnect_require_new_calculations():
    source, state = setup()
    feed(state)
    state.schema(changed=False)
    assert get(state)
    state.schema(changed=True)
    refused(state, "NOT_RECEIVED")
    feed(state)
    source.disconnect("DISCONNECTED")
    refused(state, "DISCONNECTED")
    source.begin(AT + AGE)
    source.ready(AT)
    refused(state, "SESSION_CHANGED")
    state.begin(source.generation)
    state.schema()
    refused(state, "NOT_RECEIVED")
    feed(state)
    assert get(state)


@pytest.mark.parametrize("failure", ["token", "identity", "identity_change"])
def test_live_health_required_every_time(failure):
    source, state = setup()
    feed(state)
    if failure == "token":
        source.expires_at = AT
        refused(state, "TOKEN_EXPIRED")
    elif failure == "identity":
        source.invalidate(CALL, "IDENTITY_REFRESH_FAILED")
        refused(state, "IDENTITY_REFRESH_FAILED")
    else:
        source.register(instrument(CALL, option(**{"streamer-symbol": ".NEW261009C770"}), AT))
        refused(state, "IDENTITY_CHANGED")


def test_memory_bounds_withhold_instead_of_silently_eviction():
    _, state = setup(max_entries=1)
    feed(state)
    assert not feed(state, row(AT - timedelta(seconds=100)))
    refused(state, "BOUND_EXCEEDED")
    feed(state, row(eventFlags=BEGIN | END))
    assert get(state)


def test_recorder_reordered_maps_withdrawal_isolated_and_identity_verified():
    source = with_option()
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE)
    session = open_measure(source, rec)
    feed_measure(session, "Greeks", [row()], AT)
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "CURRENT_CALCULATION"
    names = list(reversed(qm.MEASURE_FIELDS["Greeks"]))
    rec.configure(dict(eventFields={"Greeks": names}), AT)
    assert rec.greek_report(AT)["checks"][CALL]["reason"] == "GREEKS_NOT_RECEIVED"
    rec.data(dict(data=["Greeks", [row()[n] for n in names]]), AT)
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "CURRENT_CALCULATION"
    rec.configure(dict(eventFields={"Greeks": names}), AT)
    assert rec.greek_report(AT)["checks"][CALL]["status"] == "CURRENT_CALCULATION"
    rec.configure(dict(eventFields={"Greeks": ["eventType", "eventSymbol", "time"]}), AT)
    assert rec.greek_report(AT)["checks"][CALL]["reason"] == "GREEK_SCHEMA_MISSING_INDEX_FIELDS"
    assert source.connected  # core quote/trade health remains independent
    assert rec.records[0]["label"] == "RAW_OBSERVATION"


def test_measurement_disconnect_withholds_state_and_raw_rows_remain_history():
    source = with_option()
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE)
    open_measure(source, rec)
    rec._greeks_row(CALL, row(), AT)
    rec.down("MEASURE_CHANNEL_CLOSED", AT)
    assert rec.greek_report(AT)["checks"][CALL]["reason"] == "MEASURE_CHANNEL_CLOSED"
    assert rec.report()["greeks"]["current_state"] == "NOT_REDUCED_RAW_OBSERVATIONS_ONLY"
    assert len(rec.records) == 1 and source.connected


@pytest.mark.parametrize("initial", [TX, BEGIN])
def test_corrupt_transaction_tail_cannot_publish_partial_snapshot(initial):
    _, state = setup()
    feed(state)
    feed(state, row(eventFlags=initial))
    feed(state, row(index=1))
    assert not feed(state, row(AT - timedelta(seconds=100), eventFlags=END))
    refused(state, "INDEX_TIME_MISMATCH")
    assert feed(state, row(eventFlags=BEGIN | END))
    assert get(state)["status"] == "CURRENT_CALCULATION"


def test_invalid_begin_cannot_be_repaired_by_end_tail():
    _, state = setup()
    feed(state)
    feed(state, row(eventFlags=BEGIN, index=1))
    assert not feed(state, row(eventFlags=END))
    refused(state, "INDEX_TIME_MISMATCH")


def test_missing_numeric_value_never_copies_earlier_calculation():
    _, state = setup()
    feed(state)
    update = row(AT - timedelta(seconds=100))
    update.pop("vega")
    feed(state, update)
    result = get(state)
    assert result["status"] == "PARTIAL_CALCULATION"
    assert result["fields"]["vega"]["state"] != "VALUE"


def test_remove_and_readd_same_index_in_transaction_keeps_corrected_value():
    _, state = setup()
    feed(state)
    feed(state, row(eventFlags=REMOVE | TX))
    refused(state, "TRANSACTION_INCOMPLETE")
    feed(state, row(delta="0.41"))
    assert get(state)["fields"]["delta"]["value"] == "0.41"


def test_core_disconnect_withholds_greeks_without_waiting_for_new_packet():
    source = with_option()
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE)
    session = open_measure(source, rec)
    feed_measure(session, "Greeks", [row()], AT)
    with pytest.raises(QuoteUnavailable):
        session.receive(dict(type="ERROR", channel=0, error="UNAUTHORIZED"), AT)
    result = rec.greek_report(AT)
    assert result["checks"][CALL]["status"] == "UNAVAILABLE"
    assert all(not s.rows and not s.pending for s in rec.greek_state._states.values())


@pytest.mark.parametrize("outcome", ["complete", "fault", "close_fault", "report_fault", "down_fault"])
def test_capture_terminal_greeks_are_labelled_history_and_closed_state_unavailable(outcome):
    from desk.tastytrade_transport import capture, FEED_CHANNEL
    from desk.tastytrade_quotes import FIELDS
    from tests.test_tastytrade_quotes import client_fixture
    source = with_option()
    class Recorder(qm.Recorder):
        def greek_report(self, at):
            if outcome == "report_fault":
                raise RuntimeError("confidential fixture text")
            return super().greek_report(at)
        def down(self, code, at):
            super().down(code, at)
            if outcome == "down_fault":
                raise RuntimeError("confidential fixture text")
    rec = Recorder(source, clock=lambda: AT, max_age=AGE)
    client, _ = client_fixture()
    ticks = [0.0]
    phase = [dict(type="SETUP", channel=0, keepaliveTimeout=60),
             dict(type="AUTH_STATE", channel=0, state="UNAUTHORIZED"),
             dict(type="AUTH_STATE", channel=0, state="AUTHORIZED"),
             dict(type="CHANNEL_OPENED", channel=FEED_CHANNEL),
             dict(type="FEED_CONFIG", channel=FEED_CHANNEL, eventFields={k: list(v) for k, v in FIELDS.items()}),
             dict(type="CHANNEL_OPENED", channel=MEASURE_CHANNEL),
             dict(type="FEED_CONFIG", channel=MEASURE_CHANNEL, eventFields={k: list(v) for k, v in qm.MEASURE_FIELDS.items()}),
             dict(type="FEED_DATA", channel=MEASURE_CHANNEL,
                  data=["Greeks", [row()[n] for n in qm.MEASURE_FIELDS["Greeks"]]])]
    class Socket:
        def __enter__(self):
            self.rows = list(phase)
            return self
        def __exit__(self, *args):
            if outcome in {"fault", "close_fault"}:
                raise OSError("fixture close fault")
        def send(self, raw):
            pass
        def recv(self, timeout):
            ticks[0] += 0.1
            if self.rows:
                return json.dumps(self.rows.pop(0))
            if outcome == "fault":
                raise OSError("fixture network fault")
            raise TimeoutError
    result = capture(client, source, seconds=2, reconnects=0, measure=rec, clock=lambda: AT,
                     monotonic=lambda: ticks[0], connect=lambda _: Socket())
    terminal = result["final_attempt"]["greek_terminal_state"]
    assert terminal["historical"] and not terminal["eligible"]
    assert not rec.greek_state.ready  # withdrawal must precede any reader's cleanup
    assert all(not s.rows and not s.pending for s in rec.greek_state._states.values())
    assert rec.greek_state.report(AT)["checks"][CALL]["status"] == "UNAVAILABLE"
    assert all(not s.rows and not s.pending for s in rec.greek_state._states.values())
    assert not source.connected
    if outcome in {"report_fault", "down_fault"}:
        assert terminal["view"]["error"] == "GREEK_TERMINAL_VIEW_UNAVAILABLE"
        assert "confidential" not in json.dumps(result)
    elif outcome == "complete":
        assert terminal["view"]["checks"][CALL]["status"] == "CURRENT_CALCULATION"
    else:
        assert terminal["view"]["checks"][CALL]["status"] == "UNAVAILABLE"


def test_nonzero_index_timestamp_mismatch_refuses_even_when_both_times_are_plausible():
    _, state = setup()
    feed(state)
    assert not feed(state, row(index=index(AT - timedelta(seconds=119))))
    refused(state, "INDEX_TIME_MISMATCH")


@pytest.mark.parametrize("observed_during_failure", [False, True])
def test_same_identity_recovery_never_revives_pre_failure_greek(observed_during_failure):
    source, state = setup()
    feed(state)
    source.invalidate(CALL, "IDENTITY_REFRESH_FAILED")
    if observed_during_failure:
        refused(state, "IDENTITY_REFRESH_FAILED")
    source.register(instrument(CALL, option(), AT))
    refused(state, "IDENTITY_CHANGED")
    feed(state, row(AT - timedelta(seconds=100)))
    assert get(state)["age_seconds"] == "100"


def test_healthy_identity_refresh_preserves_existing_calculation_time():
    source, state = setup()
    feed(state)
    source.register(instrument(CALL, option(), AT))
    assert get(state)["age_seconds"] == "120"


def test_stale_identity_read_then_metadata_refresh_needs_new_greek_event():
    source, state = setup()
    feed(state)
    source.identity_lifetime = timedelta(milliseconds=1)
    refused(state, "IDENTITY_STALE")
    source.register(instrument(CALL, option(), AT))
    refused(state, "IDENTITY_STALE")  # the analysis cache retains its failure until a new event
    feed(state)
    assert get(state)["status"] == "CURRENT_CALCULATION"
