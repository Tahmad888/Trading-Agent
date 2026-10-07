"""Live-run package 3: diagnostic measurements that never change eligibility.

Synthetic DXLink rows only; no provider call, no clock command, no credentials.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess

import pytest

from desk import quote_measure as qm
from desk.quote_check import diagnostic
from desk.tastytrade_quotes import CORE, FIELDS, FeedDecoder, QuoteService, QuoteUnavailable, instrument
from desk.tastytrade_transport import FEED_CHANNEL, MEASURE_CHANNEL, Session, StreamToken
from tests.test_tastytrade_quotes import (AGE, AT, client_fixture, handshake, ms, option, quote_row, service,
                                         stock, trade_row)

MAP = {k: list(FIELDS[k]) for k in CORE}


def values(kind, row, names=None):
    return [row.get(n) for n in (names or FIELDS[kind])]


def recorded(source=None, *, at=AT, **kwargs):
    source = source or service()
    rec = qm.Recorder(source, clock=lambda: at + timedelta(milliseconds=5), max_age=AGE, **kwargs)
    decoder = FeedDecoder(source, CORE, observer=rec, channel=FEED_CHANNEL)
    decoder.configure(dict(dataFormat="COMPACT", eventFields=MAP))
    return source, rec, decoder


def bbo(rec):
    return [r for r in rec.records if r["type"] == "BBO"]


# ---- bid/ask: raw side-time states, schema cause vs data cause -----------------------------------
@pytest.mark.parametrize("value, state", [(0, "ZERO"), (None, "NULL"), ("NaN", "NAN"), (1.5, "INVALID"),
                                          (-1, "INVALID")])
def test_side_time_states_are_recorded_and_eligibility_is_unchanged(value, state):
    source, rec, decoder = recorded()
    row = quote_row(bidTime=value, askTime=0)
    decoder.data(dict(data=["Quote", values("Quote", row)]), AT)
    record = bbo(rec)[0]
    assert (record["bidTime"]["state"], record["askTime"]["state"]) == (state, "ZERO")
    assert record["received_at"] == AT.isoformat() and record["decision_at"] != record["received_at"]
    assert record["getter"]["status"] == "UNAVAILABLE"
    with pytest.raises(QuoteUnavailable) as refused:
        source.quote("SPY", AT, AGE)
    assert record["getter"]["reason"] == str(refused.value)
    if value in (0, None):
        assert str(refused.value) == "QUOTE_TIME_UNAVAILABLE"   # a changed BBO with zero times stays unavailable


def test_valid_and_future_side_times_keep_their_own_source_time():
    source, rec, decoder = recorded()
    good, future = quote_row(), quote_row(bidTime=ms(AT + timedelta(milliseconds=4)))
    decoder.data(dict(data=["Quote", values("Quote", good) + values("Quote", future)]), AT)
    first, second = bbo(rec)
    assert first["bidTime"]["state"] == "VALUE" and first["bidTime"]["at"] == AT.isoformat()
    assert first["getter"] == {"status": "AVAILABLE"}
    assert second["bidTime"] == {"state": "FUTURE", "raw": ms(AT) + 4, "at": (AT + timedelta(milliseconds=4)).isoformat(),
                                 "lead_ms": 4.0}
    assert second["getter"]["reason"] == "FUTURE_SOURCE_TIME" and not second["accepted_by_service"]


def test_recorder_does_not_change_any_decision():
    plain, observed = service(), service()
    _, rec, decoder = recorded(observed)
    bare = FeedDecoder(plain, CORE)
    bare.configure(dict(dataFormat="COMPACT", eventFields=MAP))
    rows = (values("Quote", quote_row(bidTime=0)) + values("Quote", quote_row())
            + values("Quote", quote_row("QQQ", bidTime=ms(AT + timedelta(seconds=1)))))
    trades = values("Trade", trade_row()) + values("Trade", trade_row("QQQ", time=ms(AT + timedelta(seconds=1))))
    for dec in (bare, decoder):
        dec.data(dict(data=["Quote", rows, "Trade", trades]), AT)

    def strip(view):
        for check in view["checks"]:
            check.pop("generation")
            for part in ("trade", "quote"):
                check[part].pop("provenance", None)
        view.pop("generation")
        return view
    assert strip(plain.inspect(AT)) == strip(observed.inspect(AT))
    assert rec.counts["trade_time"]["QQQ"] == {"FUTURE": 1}


def test_reordered_withdrawn_and_restored_maps_are_recorded():
    source, rec, decoder = recorded()
    reordered = list(reversed(MAP["Quote"]))
    decoder.configure(dict(dataFormat="COMPACT", eventFields={"Quote": reordered}))
    row = quote_row()
    decoder.data(dict(data=["Quote", values("Quote", row, reordered)]), AT)
    assert bbo(rec)[0]["bidTime"]["state"] == "VALUE" and bbo(rec)[0]["revision"] == 2
    without = [n for n in MAP["Quote"] if n != "askTime"]
    decoder.configure(dict(dataFormat="COMPACT", eventFields={"Quote": without}))
    decoder.configure(dict(dataFormat="COMPACT", eventFields={"Quote": MAP["Quote"]}))
    history = [(e["kind"], e["outcome"], e["revision"]) for e in rec.schema_log if e["kind"] == "Quote"]
    assert history == [("Quote", "ACCEPTED", 1), ("Quote", "CHANGED", 2), ("Quote", "WITHDRAWN_INVALID_MAP", 2),
                       ("Quote", "RESTORED", 3)]
    withdrawn = rec.schema_log[-2]
    assert withdrawn["accepted"] is None and withdrawn["side_times_in_offered_map"] == {"bidTime": True,
                                                                                        "askTime": False}
    assert rec.schema_log[-1]["side_times_in_accepted_map"] == {"bidTime": True, "askTime": True}


def test_an_observer_fault_never_reaches_the_quote_path():
    source = service()

    class Broken(qm.Recorder):
        def row(self, *args):
            raise RuntimeError("boom")
    rec = Broken(source, clock=lambda: AT, max_age=AGE)
    decoder = FeedDecoder(source, CORE, observer=rec, channel=FEED_CHANNEL)
    decoder.configure(dict(dataFormat="COMPACT", eventFields=MAP))
    decoder.data(dict(data=["Quote", values("Quote", quote_row())]), AT)
    assert source.quote("SPY", AT, AGE) and rec.fault == "RECORDER_FAULT:RuntimeError"


# ---- the measurement channel: separate, never feeds the service ---------------------------------
def measured_session(source, rec):
    session = Session(source, StreamToken("wss://fixture", "secret", AT + AGE, "api"), AT, measure=rec)
    session.receive(dict(type="SETUP", channel=0, keepaliveTimeout=60), AT)
    session.receive(dict(type="AUTH_STATE", channel=0, state="UNAUTHORIZED"), AT)
    session.receive(dict(type="AUTH_STATE", channel=0, state="AUTHORIZED"), AT)
    opened = session.receive(dict(type="CHANNEL_OPENED", channel=FEED_CHANNEL), AT)
    session.receive(dict(type="FEED_CONFIG", channel=FEED_CHANNEL, dataFormat="COMPACT", eventFields=MAP), AT)
    return session, opened


def with_option():
    source = service()
    source.register(instrument(option()["symbol"], option(), AT - timedelta(seconds=1)))
    return source


def test_measurement_channel_requests_volume_and_greeks_only_there():
    source = with_option()
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE)
    session, opened = measured_session(source, rec)
    assert {"type": "CHANNEL_REQUEST", "channel": MEASURE_CHANNEL, "service": "FEED",
            "parameters": {"contract": "AUTO"}} in opened
    core = next(m for m in opened if m["type"] == "FEED_SETUP")
    assert core["acceptEventFields"] == MAP                               # core request unchanged
    setup, sub = session.receive(dict(type="CHANNEL_OPENED", channel=MEASURE_CHANNEL), AT)
    assert setup["acceptEventFields"] == {k: list(v) for k, v in qm.MEASURE_FIELDS.items()}
    assert sorted((a["type"], a["symbol"]) for a in sub["add"]) == [
        ("Greeks", ".SPY261009C770"), ("Trade", "QQQ"), ("Trade", "SPY")]


def volume_row(symbol="SPY", *, at=AT, volume="1000", day=20366, price="770.1"):
    return dict(eventType="Trade", eventSymbol=symbol, time=ms(at), price=price, size="10", dayId=day,
                dayVolume=volume)


def feed_measure(session, kind, rows, received):
    names = qm.MEASURE_FIELDS[kind]
    data = [v for row in rows for v in (row.get(n) for n in names)]
    session.receive(dict(type="FEED_DATA", channel=MEASURE_CHANNEL, data=[kind, data]), received)


def open_measure(source, rec):
    session, _ = measured_session(source, rec)
    session.receive(dict(type="CHANNEL_OPENED", channel=MEASURE_CHANNEL), AT)
    session.receive(dict(type="FEED_CONFIG", channel=MEASURE_CHANNEL, dataFormat="COMPACT",
                         eventFields={k: list(v) for k, v in qm.MEASURE_FIELDS.items()}), AT)
    return session


def test_measurement_rows_never_feed_the_quote_service_and_its_faults_stay_there():
    source = with_option()
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE)
    session = open_measure(source, rec)
    before = dict(source.events)
    feed_measure(session, "Trade", [volume_row()], AT)
    assert source.events == before
    with pytest.raises(QuoteUnavailable, match="TRADE_UNAVAILABLE"):
        source.trade("SPY", AT, AGE)
    assert session.receive(dict(type="ERROR", channel=MEASURE_CHANNEL, error="TIMEOUT"), AT) == []
    assert rec.state == "UNAVAILABLE" and source.connected
    session.receive(dict(type="FEED_DATA", channel=FEED_CHANNEL, data=["Trade", values("Trade", trade_row())]), AT)
    assert source.trade("SPY", AT, AGE)                                    # core continues
    feed_measure(session, "Trade", [volume_row(volume="2000")], AT)       # withheld channel: ignored
    assert len([r for r in rec.records if r["type"] == "VOLUME"]) == 1


# ---- volume -------------------------------------------------------------------------------------
def transitions(rec, symbol="SPY"):
    return [r["transition"] for r in rec.records if r["type"] == "VOLUME" and r["symbol"] == symbol]


def test_volume_with_a_fixed_trade_time_reset_nan_decrease_and_reconnect_gap():
    source = with_option()
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE)
    session = open_measure(source, rec)
    fixed = AT - timedelta(minutes=5)
    rows = [volume_row(at=fixed, volume="1000"), volume_row(at=fixed, volume="1500"),
            volume_row(at=fixed, volume="1500"), volume_row(at=fixed, volume="NaN"),
            volume_row(at=fixed, volume="1600"), volume_row(at=fixed, volume="1400"),
            volume_row(at=fixed, volume="0", day=20367)]
    for n, row in enumerate(rows):
        feed_measure(session, "Trade", [row], AT + timedelta(seconds=n))
    assert transitions(rec) == ["FIRST_OBSERVATION", "INCREASE", "UNCHANGED", "VOLUME_UNAVAILABLE",
                                "AFTER_UNAVAILABLE", "DECREASE_OR_CORRECTION", "DAY_RESET"]
    second = [r for r in rec.records if r["type"] == "VOLUME"][1]
    assert second["trade_time_fixed_while_volume_changed"] is True
    assert second["rth_trade_time"]["at"] == fixed.isoformat() and second["received_at"] == (AT + timedelta(seconds=1)).isoformat()
    assert rec.report()["volume"]["rth_window_total"] == "NOT_DERIVED"
    later = AT + timedelta(seconds=30)
    again = Session(source, StreamToken("wss://fixture", "secret", later + AGE, "api"), later, measure=rec)
    assert again.generation != session.generation and rec.fields == {}
    session = open_measure(source, rec)
    feed_measure(session, "Trade", [volume_row(volume="5000", day=20367)], later)
    last = [r for r in rec.records if r["type"] == "VOLUME"][-1]
    assert last["transition"] == "RECONNECT_SNAPSHOT" and last["unobserved_gap_ms"] > 0


def test_receipt_brackets_around_the_opening_window():
    source = with_option()
    opened = datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc)
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE, boundaries=(opened, opened + timedelta(minutes=30)))
    session = open_measure(source, rec)
    for seconds, volume in ((-2, "100"), (1, "150"), (1799, "900"), (1801, "950")):
        feed_measure(session, "Trade", [volume_row(volume=volume)], opened + timedelta(seconds=seconds))
    edges = rec.report()["volume"]["by_symbol"]["SPY"]["receipt_brackets"]
    assert [edges[k]["day_volume"] for k in ("before_open", "after_open", "before_end", "after_end")] == [
        "100", "150", "900", "950"]


# ---- Greeks ----------------------------------------------------------------------------------------
def greeks_row(at, flags=0, index=1, sequence=0, **values_):
    row = dict(eventType="Greeks", eventSymbol=".SPY261009C770", eventFlags=flags, index=index, time=ms(at),
               sequence=sequence, price="5.10", volatility="0.21", delta="0.52", gamma="0.04", theta="-0.31",
               rho="0.05", vega="0.44")
    row.update(values_)
    return row


def test_greeks_ages_use_their_own_time_never_the_latest_trade():
    source = with_option()
    decided = AT + timedelta(seconds=2)
    rec = qm.Recorder(source, clock=lambda: decided, max_age=AGE)
    session = open_measure(source, rec)
    session.receive(dict(type="FEED_DATA", channel=FEED_CHANNEL,
                         data=["Trade", values("Trade", trade_row(at=AT))]), AT)  # newest Trade: another time
    feed_measure(session, "Greeks", [greeks_row(AT - timedelta(seconds=10)),
                                     greeks_row(AT + timedelta(milliseconds=3), sequence=1)], AT)
    old, ahead = [r for r in rec.records if r["type"] == "GREEKS"]
    assert old["receipt_age_ms"] == 10000 and old["decision_age_ms"] == 12000
    assert ahead["source_time"]["state"] == "FUTURE" and ahead["receipt_age_ms"] == -3  # kept negative
    assert old["theta"] == {"state": "VALUE", "value": "-0.31"} and old["label"] == "RAW_OBSERVATION"
    assert rec.report()["greeks"]["current_state"] == "NOT_REDUCED_RAW_OBSERVATIONS_ONLY"


def test_wire_decoded_decimal_values_are_recorded_as_values():
    # Regression (2026-10-07 cloud capture at d539d97): the transport's json_read decodes
    # numbers as Decimal, and every live BBO price/size, dayVolume and Greek was recorded
    # INVALID. Feed the same path from wire text.
    from desk.tastytrade_transport import json_read
    source = with_option()
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE)
    session = open_measure(source, rec)
    names = qm.MEASURE_FIELDS["Greeks"]
    row = greeks_row(AT - timedelta(seconds=1))
    wire = json.dumps({"type": "FEED_DATA", "channel": MEASURE_CHANNEL,
                       "data": ["Greeks", [float(row[n]) if n in qm.GREEK_VALUES else row[n] for n in names]]})
    message = json_read(wire)
    assert isinstance(message["data"][1][names.index("delta")], Decimal)
    session.receive(message, AT)
    greek = [r for r in rec.records if r["type"] == "GREEKS"][0]
    assert greek["delta"] == {"state": "VALUE", "value": "0.52"} and greek["theta"]["value"] == "-0.31"
    assert qm.number_state({"v": Decimal("NaN")}, "v") == {"state": "NAN"}
    assert qm.number_state({"v": Decimal("Infinity")}, "v") == {"state": "INFINITE"}
    assert qm.number_state({"v": True}, "v")["state"] == "INVALID"


def test_transaction_snapshot_and_removal_markers_are_kept_raw():
    source = with_option()
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE)
    session = open_measure(source, rec)
    feed_measure(session, "Greeks", [greeks_row(AT, flags=0x04 | 0x40 | 0x01), greeks_row(AT, flags=0x02, index=2),
                                     greeks_row(AT, flags=0x08, volatility="NaN")], AT)
    rows = [r for r in rec.records if r["type"] == "GREEKS"]
    assert [r["flags"] for r in rows] == [["TX_PENDING", "SNAPSHOT_BEGIN", "SNAPSHOT_MODE"], ["REMOVE_EVENT"],
                                          ["SNAPSHOT_END"]]
    assert rows[2]["volatility"] == {"state": "NAN"} and [r["index"] for r in rows] == [1, 2, 1]


def test_records_are_bounded_and_counters_complete():
    source, rec, decoder = recorded(max_records=2)
    decoder.data(dict(data=["Quote", values("Quote", quote_row()) * 5]), AT)
    assert len(rec.records) == 2 and rec.dropped == {"BBO": 3}
    names = [n for n in qm.MEASURE_FIELDS["Trade"] if n != "dayId"]     # a server map without dayId
    rec.configure(dict(dataFormat="COMPACT", eventFields={"Trade": names}), AT)
    rec.data(dict(data=["Trade", [volume_row().get(n) for n in names]]), AT)
    kept = [r for r in rec.records if r["type"] == "VOLUME"]
    assert len(kept) == 1 and kept[0]["day_id_raw_state"] == "ABSENT"  # BBO volume never crowds it out
    assert qm.window_boundaries(datetime(2026, 10, 4).date()) is None   # Sunday: no window
    assert sum(rec.counts["bbo_times"]["SPY"].values()) == 5 and len(rec.samples["3:Quote"]) == qm.SAMPLES


# ---- bounded opening-window capture ------------------------------------------------------------
def test_volume_capture_runs_bounded_segments_and_stops_on_a_fault():
    client, _ = client_fixture(dict(data=stock()))
    calls = []

    def fake(client, source, *, seconds, reconnects, measure):
        calls.append(seconds)
        assert seconds <= 600 and measure is not None
        return dict(stop_reason="CAPTURE_COMPLETE" if len(calls) < 3 else "DXLINK_DENIED_OR_CLOSED", attempts=1)
    report = qm.volume_capture(client, QuoteService(), ["SPY"], seconds=2400, capture_fn=fake, clock=lambda: AT)
    assert calls == [600, 600, 600] and report["stopped"] == "DXLINK_DENIED_OR_CLOSED"
    assert report["status"] == "STOPPED" and len(report["segments"]) == 3
    calls.clear()
    assert qm.volume_capture(client, QuoteService(), ["SPY"], seconds=1300, capture_fn=lambda *a, **k: (
        calls.append(k["seconds"]) or dict(stop_reason="CAPTURE_COMPLETE")), clock=lambda: AT)["status"] == "OBSERVATIONS_ONLY"
    assert calls == [600, 600, 100]
    with pytest.raises(QuoteUnavailable, match="INVALID_VOLUME_BOUNDS"):
        qm.volume_capture(client, QuoteService(), ["SPY"], seconds=3001, capture_fn=fake, clock=lambda: AT)


@pytest.mark.parametrize("argv", [["--seconds", "3001", "--symbols", "SPY"],
                                  ["--seconds", "60", "--symbols", "A", "B", "C", "D", "E", "F"]])
def test_volume_cli_bounds(argv, tmp_path):
    with pytest.raises(SystemExit):
        qm.main(["volume", "--environment", "production", "--output", str(tmp_path / "v.json"), *argv], env={})


# ---- host clock ----------------------------------------------------------------------------------
SNTP_OUT = "2026-10-05 09:12:01.123456 (+0400) -0.004213 +/- 0.021300 time.apple.com 17.253.4.125 s1 no-leap\n"


def test_sntp_runs_read_only_and_is_never_applied():
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=SNTP_OUT, stderr="")
    result = qm.measure_clock(run=run, clock=lambda: AT)
    assert seen == [["sntp", "time.apple.com"]]
    assert result["status"] == "MEASURED" and result["offset_seconds_as_printed"] == "-0.004213"
    assert result["plus_minus_seconds_as_printed"] == "0.021300" and result["applied"] is False
    assert result["tolerance"] == "NONE"


@pytest.mark.parametrize("behaviour, code", [("missing", "SNTP_NOT_FOUND"), ("timeout", "SNTP_TIMEOUT"),
                                              ("fail", "SNTP_FAILED"), ("garbled", "SNTP_OUTPUT_UNRECOGNIZED")])
def test_failed_clock_measurement_is_unavailable(behaviour, code):
    def run(command, **kwargs):
        if behaviour == "missing":
            raise FileNotFoundError
        if behaviour == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(command, 1 if behaviour == "fail" else 0, stdout="no answer", stderr="")
    result = qm.measure_clock(run=run, clock=lambda: AT)
    assert (result["status"], result["code"]) == ("UNAVAILABLE", code)


def test_host_clock_file_states(tmp_path):
    assert qm.load_host_clock(None)["status"] == "NOT_MEASURED"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert qm.load_host_clock(bad)["code"] == "HOST_CLOCK_FILE_UNREADABLE"
    good = tmp_path / "clock.json"
    good.write_text(json.dumps(qm.measure_clock(run=lambda c, **k: subprocess.CompletedProcess(c, 0, SNTP_OUT, ""),
                                                clock=lambda: AT)))
    loaded = qm.load_host_clock(good)
    assert loaded["status"] == "MEASURED" and loaded["applied"] is False
    forged = json.loads(good.read_text()) | {"offset_seconds_as_printed": "soon"}
    good.write_text(json.dumps(forged))
    assert qm.load_host_clock(good)["code"] == "HOST_CLOCK_FILE_INVALID"


def test_quote_check_carries_the_measurement_and_clock_without_applying_them(tmp_path):
    client, _ = client_fixture(dict(data=stock()))
    source = QuoteService()
    rec = qm.Recorder(source, clock=lambda: AT, max_age=AGE)
    seen = {}

    def captured(client, source, **kwargs):
        seen.update(kwargs)
        return dict(stop_reason="CAPTURE_COMPLETE", observations=[], requests=1, final_attempt=None, attempts=1,
                    connected_after_capture=False)
    clock = {"status": "MEASURED", "offset_seconds_as_printed": "-0.004", "applied": False}
    report = diagnostic(client, source, ["SPY"], capture_fn=captured, clock=lambda: AT, measure=rec, host_clock=clock)
    assert seen["measure"] is rec and report["measurements"]["measurement_channel"] == MEASURE_CHANNEL
    assert report["clock"]["host_clock"] == clock and report["clock"]["tolerance_applied"] == "NONE"
    seen.clear()
    plain = diagnostic(client, QuoteService(), ["SPY"], capture_fn=captured, clock=lambda: AT)
    assert "measure" not in seen and plain["measurements"] == {"status": "NOT_REQUESTED"}
    assert plain["clock"]["host_clock"]["status"] == "NOT_MEASURED"


# ---- reports -------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["value secret-refresh-value", "Bearer abc", "x?token=abc", "access_token"])
def test_reports_with_credentials_are_refused(tmp_path, text):
    out = qm.write_report({"purpose": "p", "note": text}, tmp_path / "r.json",
                          env={"TASTYTRADE_REFRESH_TOKEN": "secret-refresh-value"})
    saved = (tmp_path / "r.json").read_text()
    assert out["code"] == "REPORT_CREDENTIAL_MATCH" and text not in saved


def test_compare_is_side_by_side_without_tolerance():
    edges = {k: {"received_at": AT.isoformat(), "day_volume": v, "day_id": 1, "generation": "g"}
             for k, v in (("before_open", "100"), ("after_open", "150"), ("before_end", "900"), ("after_end", "950"))}
    capture = {"started_at": "2026-10-05T13:25:00+00:00",
               "measurements": {"volume": {"by_symbol": {"SPY": {"receipt_brackets": dict(edges, generations_between=["g"])},
                                                         "NVDA": {"receipt_brackets": dict(edges, generations_between=["g", "h"])}}}}}
    alpaca = {"entry_session": "2026-10-05",
              "requests": {"RTH30": {"request": {"params": {"feed": "sip", "adjustment": "raw", "timeframe": "15Min",
                                                            "start": "2026-10-05T13:30:00Z", "end": "2026-10-05T13:59:59Z"}}}},
              "tickers": {"SPY": {"status": "AVAILABLE", "first30_volume": "800", "first30_intervals": []},
                          "NVDA": {"status": "AVAILABLE", "first30_volume": "800", "first30_intervals": []},
                          "QQQ": {"status": "AVAILABLE", "first30_volume": "10", "first30_intervals": []}}}
    out = qm.compare(capture, alpaca)
    assert out["tolerance"] == "NONE" and out["alpaca_definition"]["adjustment"] == "raw"
    assert out["by_symbol"]["SPY"]["status"] == "WITHIN_RECEIPT_BRACKETS_NOT_PROOF"
    assert out["by_symbol"]["SPY"]["tastytrade"]["label"] == "NOT_AN_RTH_TOTAL"
    nvda = out["by_symbol"]["NVDA"]
    assert nvda["status"] == "WITHIN_RECEIPT_BRACKETS_NOT_PROOF" and nvda["tastytrade"]["connections_spanned"] == 2
    assert out["by_symbol"]["SPY"]["tastytrade"]["open_bracket_ms"] == 0
    assert out["by_symbol"]["QQQ"]["tastytrade"]["status"] == "BRACKETS_INCOMPLETE"
    alpaca["tickers"]["SPY"]["first30_volume"] = "1000"
    assert qm.compare(capture, alpaca)["by_symbol"]["SPY"]["status"] == "OUTSIDE_RECEIPT_BRACKETS_UNRESOLVED"
    spy = capture["measurements"]["volume"]["by_symbol"]["SPY"]["receipt_brackets"]
    spy["after_end"] = dict(spy["after_end"], day_id=2)
    assert qm.compare(capture, alpaca)["by_symbol"]["SPY"]["tastytrade"]["status"] == "NOT_COMPARABLE_DAY_ID_CHANGED"
    spy["after_end"] = dict(spy["after_end"], day_id=None)
    assert qm.compare(capture, alpaca)["by_symbol"]["SPY"]["tastytrade"]["status"] == "NOT_COMPARABLE_DAY_ID_UNAVAILABLE"
    alpaca["entry_session"] = "2026-10-02"
    assert qm.compare(capture, alpaca)["code"] == "SESSION_MISMATCH"


def test_measurements_stay_out_of_decision_modules():
    root = Path(__file__).resolve().parents[1] / "src" / "desk"
    importer = re.compile(r"^\s*(from desk(\.quote_measure| import quote_measure)|import desk\.quote_measure)", re.M)
    users = sorted(p.name for p in root.rglob("*.py") if importer.search(p.read_text()))
    assert users == ["option_conventions.py", "quote_check.py", "snapshot_quote_check.py",
                     "tradier_option_check.py", "webull_quote_check.py"]  # diagnostics only


def test_every_documented_command_and_flag_parses_without_a_request(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TASTYTRADE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("TASTYTRADE_REFRESH_TOKEN", raising=False)
    clock = tmp_path / "clock.json"
    original = qm.measure_clock
    monkeypatch.setattr(qm, "measure_clock", lambda server: original(
        server, run=lambda c, **k: subprocess.CompletedProcess(c, 0, SNTP_OUT, ""), clock=lambda: AT))
    assert qm.main(["clock", "--output", str(clock)], env={}) == 0
    from desk.quote_check import main as check
    out = tmp_path / "quote.json"
    assert check(["--symbols", "SPY", "--environment", "production", "--option-underlying", "SPY", "--measure",
                  "--host-clock", str(clock), "--seconds", "600", "--output", str(out)]) == 1
    assert json.loads(out.read_text())["reason"] == "CREDENTIALS_MISSING"
    with pytest.raises(SystemExit):     # quote_check keeps its 600-second bound
        check(["--symbols", "SPY", "--environment", "production", "--seconds", "601", "--output", str(out)])
    volume = tmp_path / "volume.json"
    assert qm.main(["volume", "--environment", "production", "--symbols", "SPY", "NVDA", "--seconds", "2400",
                    "--max-requests", "20", "--host-clock", str(clock), "--output", str(volume)], env={}) == 1
    saved = json.loads(volume.read_text())
    assert saved["reason"] == "CREDENTIALS_MISSING" and saved["host_clock"]["status"] == "MEASURED"
    probe = tmp_path / "result.json"
    probe.write_text(json.dumps({"entry_session": "2026-10-05", "tickers": {}}))
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({"started_at": "2026-10-05T13:25:00+00:00", "measurements": {}}))
    assert qm.main(["compare", "--capture", str(capture), "--alpaca-result", str(probe),
                    "--output", str(tmp_path / "compare.json")], env={}) == 0
