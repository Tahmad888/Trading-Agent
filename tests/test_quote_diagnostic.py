"""Offline REST/CLI evidence limits; no provider calls or real credentials."""
from datetime import timedelta
from decimal import Decimal
import json

import pytest

from desk.quote_check import diagnostic, main
from desk.tastytrade_quotes import QuoteService, QuoteUnavailable, instrument
from desk.tastytrade_transport import Session, StreamToken
from tests.test_tastytrade_quotes import (AGE, AT, client_fixture, handshake, option,
                                         quote_row, service, stock, trade_row)


def test_bad_metadata_isolation_and_http_stop_without_secret_output():
    client, calls = client_fixture(dict(data=stock("QQQ")))
    source = service()
    source.feed("Trade", trade_row(), AT)
    source.feed("Trade", trade_row("QQQ"), AT)
    with pytest.raises(QuoteUnavailable, match="IDENTITY_MISMATCH"):
        client.resolve("SPY", "Equity", source)
    assert source.trade("QQQ", AT, AGE)
    with pytest.raises(QuoteUnavailable):
        source.trade("SPY", AT, AGE)
    def failure(*args):
        raise RuntimeError("secret-refresh")
    client.request = failure
    result = diagnostic(client, source, ["SPY"], clock=lambda:AT)
    assert "secret-refresh" not in json.dumps(result)
    assert result["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("code", ["REST_HTTP_401", "REST_HTTP_403", "REST_HTTP_429", "REST_REQUEST_BUDGET"])
def test_denial_or_budget_stops_no_retries(code):
    client, calls = client_fixture()
    source = QuoteService()
    seen = []
    def resolve(*args):
        seen.append(args[0])
        raise QuoteUnavailable(code)
    client.resolve = resolve
    result = diagnostic(client, source, ["SPY", "QQQ"], clock=lambda:AT)
    assert seen == ["SPY"] and result["status"] == "UNAVAILABLE"


def test_actual_option_chain_pair_and_instrument_validation():
    expiry = dict(**{"expiration-date":"2026-10-09"}, strikes=[{
        "strike-price":"770", "call":"SPY   261009C00770000", "put":"SPY   261009P00770000"}])
    chain = {"items":[{"underlying-symbol":"SPY", "option-chain-type":"Standard", "expirations":[expiry]}]}
    client, _ = client_fixture(dict(data=chain))
    assert client.chain_pair("SPY", strike=Decimal("770")) == (expiry["strikes"][0]["call"],expiry["strikes"][0]["put"])
    source = QuoteService()
    client.request = lambda *args: dict(data=option())
    contract = client.resolve("SPY   261009C00770000", "Equity Option", source)
    assert contract.streamer_symbol == ".SPY261009C770"
    assert not hasattr(contract, "deliverable_cash") and not hasattr(contract, "open_interest")


def test_closed_market_never_claims_live_even_with_fresh_fixture_events():
    closed = AT-timedelta(days=1)  # Sunday, all timestamps intentionally synthetic.
    client, _ = client_fixture(dict(data=stock()))
    client.clock = lambda:closed
    def captured(client, source, **kwargs):
        source.begin(closed+AGE)
        source.ready(closed)
        source.feed("Trade", trade_row(at=closed), closed)
        source.feed("Quote", quote_row(at=closed), closed)
        view = source.inspect(closed)
        source.disconnect("CAPTURE_COMPLETE")
        return dict(stop_reason="CAPTURE_COMPLETE", observations=[view], requests=2,
                    final_attempt=dict(attempt=1, generation=view["generation"], outcome="CAPTURE_COMPLETE"),
                    attempts=1, connected_after_capture=False)
    result = diagnostic(client, QuoteService(), ["SPY"], capture_fn=captured, clock=lambda:closed)
    assert result["LIVE_TIMING"] == "NOT_TESTED_MARKET_CLOSED"
    assert result["stocks"]["timing_status"] == "NOT_TESTED_MARKET_CLOSED"
    assert result["options"]["timing_status"] == "NOT_TESTED_MARKET_CLOSED"
    assert result["source_roles"]["decision_volume"] == "Alpaca SIP unchanged"


def test_protocol_denial_invalidates_immediately_before_socket_teardown():
    source = service()
    session = Session(source, StreamToken("wss://fixture", "secret", AT+AGE, "api"), AT)
    handshake(session)
    source.feed("Trade", trade_row(), AT)
    with pytest.raises(QuoteUnavailable):
        session.receive(dict(type="AUTH_STATE", channel=0, state="UNAUTHORIZED"), AT)
    assert not source.connected
    with pytest.raises(QuoteUnavailable):
        source.trade("SPY", AT, AGE)


def test_unknown_or_bad_wire_row_does_not_discard_peers():
    source = service()
    assert not source.feed("Trade", trade_row(eventSymbol=[]), AT)
    assert source.feed("Trade", trade_row("QQQ"), AT)
    assert source.trade("QQQ", AT, AGE)


def test_environment_mismatch_and_metadata_expiry_refuse():
    client, _ = client_fixture()
    with pytest.raises(QuoteUnavailable, match="QUOTE_ENVIRONMENT_MISMATCH"):
        client.resolve("SPY", "Equity", QuoteService(environment="sandbox"))
    source = service()
    source.feed("Trade", trade_row(), AT)
    source.identities["SPY"] = instrument("SPY", stock(), AT-timedelta(days=2))
    with pytest.raises(QuoteUnavailable, match="IDENTITY_STALE"):
        source.trade("SPY", AT, AGE)


def test_cli_missing_credentials_is_safe_and_makes_no_call(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TASTYTRADE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("TASTYTRADE_REFRESH_TOKEN", raising=False)
    output = tmp_path/"quote.json"
    status = main(["--symbols","SPY","--environment","production","--output",str(output)])
    report = json.loads(output.read_text())
    assert status == 1 and report["requests"] == 0 and report["reason"] == "CREDENTIALS_MISSING"
    assert "CREDENTIALS_MISSING" in capsys.readouterr().out
