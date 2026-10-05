"""Live-run package 1: share-class option-chain lookup (offline fixtures, no provider)."""
from decimal import Decimal

import pytest

from desk.tastytrade_quotes import QuoteService, QuoteUnavailable, equity_provider_symbol
from tests.test_tastytrade_quotes import client_fixture, stock

BRKB_CALL, BRKB_PUT = "BRKB  261009C00500000", "BRKB  261009P00500000"


def chain(underlying, *, kind="Standard", call=BRKB_CALL, put=BRKB_PUT, strike="500", expiry="2026-10-09"):
    return {"underlying-symbol": underlying, "option-chain-type": kind, "expirations": [
        {"expiration-date": expiry, "strikes": [{"strike-price": strike, "call": call, "put": put}]}]}


def lookup(requested, *items):
    client, calls = client_fixture(dict(data={"items": list(items)}))
    try:
        result = client.chain_options(requested)
    except QuoteUnavailable as exc:
        result = str(exc)
    paths = [url.split("/api.tastyworks.com")[-1].split(".com", 1)[-1] for _, url, _, _ in calls
             if "option-chains" in url]
    return result, paths


def test_ordinary_symbol_control():
    rows, paths = lookup("SPY", chain("SPY", call="SPY   261009C00770000", put="SPY   261009P00770000", strike="770"))
    assert paths == ["/option-chains/SPY/nested"]
    assert rows == [(rows[0][0], Decimal("770"), "SPY   261009C00770000", "SPY   261009P00770000")]


@pytest.mark.parametrize("requested", ["BRK.B", "BRK/B", "brk.b"])
def test_share_class_requests_the_encoded_provider_symbol_and_accepts_its_chain(requested):
    rows, paths = lookup(requested, chain("BRK/B"))
    assert paths == ["/option-chains/BRK%2FB/nested"]
    (day, strike, call, put), = rows
    assert (str(day), strike, call, put) == ("2026-10-09", Decimal("500"), BRKB_CALL, BRKB_PUT)  # OCC verbatim


@pytest.mark.parametrize("underlying", ["BRK/A", "BRK.A", "SPY", "BRKB", "BRK/B/X", None, 7, ""])
def test_another_class_underlying_or_malformed_reply_cannot_satisfy_brkb(underlying):
    assert lookup("BRK.B", chain(underlying))[0] == "OPTION_CHAIN_UNAVAILABLE"


def test_ambiguous_identities_refuse():
    assert lookup("BRK.B", chain("BRK/B"), chain("BRK/B", call="BRKB  261009C00500001"))[0] == "OPTION_CHAIN_AMBIGUOUS"
    rows, _ = lookup("BRK.B", chain("BRK/B"), chain("BRK/B"))  # identical duplicates merge, as before
    assert len(rows) == 1


def test_non_standard_chains_are_excluded_and_peers_unaffected():
    assert lookup("BRK.B", chain("BRK/B", kind="Non-standard"))[0] == "OPTION_CHAIN_UNAVAILABLE"
    rows, _ = lookup("BRK.B", chain("BRK/B", kind="Non-standard", call="BRKB1 261009C00500000"), chain("BRK/B"),
                     chain("BRK/A", call="BRKA  261009C00500000"))
    assert rows == [(rows[0][0], Decimal("500"), BRKB_CALL, BRKB_PUT)]


@pytest.mark.parametrize("strike,expiry", [("abc", "2026-10-09"), ("500", "not-a-date"), ("-5", "2026-10-09")])
def test_malformed_strikes_or_expiries_stay_unavailable(strike, expiry):
    assert lookup("BRK.B", chain("BRK/B", strike=strike, expiry=expiry))[0] == "OPTION_CHAIN_UNAVAILABLE"


@pytest.mark.parametrize("code", ["REST_HTTP_401", "REST_HTTP_403", "REST_HTTP_429", "REST_REQUEST_BUDGET"])
def test_request_stops_are_unchanged_and_not_retried(code):
    client, calls = client_fixture()
    seen = []

    def stop(method, path, *args, **kwargs):
        seen.append(path)
        raise QuoteUnavailable(code)
    client._call = stop
    with pytest.raises(QuoteUnavailable, match=code):
        client.chain_options("BRK.B")
    assert seen == ["/option-chains/BRK%2FB/nested"]


def test_equity_resolve_uses_the_same_conversion():
    client, calls = client_fixture(dict(data=stock("BRK.B")))
    try:
        client.resolve("BRK.B", "Equity", QuoteService())
    except QuoteUnavailable:
        pass  # the fixture row's own validation is not under test here
    assert any(url.endswith("/instruments/equities/BRK%2FB") for _, url, _, _ in calls)
    assert equity_provider_symbol("BRK.B") == equity_provider_symbol("brk/b") == "BRK/B"
    assert equity_provider_symbol("SPY") == "SPY"
    with pytest.raises(QuoteUnavailable):
        equity_provider_symbol("SPY   261009C00770000")  # never applied to option symbols
