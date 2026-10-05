"""Child 3 protocol, timing and diagnostic regressions. Synthetic DXLink only: no provider call.

Server scripts follow the dxLink specification's permitted orderings (lazy FEED_CONFIG
before first data, configuration updates, per-type maps); they are not observations of
tastytrade's live server.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.message import Message
import io
import json
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

import pytest

from desk.quote_check import diagnostic
from desk.tastytrade_quotes import CORE, FIELDS, FeedDecoder, QuoteService, QuoteUnavailable, instrument
from desk.tastytrade_transport import (PROFILE_CHANNEL, Credentials, ReadClient, Session, StreamToken, _NoRedirect, capture,
                                      connect_ws, request_json, select_pair, stream_endpoint_allowed)
from tests.test_tastytrade_quotes import AGE, AT, client_fixture, ms, option, quote_row, service, stock, trade_row

CORE_MAP = {k: list(FIELDS[k]) for k in CORE}
TRADE = list(FIELDS["Trade"])


def compact(kind, *rows, names=None):
    names = names or FIELDS[kind]
    return dict(type="FEED_DATA", channel=3, data=[kind, [row[n] for row in rows for n in names]])


class Server:
    """A scripted dxLink server reacting to client messages; records what it was sent."""

    def __init__(self, on_subscribe=(), *, early_fields=CORE_MAP, after_setup=(), profile=None, close_after=None):
        self.on_subscribe, self.early_fields, self.after_setup = list(on_subscribe), early_fields, list(after_setup)
        self.profile, self.close_after, self.sent, self.connections, self.received = profile, close_after, [], 0, 0

    def reply(self, msg):
        kind, channel = msg["type"], msg.get("channel")
        if kind == "SETUP":
            return [dict(type="SETUP", channel=0, keepaliveTimeout=60),
                    dict(type="AUTH_STATE", channel=0, state="UNAUTHORIZED")]
        if kind == "AUTH":
            return [dict(type="AUTH_STATE", channel=0, state="AUTHORIZED")]
        if kind == "CHANNEL_REQUEST":
            return [dict(type="CHANNEL_OPENED", channel=channel)]
        if kind == "FEED_SETUP" and channel == 3:
            config = dict(type="FEED_CONFIG", channel=3, dataFormat="COMPACT", aggregationPeriod=0.1)
            if self.early_fields is not None:
                config["eventFields"] = self.early_fields
            return [config] + self.after_setup
        if kind == "FEED_SETUP" and channel == PROFILE_CHANNEL:
            return list(self.profile or [])
        if kind == "FEED_SUBSCRIPTION" and channel == 3 and msg.get("reset"):
            return list(self.on_subscribe)
        return []

    def factory(self, ticks):
        server = self

        class Socket:
            def __enter__(self):
                server.connections += 1
                self.queue = []
                return self

            def __exit__(self, *a):
                pass

            def send(self, raw):
                msg = json.loads(raw)
                server.sent.append(msg)
                self.queue.extend(server.reply(msg))

            def recv(self, timeout):
                ticks[0] += 0.25
                if self.queue:
                    server.received += 1
                    return json.dumps(self.queue.pop(0), default=str)
                if server.close_after is not None and server.received >= server.close_after:
                    raise OSError("connection reset by peer secret-quote")
                raise TimeoutError
        return lambda url: Socket()


def run(server, *, seconds=15, reconnects=1, source=None, profile=False, client=None, **kwargs):
    client = client or client_fixture()[0]
    source = source or service(("SPY",))
    ticks = [0.0]
    result = capture(client, source, seconds=seconds, reconnects=reconnects, connect=server.factory(ticks),
                     clock=lambda: AT, monotonic=lambda: ticks[0],
                     sleep=lambda s: ticks.__setitem__(0, ticks[0] + s), profile=profile, **kwargs)
    return result, source


def subscriptions(server):
    return [m for m in server.sent if m["type"] == "FEED_SUBSCRIPTION"]


def last(result, symbol="SPY"):
    return next(c for c in result["observations"][-1]["checks"] if c["symbol"] == symbol)


# ---- Package A: lazy, repeated, changed, partial and invalid configuration ----------------------
def test_lazy_config_after_subscription_reaches_data_once():
    """F1: the server withholds the map until it has a subscription (dxLink: lazy config)."""
    server = Server(early_fields=None, on_subscribe=[
        dict(type="FEED_CONFIG", channel=3, dataFormat="COMPACT", aggregationPeriod=0.1, eventFields=CORE_MAP),
        compact("Trade", trade_row(price="770.25"))])
    result, _ = run(server)
    assert len(subscriptions(server)) == 1
    assert result["stop_reason"] == "CAPTURE_COMPLETE" and result["field_schema_accepted"]
    trade = last(result)["trade"]
    assert trade["status"] == "AVAILABLE" and trade["price"] == "770.25" and trade["source_at"] == AT.isoformat()


def test_early_map_still_works():
    server = Server(on_subscribe=[compact("Trade", trade_row())])
    result, _ = run(server)
    assert last(result)["trade"]["status"] == "AVAILABLE" and len(subscriptions(server)) == 1


def test_identical_repeated_config_while_streaming_changes_nothing():
    """F2: a valid second FEED_CONFIG no longer kills the session."""
    again = dict(type="FEED_CONFIG", channel=3, dataFormat="COMPACT", aggregationPeriod=0.1, eventFields=CORE_MAP)
    server = Server(on_subscribe=[compact("Trade", trade_row()), again, again])
    result, source = run(server)
    assert result["stop_reason"] == "CAPTURE_COMPLETE" and result["attempts"] == 1
    assert last(result)["trade"]["status"] == "AVAILABLE" and len(subscriptions(server)) == 1


def test_changed_field_order_is_adopted_and_withholds_earlier_values():
    source = service(("SPY",))
    decoder = FeedDecoder(source)
    decoder.configure(dict(dataFormat="COMPACT", eventFields=CORE_MAP))
    decoder.data(compact("Trade", trade_row(price="770.10")), AT)
    assert source.trade("SPY", AT, AGE)[0].price == Decimal("770.10")
    reordered = ["time", "price", "eventSymbol", "size", "eventType"]
    decoder.configure(dict(dataFormat="COMPACT", eventFields={"Trade": reordered}))
    with pytest.raises(QuoteUnavailable, match="FEED_SCHEMA_CHANGED"):
        source.trade("SPY", AT, AGE)  # decoded under the old map; needs a new event
    later = AT + timedelta(seconds=2)
    decoder.data(compact("Trade", trade_row(at=later, price="771.50"), names=reordered), later)
    value, _ = source.trade("SPY", later, AGE)
    assert value.price == Decimal("771.50") and value.traded_at == later


def test_data_before_its_map_is_never_decoded():
    source = service(("SPY",))
    decoder = FeedDecoder(source)
    decoder.data(compact("Trade", trade_row()), AT)
    with pytest.raises(QuoteUnavailable, match="TRADE_UNAVAILABLE"):
        source.trade("SPY", AT, AGE)
    assert source.undecodable["Trade"] == 1
    decoder.configure(dict(dataFormat="COMPACT", eventFields={"Trade": TRADE}))
    decoder.data(compact("Trade", trade_row()), AT)
    assert source.trade("SPY", AT, AGE)


def test_partial_map_decodes_trade_without_waiting_for_quiet_quote():
    server = Server(early_fields={"Trade": TRADE}, on_subscribe=[compact("Trade", trade_row()),
                                                                  compact("Quote", quote_row())])
    result, source = run(server)
    check = last(result)
    assert result["stop_reason"] == "CAPTURE_COMPLETE"
    assert check["trade"]["status"] == "AVAILABLE" and check["quote"]["status"] == "UNAVAILABLE"
    assert result["schema"]["Quote"] == "NO_ACCEPTED_MAP" and source.undecodable["Quote"] == 1


@pytest.mark.parametrize("config", [
    dict(dataFormat="COMPACT", eventFields={"Trade": ["eventType", "eventSymbol", "price"]}),
    dict(dataFormat="COMPACT", eventFields={"Trade": ["eventType", "eventSymbol", "price", "time", "time"]}),
])
def test_invalid_map_for_one_type_withholds_that_type_only(config):
    source = service(("SPY",))
    decoder = FeedDecoder(source)
    decoder.configure(dict(dataFormat="COMPACT", eventFields=CORE_MAP))
    decoder.data(compact("Trade", trade_row()), AT)
    decoder.data(compact("Quote", quote_row()), AT)
    decoder.configure(config)  # Quote's map is still valid: the session continues
    with pytest.raises(QuoteUnavailable, match="FEED_SCHEMA_UNSUPPORTED"):
        source.trade("SPY", AT, AGE)
    decoder.data(compact("Trade", trade_row()), AT)  # never decoded under the known-bad map
    with pytest.raises(QuoteUnavailable, match="FEED_SCHEMA_UNSUPPORTED"):
        source.trade("SPY", AT, AGE)
    assert source.quote("SPY", AT, AGE) and source.undecodable["Trade"] == 1


@pytest.mark.parametrize("config", [
    dict(dataFormat="FULL", eventFields=CORE_MAP),
    dict(dataFormat="COMPACT", eventFields=["Trade"]),
    dict(dataFormat="COMPACT", eventFields={"Trade": ["eventType"], "Quote": ["eventType"]}),
])
def test_no_usable_core_map_withholds_then_stops(config):
    source = service(("SPY",))
    decoder = FeedDecoder(source)
    decoder.configure(dict(dataFormat="COMPACT", eventFields=CORE_MAP))
    decoder.data(compact("Trade", trade_row()), AT)
    with pytest.raises(QuoteUnavailable, match="FEED_SCHEMA_UNSUPPORTED"):
        decoder.configure(config)
    with pytest.raises(QuoteUnavailable):
        source.trade("SPY", AT, AGE)


def test_responsive_server_without_any_usable_map_reports_schema_unavailable():
    server = Server(early_fields=None, on_subscribe=[])
    result, _ = run(server, seconds=60)
    assert result["stop_reason"] == "DXLINK_SCHEMA_UNAVAILABLE" and len(subscriptions(server)) == 1


# ---- F9: Profile on its own channel; missing or failed Profile never blocks Quote/Trade ------------
PROFILE_MAP = dict(type="FEED_CONFIG", channel=PROFILE_CHANNEL, dataFormat="COMPACT", aggregationPeriod=0.1,
                   eventFields={"Profile": list(FIELDS["Profile"])})


def profile_row(status, symbol="SPY"):
    return dict(type="FEED_DATA", channel=PROFILE_CHANNEL, data=[
        "Profile", ["Profile", symbol, status, 0, 0]])


@pytest.mark.parametrize("status,expected,trade", [("ACTIVE", "ACTIVE", "AVAILABLE"),
                                                   ("HALTED", "HALTED", "UNAVAILABLE"),
                                                   ("UNDEFINED", "UNKNOWN", "AVAILABLE")])
def test_profile_status_is_reported_separately(status, expected, trade):
    server = Server(on_subscribe=[compact("Trade", trade_row())], profile=[PROFILE_MAP, profile_row(status),
                                                                         compact("Trade", trade_row())])
    result, _ = run(server, profile=True)
    check = last(result)
    assert check["trading_status"]["status"] == expected and check["trade"]["status"] == trade
    if status == "HALTED":
        assert check["trade"]["reason"] == "SECURITY_HALTED"  # a halted print is not current evidence


def test_profile_error_or_absence_never_stops_quotes_or_trades():
    errored = Server(on_subscribe=[compact("Trade", trade_row())],
                     profile=[dict(type="ERROR", channel=PROFILE_CHANNEL, error="UNAUTHORIZED", message="x"),
                              compact("Trade", trade_row())])
    result, _ = run(errored, profile=True)
    assert result["stop_reason"] == "CAPTURE_COMPLETE"
    assert last(result)["trading_status"] == dict(status="UNKNOWN", reason="PROFILE_CHANNEL_ERROR_UNAUTHORIZED")
    absent = Server(on_subscribe=[compact("Trade", trade_row())])
    result, _ = run(absent, profile=True)
    assert result["stop_reason"] == "CAPTURE_COMPLETE" and last(result)["trading_status"]["status"] == "UNKNOWN"


def test_profile_traffic_after_profile_is_withheld_never_stops_the_session():
    """A withheld Profile channel keeps sending: Quote/Trade must continue."""
    broken = dict(type="FEED_DATA", channel=PROFILE_CHANNEL, data=["Profile", ["Profile", "SPY", "ACTIVE"]])
    server = Server(on_subscribe=[compact("Trade", trade_row())],
                    profile=[PROFILE_MAP, broken, profile_row("HALTED"), PROFILE_MAP,
                             dict(type="CHANNEL_OPENED", channel=PROFILE_CHANNEL), compact("Trade", trade_row())])
    result, source = run(server, profile=True)
    assert result["stop_reason"] == "CAPTURE_COMPLETE" and result["attempts"] == 1
    check = last(result)
    assert check["trade"]["status"] == "AVAILABLE"
    assert check["trading_status"] == dict(status="UNKNOWN", reason="FEED_DATA_INVALID")
    assert source.undecodable["Profile"] == 1


def test_an_invalid_optional_map_withholds_profile_only():
    bad_map = dict(PROFILE_MAP, eventFields={"Profile": ["eventType", "eventSymbol"]})
    server = Server(on_subscribe=[compact("Trade", trade_row())],
                    profile=[bad_map, profile_row("HALTED"), compact("Trade", trade_row())])
    result, _ = run(server, profile=True)
    assert result["stop_reason"] == "CAPTURE_COMPLETE"
    assert last(result)["trading_status"] == dict(status="UNKNOWN", reason="FEED_SCHEMA_UNSUPPORTED")


def test_invalid_profile_status_is_unknown_not_active():
    source = service(("SPY",))
    assert not source.feed("Profile", dict(eventType="Profile", eventSymbol="SPY", tradingStatus="OPEN"), AT)
    assert source.status("SPY", AT) == ("UNKNOWN", "PROFILE_STATUS_INVALID")


# ---- F10 and connection health: recovery, no revival, no retry loops -----------------------------
def test_heartbeat_timeout_recovers_with_fresh_generation_and_new_events():
    server = Server(on_subscribe=[compact("Trade", trade_row()), compact("Quote", quote_row())])
    # Heartbeat timeout at 60 s; the new connection then ends deliberately at the 100 s
    # deadline, before its own heartbeat limit (child 3, D1: usable at the attempt's end).
    result, source = run(server, seconds=100, reconnects=1)
    assert result["faults"][0]["code"] == "DXLINK_HEARTBEAT_TIMEOUT" and result["faults"][0]["recoverable"]
    assert result["stop_reason"] == "CAPTURE_COMPLETE"
    recovery = result["recoveries"][0]
    assert recovery["recovered"] and recovery["fresh_events"] > 0
    assert recovery["outcome"] == "USABLE_AT_ATTEMPT_END"
    assert recovery["by_symbol"]["SPY"]["Trade"] and recovery["by_symbol"]["SPY"]["Quote"]
    generations = {c["generation"] for view in result["observations"] for c in view["checks"]}
    assert len(generations) == 2 and server.connections == 2
    assert not source.connected  # the bounded capture always ends disconnected


def test_exhausted_retries_report_the_fault_not_a_deadline():
    server = Server(on_subscribe=[compact("Trade", trade_row())])
    result, _ = run(server, seconds=600, reconnects=2)
    assert result["attempts"] == 3 and result["stop_reason"] == "DXLINK_HEARTBEAT_TIMEOUT"
    assert len(result["faults"]) == 3 and not result["deadline_reached"]


def test_deadline_is_distinct_from_a_fault():
    server = Server(on_subscribe=[compact("Trade", trade_row())] * 3)
    result, _ = run(server, seconds=3)
    assert result["stop_reason"] == "CAPTURE_COMPLETE" and result["deadline_reached"] and result["faults"] == []


@pytest.mark.parametrize("message,code", [
    (dict(type="ERROR", channel=0, error="UNAUTHORIZED", message="secret"), "DXLINK_ERROR_UNAUTHORIZED"),
    (dict(type="CHANNEL_CLOSED", channel=3), "DXLINK_DENIED_OR_CLOSED"),
])
def test_denials_are_not_retried(message, code):
    server = Server(on_subscribe=[message])
    result, _ = run(server, reconnects=2)
    assert result["attempts"] == 1 and result["stop_reason"] == code and server.connections == 1


def test_rate_limited_token_refresh_is_not_retried():
    client, calls = client_fixture()
    def limited(method, url, headers, body):
        calls.append(url)
        if url.endswith("/oauth/token"):
            return dict(access_token="secret-access", token_type="Bearer", expires_in=900)
        raise QuoteUnavailable("REST_HTTP_429")
    client.request = limited
    result, _ = run(Server(), client=client, reconnects=2)
    assert result["stop_reason"] == "REST_HTTP_429" and result["attempts"] == 1


def test_token_expiry_stops_and_reports_renewal_pending():
    client, _ = client_fixture()
    server = Server(on_subscribe=[compact("Trade", trade_row())])
    ticks = [0.0]
    times = iter([AT] * 12 + [AT + timedelta(hours=2)] * 1000)
    result = capture(client, service(("SPY",)), seconds=60, reconnects=2, connect=server.factory(ticks),
                     clock=lambda: next(times), monotonic=lambda: ticks[0], sleep=lambda s: None)
    assert result["stop_reason"] == "QUOTE_TOKEN_EXPIRED" and result["attempts"] == 1
    assert result["token_renewal"] == "NOT_IMPLEMENTED_FOR_LONG_RUNNING_SERVICE"


def test_keepalive_after_disconnect_never_revives_the_session():
    source = service(("SPY",))
    session = Session(source, StreamToken("wss://x.dxfeed.com", "t", AT + AGE, "api"), AT)
    for msg in (dict(type="SETUP", channel=0, keepaliveTimeout=60), dict(type="AUTH_STATE", channel=0, state="UNAUTHORIZED"),
                dict(type="AUTH_STATE", channel=0, state="AUTHORIZED"), dict(type="CHANNEL_OPENED", channel=3),
                dict(type="FEED_CONFIG", channel=3, dataFormat="COMPACT", eventFields=CORE_MAP),
                compact("Trade", trade_row())):
        session.receive(msg, AT)
    assert source.trade("SPY", AT, AGE)
    source.disconnect("DXLINK_HEARTBEAT_TIMEOUT")
    with pytest.raises(QuoteUnavailable, match="SESSION_NOT_CONNECTED"):
        session.receive(dict(type="KEEPALIVE", channel=0), AT)
    assert not source.connected
    with pytest.raises(QuoteUnavailable, match="DXLINK_HEARTBEAT_TIMEOUT"):
        source.trade("SPY", AT, AGE)


def test_message_for_a_superseded_session_cannot_disturb_the_new_one():
    source = service(("SPY",))
    old = Session(source, StreamToken("wss://x.dxfeed.com", "t", AT + AGE, "api"), AT)
    source.begin(AT + AGE)
    source.ready(AT)
    source.feed("Trade", trade_row(), AT)
    with pytest.raises(QuoteUnavailable, match="SESSION_SUPERSEDED"):
        old.receive(dict(type="KEEPALIVE", channel=0), AT)
    assert source.connected and source.trade("SPY", AT, AGE)


# ---- C1: ambiguous quote ordering --------------------------------------------------------------
def test_mixed_side_order_withholds_the_quote_until_a_consistent_snapshot():
    source = service(("SPY", "QQQ"))
    t20, t21, t18, t17 = (AT - timedelta(seconds=s) for s in (20, 21, 18, 17))
    source.feed("Quote", quote_row(bidPrice="100.00", askPrice="100.05", bidTime=ms(t20), askTime=ms(t20)), AT)
    source.feed("Quote", quote_row("QQQ"), AT)
    assert not source.feed("Quote", quote_row(bidPrice="99.50", askPrice="99.60", bidTime=ms(t21), askTime=ms(t18)), AT)
    with pytest.raises(QuoteUnavailable, match="QUOTE_ORDER_AMBIGUOUS"):
        source.quote("SPY", AT, AGE)  # the preceding BBO is not usable either
    assert source.quote("QQQ", AT, AGE)
    # Still behind the bid watermark: stays withheld.
    assert not source.feed("Quote", quote_row(bidPrice="99.50", askPrice="99.61", bidTime=ms(t21), askTime=ms(t17)), AT)
    with pytest.raises(QuoteUnavailable, match="QUOTE_ORDER_AMBIGUOUS"):
        source.quote("SPY", AT, AGE)
    assert source.feed("Quote", quote_row(bidPrice="99.55", askPrice="99.62", bidTime=ms(t20), askTime=ms(t17)), AT)
    assert str(source.quote("SPY", AT, AGE).bid) == "99.55"


def test_new_generation_clears_the_ordering_watermark():
    source = service(("SPY",))
    source.feed("Quote", quote_row(bidTime=ms(AT), askTime=ms(AT)), AT)
    source.feed("Quote", quote_row(bidTime=ms(AT - timedelta(seconds=5)), askTime=ms(AT + timedelta(seconds=0))), AT)
    source.begin(AT + AGE)
    source.ready(AT)
    assert source.feed("Quote", quote_row(bidTime=ms(AT - timedelta(seconds=5)), askTime=ms(AT)), AT)


def test_purely_older_quote_and_trade_are_ignored_without_freshening():
    source = service(("SPY",))
    source.feed("Quote", quote_row(bidPrice="100.00", askPrice="100.05", bidTime=ms(AT), askTime=ms(AT)), AT)
    assert not source.feed("Quote", quote_row(bidPrice="1", askPrice="2", bidTime=ms(AT - AGE), askTime=ms(AT - AGE)), AT)
    assert str(source.quote("SPY", AT, AGE).bid) == "100.00"
    source.feed("Trade", trade_row(price="100"), AT)
    later = AT + timedelta(seconds=30)
    assert not source.feed("Trade", trade_row(at=AT - timedelta(seconds=1), price="50"), later)
    value, _ = source.trade("SPY", later, AGE)
    assert value.traded_at == AT and value.price == 100


# ---- C2: clock uncertainty and honest timestamp semantics -----------------------------------------
@pytest.mark.parametrize("lead_ms", [1, 3_600_000])
def test_future_source_time_is_explained_not_clamped(lead_ms):
    source = service(("SPY",))
    assert not source.feed("Trade", trade_row(time=ms(AT) + lead_ms), AT)
    with pytest.raises(QuoteUnavailable, match="FUTURE_SOURCE_TIME"):
        source.trade("SPY", AT, AGE)
    trade = source.inspect(AT)["checks"][0]["trade"]
    assert trade["clock_uncertainty"]["lead_ms"] == lead_ms
    assert "local clock may lag" in trade["clock_uncertainty"]["explanation"]
    assert trade["lag_status"] == "CLOCK_UNCERTAIN_FUTURE_SOURCE_TIME"


@pytest.mark.parametrize("value", [0, None])
def test_zero_or_missing_trade_time_is_unavailable(value):
    source = service(("SPY",))
    assert not source.feed("Trade", trade_row(time=value), AT)
    with pytest.raises(QuoteUnavailable, match="SOURCE_TIME_UNAVAILABLE"):
        source.trade("SPY", AT, AGE)


def test_clearly_old_liquid_trade_is_visibly_aged_and_ineligible():
    source = service(("SPY",))
    source.feed("Trade", trade_row(at=AT - timedelta(minutes=15)), AT)
    with pytest.raises(QuoteUnavailable, match="TRADE_STALE"):
        source.trade("SPY", AT, AGE)
    trade = source.inspect(AT)["checks"][0]["trade"]
    assert trade["lag_status"] == "EXCEEDS_QUOTE_POLICY" and trade["age_seconds"] == 900
    assert trade["receipt_minus_source_ms"] == 900_000 and trade["eligible"] is False


def test_old_side_change_on_a_freshly_received_option_quote_is_not_delay_evidence():
    source = service(("SPY",))
    contract = instrument(option()["symbol"], option(), AT)
    source.register(contract)
    old = ms(AT - timedelta(seconds=61))
    source.feed("Quote", quote_row(contract.streamer_symbol, bidTime=old, askTime=old), AT)
    with pytest.raises(QuoteUnavailable, match="QUOTE_STALE"):
        source.quote(contract.symbol, AT, AGE)
    view = next(c for c in source.inspect(AT)["checks"] if c["symbol"] == contract.symbol)["quote"]
    assert view["age_status"] == "SIDE_CHANGE_AGE_EXCEEDS_QUOTE_POLICY_NOT_DELAY_EVIDENCE"


def test_backward_clock_jump_never_makes_a_trade_fresh():
    source = service(("SPY",), at=AT - timedelta(seconds=30))
    source.feed("Trade", trade_row(), AT)
    with pytest.raises(QuoteUnavailable, match="TRADE_STALE"):
        source.trade("SPY", AT - timedelta(seconds=5), AGE)


def test_yesterdays_print_received_today_stays_yesterdays():
    yesterday = datetime(2026, 10, 2, 19, 59, 59, tzinfo=timezone.utc)  # Friday 15:59:59 ET
    monday = datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc)
    source = service(("SPY",), at=monday)
    source.feed("Trade", trade_row(at=yesterday), monday)
    trade = source.inspect(monday)["checks"][0]["trade"]
    assert trade["source_at"] == yesterday.isoformat() and trade["status"] == "UNAVAILABLE"


def lag_report(trade_age, *, at):
    def captured(client, source, **kwargs):
        source.begin(at + AGE)
        source.ready(at)
        source.feed("Trade", trade_row(at=at - trade_age), at)
        view = source.inspect(at)
        source.disconnect("CAPTURE_COMPLETE")
        # A deliberate end: the terminal view is taken before closing (child 3, D1).
        return dict(stop_reason="CAPTURE_COMPLETE", observations=[view], requests=2, attempts=1,
                    final_attempt=dict(attempt=1, generation=view["generation"], outcome="CAPTURE_COMPLETE",
                                       deliberate_end=True, terminal_view=view,
                                       components={"SPY": {"Quote": False, "Trade": True, "Profile": False}}),
                    connected_after_capture=False)
    client, _ = client_fixture(dict(data=stock()))
    client.clock = lambda: at
    return diagnostic(client, QuoteService(), ["SPY"], capture_fn=captured, clock=lambda: at)


@pytest.mark.parametrize("age,expected", [(timedelta(seconds=1), "AVAILABLE_WITHIN_QUOTE_POLICY"),
                                          (timedelta(minutes=15), "INCONSISTENT_WITH_QUOTE_POLICY")])
def test_diagnostic_lag_evidence_in_session(age, expected):
    report = lag_report(age, at=AT)  # Monday 10:00 ET, regular session
    assert report["lag_evidence"] == expected and report["LIVE_TIMING"] == "OBSERVATIONS_REQUIRE_REVIEW"
    assert "PASS" not in json.dumps(report)


def test_diagnostic_closed_session_ages_are_not_live_evidence():
    report = lag_report(timedelta(seconds=1), at=AT - timedelta(days=1))
    assert report["LIVE_TIMING"] == "NOT_TESTED_MARKET_CLOSED"
    assert report["lag_evidence"] == "CLOSED_SESSION_AGES_ONLY_NOT_LIVE_EVIDENCE"
    assert report["clock"]["tolerance_applied"] == "NONE"


# ---- C3: option selection and distinct missing fields -----------------------------------------------
CHAIN = [(datetime(2026, 10, 5).date(), Decimal(s), f"C{s}", f"P{s}") for s in ("660", "670", "680", "900")] + \
        [(datetime(2026, 10, 6).date(), Decimal(s), f"C{s}b", f"P{s}b") for s in ("670", "675")]


def test_option_pair_prefers_explicit_or_observed_strike_on_the_et_market_date():
    morning = AT  # Monday 10:00 ET
    assert select_pair(CHAIN, now=morning, strike=Decimal("671"))["call"] == "C670"
    near = select_pair(CHAIN, now=morning, reference=Decimal("678.4"))
    assert (near["call"], near["method"], near["expiry"]) == ("C680", "NEAR_OBSERVED_UNDERLYING", "2026-10-05")
    evening = datetime(2026, 10, 6, 1, tzinfo=timezone.utc)  # 21:00 ET Monday: today's expiry has closed
    assert select_pair(CHAIN, now=evening, reference=Decimal("674"))["expiry"] == "2026-10-06"
    median = select_pair(CHAIN, now=morning, median=True)
    assert median["method"] == "MEDIAN_LISTED_NOT_REPRESENTATIVE"
    with pytest.raises(QuoteUnavailable, match="OPTION_STRIKE_UNAVAILABLE"):
        select_pair(CHAIN, now=morning)


def test_diagnostic_selects_the_pair_near_the_observed_underlying_trade():
    chain = {"items": [{"underlying-symbol": "SPY", "option-chain-type": "Standard", "expirations": [
        {"expiration-date": "2026-10-09", "strikes": [
            {"strike-price": s, "call": f"SPY   261009C00{s}000", "put": f"SPY   261009P00{s}000"}
            for s in ("660", "770", "880")]}]}]}

    def request(method, url, headers, body):
        if url.endswith("/oauth/token"):
            return dict(access_token="secret-access", token_type="Bearer", expires_in=900)
        if "/nested" in url:
            return dict(data=chain)
        if "/equity-options/" in url:
            from urllib.parse import unquote
            occ = unquote(url.rsplit("/", 1)[1])
            return dict(data=option(symbol=occ, **{"streamer-symbol": "." + occ.replace(" ", "")}))
        if "/equities/" in url:
            return dict(data=stock())
        return dict(data={"token": "secret-quote", "dxlink-url": "wss://fixture.dxfeed.com/realtime",
                          "expires-at": (AT + timedelta(hours=1)).isoformat(), "level": "api"})
    client = ReadClient(Credentials("secret-client", "secret-refresh"), request=request, clock=lambda: AT)
    server = Server(on_subscribe=[compact("Trade", trade_row(price="771.20"))])
    ticks = [0.0]

    def capture_fn(client, source, **kwargs):
        return capture(client, source, connect=server.factory(ticks), clock=lambda: AT, monotonic=lambda: ticks[0],
                       sleep=lambda s: None, **kwargs)
    report = diagnostic(client, QuoteService(), ["SPY"], option_underlying="SPY", seconds=5,
                        capture_fn=capture_fn, clock=lambda: AT)
    selection = report["options"]["selection"]
    assert selection["method"] == "NEAR_OBSERVED_UNDERLYING" and selection["strike"] == "770"
    added = [m for m in subscriptions(server) if not m.get("reset")]
    assert len(added) == 1 and {a["type"] for a in added[0]["add"]} == {"Quote", "Trade"}
    text = json.dumps(report)
    assert not any(secret in text for secret in ("secret-quote", "secret-access", "secret-client", "secret-refresh"))


@pytest.mark.parametrize("changes,code", [
    (dict(bidPrice="0", bidSize="0"), "QUOTE_NO_BID"),
    (dict(bidPrice="NaN"), "QUOTE_BID_UNAVAILABLE"),
    (dict(askSize="NaN"), "QUOTE_SIZE_UNAVAILABLE"),
    (dict(bidTime=0), "QUOTE_TIME_UNAVAILABLE"),
])
def test_missing_quote_parts_have_distinct_codes(changes, code):
    source = service(("SPY",))
    source.feed("Quote", quote_row(**changes), AT)
    with pytest.raises(QuoteUnavailable, match=code):
        source.quote("SPY", AT, AGE)


def test_missing_trade_size_keeps_price_and_time_but_corrupt_size_rejects():
    source = service(("SPY", "QQQ"))
    assert source.feed("Trade", trade_row(size="NaN"), AT)
    value, _ = source.trade("SPY", AT, AGE)
    assert value.size is None and source.inspect(AT)["checks"][0]["trade"]["size_status"] == "UNAVAILABLE"
    assert not source.feed("Trade", trade_row("QQQ", size=True), AT)
    assert not source.feed("Trade", trade_row("QQQ", size="-1"), AT)
    with pytest.raises(QuoteUnavailable, match="INVALID_NUMBER"):
        source.trade("QQQ", AT, AGE)


# ---- Secret destinations: endpoint allowlist and redirect refusal -----------------------------------
@pytest.mark.parametrize("url,allowed", [
    ("wss://tasty-openapi-ws.dxfeed.com/realtime", True),
    ("wss://a.b.dxfeed.com:443/realtime", True),
    ("ws://tasty-openapi-ws.dxfeed.com/realtime", False),
    ("https://tasty-openapi-ws.dxfeed.com/realtime", False),
    ("wss://dxfeed.com/realtime", False),
    ("wss://dxfeed.com.attacker.example/realtime", False),
    ("wss://evil-dxfeed.com/realtime", False),
    ("wss://user@x.dxfeed.com/realtime", False),
    ("wss://user:pw@x.dxfeed.com/realtime", False),
    ("wss://x.dxfeed.com/realtime?token=secret", False),
    ("wss://x.dxfeed.com/realtime#secret", False),
    ("wss://x.dxfeed.com:8443/realtime", False),
    ("wss://x.dxfeed.com:notaport/realtime", False),
    ("wss://x.dxfeed.com./realtime", False),
    (None, False),
])
def test_quote_token_destination_allowlist(url, allowed):
    assert stream_endpoint_allowed(url) is allowed


@pytest.mark.parametrize("url", ["wss://dxfeed.com.attacker.example/realtime", "wss://user@x.dxfeed.com/r"])
def test_forbidden_destination_never_receives_the_token(url):
    data = dict(token="secret-quote", **{"dxlink-url": url, "expires-at": (AT + AGE).isoformat()})
    client, _ = client_fixture(dict(data=data))
    contacted = []
    result = capture(client, service(("SPY",)), seconds=5, reconnects=2, connect=lambda u: contacted.append(u),
                     clock=lambda: AT, monotonic=iter(range(100)).__next__, sleep=lambda s: None)
    assert contacted == [] and result["stop_reason"] == "QUOTE_ENDPOINT_INVALID"
    with pytest.raises(QuoteUnavailable, match="QUOTE_ENDPOINT_INVALID"):
        connect_ws(url)


def test_redirect_is_refused_and_credentials_go_nowhere_else():
    seen = []

    class Redirecting(HTTPSHandler):
        def https_open(self, request):
            seen.append((request.full_url, request.get_header("Authorization")))
            headers = Message()  # case-insensitive, as a real HTTP response's headers are
            headers["Location"] = "https://attacker.example/steal"
            response = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            return response
    opener = build_opener(_NoRedirect(), Redirecting())
    with pytest.raises(QuoteUnavailable, match="REST_HTTP_302"):
        request_json("GET", "https://api.tastyworks.com/api-quote-tokens",
                     {"Authorization": "Bearer secret-access"}, None, opener=opener)
    assert [url for url, _ in seen] == ["https://api.tastyworks.com/api-quote-tokens"]
