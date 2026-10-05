"""Synthetic DXLink fixtures: zero provider calls, no live entitlement claim."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import ssl

import pytest

from desk.tastytrade_quotes import (FIELDS, FeedDecoder, QuoteService, QuoteUnavailable,
                                   instrument, number)
from desk.tastytrade_transport import (Credentials, ReadClient, Session, StreamToken,
                                      capture, json_read, tls_context)

AT = datetime(2026, 10, 5, 14, tzinfo=timezone.utc)
AGE = timedelta(seconds=60)


def stock(symbol="SPY", **changes):
    row = {"symbol": symbol, "streamer-symbol": symbol, "instrument-type": "Equity",
           "active": True, "cusip": "synthetic:"+symbol}
    row.update(changes)
    return row


def option(**changes):
    row = {"symbol": "SPY   261009C00770000", "streamer-symbol": ".SPY261009C770",
           "instrument-type": "Equity Option", "active": True, "underlying-symbol": "SPY",
           "expiration-date": "2026-10-09", "strike-price": "770", "option-type": "C"}
    row.update(changes)
    return row


def ms(at):
    return int(at.timestamp()*1000)


def trade_row(symbol="SPY", at=AT, **changes):
    row = dict(eventType="Trade", eventSymbol=symbol, price="770.1250", size="100", time=ms(at))
    row.update(changes)
    return row


def quote_row(symbol="SPY", at=AT, **changes):
    row = dict(eventType="Quote", eventSymbol=symbol, bidPrice="770.12", askPrice="770.13",
               bidSize="200", askSize="300", bidTime=ms(at), askTime=ms(at-timedelta(seconds=1)))
    row.update(changes)
    return row


def service(symbols=("SPY", "QQQ"), at=AT):
    source = QuoteService()
    for symbol in symbols:
        source.register(instrument(symbol, stock(symbol), at-timedelta(seconds=1)))
    source.begin(at+timedelta(hours=1))
    source.ready(at)
    return source


@pytest.mark.parametrize("value", [True, False, "NaN", "Infinity", "-1", float("nan"), None, [], {}])
def test_numeric_refusal(value):
    with pytest.raises(QuoteUnavailable):
        number(value)


def test_negotiated_order_multiple_rows_and_decimal_precision():
    source = service()
    decoder = FeedDecoder(source)
    fields = {kind: list(reversed(names)) for kind, names in FIELDS.items()}
    decoder.configure(dict(dataFormat="COMPACT", eventFields=fields))
    values = []
    for symbol in ("SPY", "QQQ"):
        row = trade_row(symbol, price="770.123456789123456789")
        values.extend(row[n] for n in fields["Trade"])
    decoder.data(dict(data=["Trade", values]), AT)
    for symbol in ("SPY", "QQQ"):
        value, proof = source.trade(symbol, AT, AGE)
        assert value.price == Decimal("770.123456789123456789")
        assert proof.symbol == symbol and proof.source == "tastytrade-dxlink"
    assert json_read('{"price":770.123456789123456789}')["price"] == Decimal("770.123456789123456789")


@pytest.mark.parametrize("bad", [dict(dataFormat="FULL", eventFields=FIELDS),
                                  dict(dataFormat="COMPACT", eventFields={"Trade": list(FIELDS["Trade"])}),
                                  dict(dataFormat="COMPACT", eventFields={k: list(v)+[v[0]] for k,v in FIELDS.items()})])
def test_schema_refusal(bad):
    with pytest.raises(QuoteUnavailable):
        FeedDecoder(service()).configure(bad)


@pytest.mark.parametrize("timestamp", [0, None, True, "123", ms(AT+timedelta(seconds=1))])
def test_bad_source_time_is_not_receipt_time(timestamp):
    source = service()
    assert not source.feed("Trade", trade_row(time=timestamp), AT)
    with pytest.raises(QuoteUnavailable):
        source.trade("SPY", AT, AGE)


def test_receipt_heartbeat_zero_event_time_and_out_of_order_do_not_freshen():
    source = service()
    source.feed("Trade", trade_row(at=AT-timedelta(seconds=61), eventTime=0), AT)
    with pytest.raises(QuoteUnavailable, match="TRADE_STALE"):
        source.trade("SPY", AT, AGE)
    source.feed("Trade", trade_row(eventTime=0), AT)
    assert not source.feed("Trade", trade_row(at=AT-timedelta(seconds=20), price=1), AT)
    assert source.trade("SPY", AT, AGE)[0].price == Decimal("770.1250")
    with pytest.raises(QuoteUnavailable, match="TRADE_STALE"):
        source.trade("SPY", AT+timedelta(seconds=61), AGE)


def test_bid_ask_age_is_separate_from_trade():
    source = service()
    source.feed("Trade", trade_row(), AT)
    source.feed("Quote", quote_row(askTime=ms(AT-timedelta(seconds=61))), AT)
    assert source.trade("SPY", AT, AGE)[0].traded_at == AT
    with pytest.raises(QuoteUnavailable, match="QUOTE_STALE"):
        source.quote("SPY", AT, AGE)
    source.feed("Quote", quote_row(), AT)
    assert source.quote("SPY", AT, AGE).ask_at == AT-timedelta(seconds=1)


@pytest.mark.parametrize("changes,reason", [(dict(bidPrice="771"), "QUOTE_CROSSED"),
                                            (dict(bidPrice="770.13"), "QUOTE_LOCKED"),
                                            (dict(bidSize=0), "QUOTE_ZERO_SIZE"),
                                            (dict(askSize=0), "QUOTE_ZERO_SIZE")])
def test_non_executable_quote(changes, reason):
    source = service()
    source.feed("Quote", quote_row(**changes), AT)
    with pytest.raises(QuoteUnavailable, match=reason):
        source.quote("SPY", AT, AGE)


def test_one_bad_row_failed_refresh_and_peer_recovery():
    source = service()
    for symbol in ("SPY", "QQQ"):
        source.feed("Trade", trade_row(symbol), AT)
    source.feed("Trade", trade_row(price=True), AT)
    with pytest.raises(QuoteUnavailable):
        source.trade("SPY", AT, AGE)
    assert source.trade("QQQ", AT, AGE)[0].identity.symbol == "QQQ"
    source.feed("Trade", trade_row(), AT)
    source.invalidate("SPY")
    source.register(instrument("SPY", stock(), AT))
    with pytest.raises(QuoteUnavailable, match="TRADE_UNAVAILABLE"):
        source.trade("SPY", AT, AGE)
    source.feed("Trade", trade_row(), AT)
    assert source.trade("SPY", AT, AGE)[0].price > 0


def test_restart_disconnect_expiry_reconnect_and_generation():
    source = service()
    source.feed("Trade", trade_row(), AT)
    old = source.trade("SPY", AT, AGE)[1]
    with pytest.raises(QuoteUnavailable, match="QUOTE_TOKEN_EXPIRED"):
        source.trade("SPY", AT+timedelta(hours=1), AGE)
    source.disconnect()
    with pytest.raises(QuoteUnavailable, match="DISCONNECTED"):
        source.trade("SPY", AT, AGE)
    source.begin(AT+timedelta(hours=1))
    source.ready(AT)
    with pytest.raises(QuoteUnavailable, match="TRADE_UNAVAILABLE"):
        source.trade("SPY", AT, AGE)
    source.feed("Trade", trade_row(), AT)
    assert source.trade("SPY", AT, AGE)[1].generation != old.generation
    with pytest.raises(QuoteUnavailable, match="NOT_CONNECTED"):
        QuoteService().trade("SPY", AT, AGE)


def test_identity_ambiguity_change_and_option_explicit_fields():
    source = service()
    source.feed("Trade", trade_row(), AT)
    old = source.trade("SPY", AT, AGE)[1]
    source.register(instrument("SPY", stock(cusip="new"), AT))
    with pytest.raises(QuoteUnavailable, match="TRADE_UNAVAILABLE"):
        source.trade("SPY", AT, AGE)
    source.feed("Trade", trade_row(), AT)
    assert source.trade("SPY", AT, AGE)[1].identity_digest != old.identity_digest
    with pytest.raises(QuoteUnavailable, match="IDENTITY_AMBIGUOUS"):
        source.register(instrument("BAD", stock("BAD", **{"streamer-symbol": "QQQ"}), AT))
    with pytest.raises(QuoteUnavailable):
        source.trade("QQQ", AT, AGE)
    contract = instrument(option()["symbol"], option(), AT)
    source.register(contract)
    source.feed("Trade", trade_row(contract.streamer_symbol, price="1.2345"), AT)
    assert source.trade(contract.symbol, AT, AGE)[0].price == Decimal("1.2345")
    with pytest.raises(QuoteUnavailable, match="OPTION_IDENTITY_MISMATCH"):
        instrument(option()["symbol"], option(**{"strike-price": "771"}), AT)
    assert instrument("BRK.B", stock("BRK/B"), AT).symbol == "BRK.B"


def client_fixture(reply=None):
    calls = []
    def request(method, url, headers, body):
        calls.append((method, url, headers, body))
        if url.endswith("/oauth/token"):
            return dict(access_token="secret-access", token_type="Bearer", expires_in=900)
        if reply is not None:
            return reply
        return dict(data={"token": "secret-quote", "dxlink-url": "wss://fixture.dxfeed.com/realtime",
                          "expires-at": (AT+timedelta(hours=1)).isoformat(), "level": "api"})
    return ReadClient(Credentials("secret-client", "secret-refresh"), request=request, clock=lambda: AT), calls


def test_read_grant_no_client_id_secret_repr_allowlist_tls():
    client, calls = client_fixture()
    token = client.stream_token()
    assert set(calls[0][3]) == {"grant_type", "client_secret", "refresh_token", "scope"}
    assert calls[0][3]["scope"] == "read" and all(c[2]["User-Agent"] for c in calls)
    assert "secret" not in repr(client.credentials)+repr(token)
    with pytest.raises(QuoteUnavailable, match="REST_ROUTE_REFUSED"):
        client._call("POST", "/accounts/foo/orders")
    context = tls_context()
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("change", [{"expires-at": None}, {"expires-at": AT.isoformat()},
                                   {"dxlink-url": "ws://feed.example.test"},
                                   {"dxlink-url": "wss://fixture.dxfeed.com:secret-value"},
                                   {"dxlink-url": "wss://[invalid"},
                                   {"dxlink-url": "wss://feed.example.test/?token=secret"}])
def test_bad_quote_token_response(change):
    data = dict(token="secret", **{"dxlink-url": "wss://fixture.dxfeed.com", "expires-at": (AT+AGE).isoformat()})
    data.update(change)
    client, _ = client_fixture(dict(data=data))
    with pytest.raises(QuoteUnavailable):
        client.stream_token()


def handshake(session):
    session.receive(dict(type="SETUP", channel=0, keepaliveTimeout=60), AT)
    auth = session.receive(dict(type="AUTH_STATE", channel=0, state="UNAUTHORIZED"), AT)
    assert auth[0]["token"]
    session.receive(dict(type="AUTH_STATE", channel=0, state="AUTHORIZED"), AT)
    session.receive(dict(type="CHANNEL_OPENED", channel=3), AT)
    session.receive(dict(type="FEED_CONFIG", channel=3, dataFormat="COMPACT"), AT)
    return session.receive(dict(type="FEED_CONFIG", channel=3, dataFormat="COMPACT",
                                eventFields={k:list(v) for k,v in FIELDS.items()}), AT)


def test_handshake_subscribes_actual_streamer_and_keepalive_is_not_trade():
    source = service()
    source.disconnect()
    session = Session(source, StreamToken("wss://fixture", "secret", AT+AGE, "api"), AT)
    assert not source.connected
    subscriptions = handshake(session)[0]["add"]
    assert subscriptions == [{"type": k, "symbol": v.streamer_symbol} for v in source.identities.values() for k in FIELDS]
    session.receive(dict(type="KEEPALIVE", channel=0), AT)
    with pytest.raises(QuoteUnavailable, match="TRADE_UNAVAILABLE"):
        source.trade("SPY", AT, AGE)
    with pytest.raises(QuoteUnavailable, match="DXLINK_DENIED_OR_CLOSED"):
        session.receive(dict(type="ERROR", channel=0, message="secret-client"), AT)


def test_capture_bounded_reconnect_no_cached_revival_and_no_secret_artifact():
    client, calls = client_fixture()
    source = service()
    ticks = [0.0]
    def mono():
        return ticks[0]
    phases = [dict(type="SETUP", channel=0, keepaliveTimeout=60),
              dict(type="AUTH_STATE", channel=0, state="UNAUTHORIZED"),
              dict(type="AUTH_STATE", channel=0, state="AUTHORIZED"),
              dict(type="CHANNEL_OPENED", channel=3),
              dict(type="FEED_CONFIG", channel=3, dataFormat="COMPACT", eventFields={k:list(v) for k,v in FIELDS.items()}),
              dict(type="FEED_DATA", channel=3, data=["Trade", list(trade_row().values())])]
    connects = []
    class Socket:
        def __enter__(self):
            connects.append(1)
            self.rows = list(phases)
            return self
        def __exit__(self, *args):
            pass
        def send(self, raw):
            pass
        def recv(self, timeout):
            ticks[0] += .1
            if self.rows:
                return json.dumps(self.rows.pop(0))
            raise OSError("secret-quote")
    seen = []
    result = capture(client, source, seconds=3, reconnects=1, connect=lambda _:Socket(),
                     clock=lambda:AT, monotonic=mono, sleep=lambda s:ticks.__setitem__(0,ticks[0]+s),
                     on_observation=lambda view:seen.append(view))
    assert len(connects) == 2 and result["attempts"] == 2 and not source.connected
    assert result["stop_reason"] == "DXLINK_TRANSPORT_FAILURE"
    generations = [v["checks"][0]["generation"] for v in seen]
    assert len(set(generations)) == 2
    assert all(secret not in json.dumps(result) for secret in ("secret-quote", "secret-access", "secret-client"))


def test_no_candle_or_volume_interface_or_dependency():
    assert set(FIELDS) == {"Quote", "Trade"}
    assert not hasattr(QuoteService(), "bars") and not hasattr(QuoteService(), "decision_volume")
    with pytest.raises(QuoteUnavailable):
        json_read('{"price":NaN}')


def test_trusted_callable_is_last_trade_not_midpoint_or_request_price():
    source = service()
    source.feed("Trade", trade_row(price="770.11"), AT)
    source.feed("Quote", quote_row(), AT)
    assert source.trade_source(lambda:AT, AGE)("SPY") == ("SPY", 770.11, AT)
    assert source.quote("SPY", AT, AGE).provenance.environment == "production"
