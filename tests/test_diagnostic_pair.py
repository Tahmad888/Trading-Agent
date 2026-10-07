"""Bound shared-pair orchestration; all HTTP and streaming inputs are synthetic."""
from datetime import datetime, timedelta
from decimal import Decimal
import json
from urllib.parse import unquote, urlsplit

import pytest

from desk import diagnostic_pair as dp
from desk import quote_check as qc
from desk import tradier_option_check as tc
from desk.tastytrade_quotes import QuoteService, QuoteUnavailable, canonical
from desk.tastytrade_transport import Credentials, ReadClient
from tests.test_tradier_option_check import CALL, PUT, Clock, Fake, RTH, TOKEN
from tests.test_tastytrade_quotes import option, stock, trade_row


def selected():
    clock = Clock(RTH)
    fake = Fake(clock)
    client = tc.TradierClient(TOKEN, request=fake, clock=clock)
    report = tc.diagnostic(client, ["SPY"], option_underlying="SPY", select_only=True, sleep=lambda _: None)
    assert report["status"] == "OPTION_PAIR_SELECTED" and report["requests"]["count"] == 3
    assert report["rounds"] == []
    assert report["verdicts"]["stock_quote_freshness"]["verdict"] == "NOT_RUN"
    return report["option_pair"]


def provider(pair, *, mutate=None, include_selected=True, missing_symbol=None):
    at = RTH + timedelta(seconds=10)
    calls = []
    rows = {}
    for right, flag in (("call", "C"), ("put", "P")):
        symbol = pair[right]["symbol"]
        row = option(**{"symbol": "SPY   " + symbol[3:], "streamer-symbol": f".SPY261007{flag}780",
                        "expiration-date": pair["expiry"], "strike-price": pair["strike"], "option-type": flag,
                        "option-chain-type": "Standard", "shares-per-contract": pair[right]["contract_size"]})
        if mutate:
            row = mutate(symbol, row)
        rows[symbol] = row

    def request(method, url, headers, payload):
        calls.append(urlsplit(url).path)
        path = unquote(urlsplit(url).path)
        if path == "/oauth/token":
            return dict(access_token="synthetic-access", token_type="Bearer", expires_in=900)
        if path == "/api-quote-tokens":
            return dict(data={"token": "synthetic-quote", "dxlink-url": "wss://fixture.dxfeed.com/realtime",
                              "expires-at": (at + timedelta(hours=1)).isoformat(), "level": "api"})
        if path.startswith("/instruments/equities/"):
            return dict(data=stock(path.rsplit("/", 1)[-1]))
        if path == "/option-chains/SPY/nested":
            strikes = [{"strike-price": "781", "call": "SPY   261007C00781000", "put": "SPY   261007P00781000"}]
            if include_selected:
                strikes.append({"strike-price": pair["strike"], "call": rows[CALL]["symbol"], "put": rows[PUT]["symbol"]})
            return dict(data={"items": [{"underlying-symbol": "SPY", "option-chain-type": "Standard",
                        "expirations": [{"expiration-date": pair["expiry"], "strikes": strikes}]}]})
        if path.startswith("/instruments/equity-options/"):
            symbol = canonical(path.rsplit("/", 1)[-1], "Equity Option")
            if symbol == missing_symbol:
                raise QuoteUnavailable("REST_HTTP_404")
            return dict(data=rows[symbol])
        raise AssertionError(path)

    client = ReadClient(Credentials("synthetic-client", "synthetic-refresh"), request=request,
                        clock=lambda: at, max_requests=12)
    service = QuoteService()
    return client, service, calls, at


def capture(client, service, **options):
    # Also exercises request-budget inclusion of authentication and quote-token setup.
    at = client.clock()
    token = client.stream_token()
    service.begin(token.expires_at)
    service.ready(at)
    for identity in list(service.identities.values()):
        service.feed("Trade", trade_row(identity.streamer_symbol, at=at, price="780.60"), at)
    view = service.inspect(at)
    return dict(stop_reason="CAPTURE_COMPLETE", observations=[view], requests=client.requests,
                final_attempt=dict(generation=service.generation, terminal_view=view, attempt=1,
                                   components={n: {"Trade": True} for n in service.identities}))


def test_shared_pair_stays_bound_when_underlying_moves_across_strike_midpoint():
    pair = selected()
    # A fresh Tradier quote and the tastytrade trade both now favor 781 if re-selected.
    clock = Clock(RTH + timedelta(seconds=10))
    def changed(n, rows):
        return [dict(r, bid=780.55, ask=780.65, last=780.60) if r["symbol"] == "SPY" else r for r in rows]
    client = tc.TradierClient(TOKEN, request=Fake(clock, quote_hook=changed), clock=clock)
    tradier = tc.diagnostic(client, ["SPY", "QQQ", "NVDA"], option_pair=pair, rounds=2, sleep=lambda _: None)
    tt, service, calls, at = provider(pair)
    tastytrade = qc.diagnostic(tt, service, ["SPY", "QQQ", "NVDA"], option_pair=pair,
                              capture_fn=capture, clock=lambda: at)
    for report in (tradier, tastytrade):
        assert report["option_pair_comparison"]["selection_id"] == pair["selection_id"]
        assert report["option_pair_comparison"]["status"] == "MATCHED_REPORTED_FIELDS"
    assert tradier["option_selection"]["call"] == CALL
    assert canonical(tastytrade["options"]["selection"]["call"], "Equity Option") == CALL
    assert {n for n in service.identities if "261007" in n} == {CALL, PUT}
    assert client.requests == 2  # no repeated seed/chain selection
    assert tt.requests == len(calls) == 8 <= 12


def test_missing_selected_pair_never_substitutes_neighbor_and_preserves_stocks():
    pair = selected()
    client, service, calls, at = provider(pair, include_selected=False)
    report = qc.diagnostic(client, service, ["SPY"], option_pair=pair, capture_fn=capture, clock=lambda: at)
    assert report["options"]["selection"]["reason"] == "OPTION_PAIR_NOT_LISTED"
    assert report["option_pair_comparison"]["status"] == "FAIL"
    assert list(service.identities) == ["SPY"] and report["stocks"]["checks"]
    assert not any("equity-options" in path for path in calls)


def test_missing_counterpart_isolates_it_and_retains_healthy_peer():
    pair = selected()
    client, service, calls, at = provider(pair, missing_symbol=PUT)
    report = qc.diagnostic(client, service, ["SPY"], option_pair=pair, capture_fn=capture, clock=lambda: at)
    assert report["identity_issues"]["SPY   " + PUT[3:]] == "REST_HTTP_404"
    assert report["option_pair_comparison"]["status"] == "FAIL"
    assert set(service.identities) == {"SPY", CALL}


@pytest.mark.parametrize("changes", [{"strike-price": "781"}, {"expiration-date": "2026-10-08"},
                                     {"option-type": "P"}, {"shares-per-contract": 10},
                                     {"option-chain-type": "Adjusted"}])
def test_conflicting_tastytrade_counterpart_cannot_match(changes):
    pair = selected()
    def wrong(symbol, row):
        return dict(row, **changes) if symbol == CALL else row
    client, service, calls, at = provider(pair, mutate=wrong)
    report = qc.diagnostic(client, service, ["SPY"], option_pair=pair, capture_fn=capture, clock=lambda: at)
    assert report["option_pair_comparison"]["status"] == "FAIL"
    assert CALL not in service.identities and PUT in service.identities


def test_unknown_counterpart_size_is_partial_without_guessing():
    pair = selected()
    def missing(symbol, row):
        return {k: v for k, v in row.items() if k != "shares-per-contract"}
    client, service, calls, at = provider(pair, mutate=missing)
    report = qc.diagnostic(client, service, ["SPY"], option_pair=pair, capture_fn=capture, clock=lambda: at)
    assert report["option_pair_comparison"]["status"] == "PARTIAL"
    assert set(service.identities) == {"SPY", CALL, PUT}
    assert report["option_pair_comparison"]["by_symbol"][CALL]["observed"] is None


def test_missing_chain_classification_is_partial_without_assuming_standard():
    pair = selected()
    def missing(symbol, row):
        return {k: v for k, v in row.items() if k != "option-chain-type"}
    client, service, calls, at = provider(pair, mutate=missing)
    report = qc.diagnostic(client, service, ["SPY"], option_pair=pair, capture_fn=capture, clock=lambda: at)
    assert report["option_pair_comparison"]["status"] == "PARTIAL"
    assert report["option_pair_comparison"]["by_symbol"][CALL]["reason"] == "CHAIN_TYPE_UNAVAILABLE"


@pytest.mark.parametrize("changes", [{"strike": True}, {"strike": 780}, {"strike": "NaN"},
                                     {"expiry": "2026-02-30"}, {"expiry": "2026-10-07T00:00:00"},
                                     {"call": None}])
def test_malformed_pair_is_a_structured_failure(changes):
    with pytest.raises(QuoteUnavailable):
        dp.validate_pair(dict(selected(), **changes), environment="production", now=RTH + timedelta(seconds=10))


@pytest.mark.parametrize("bad", [{"schema": "wrong"}, [], {"call": None},
                                 {"selected_at": "2026-02-30T12:00:00Z"}])
def test_tastytrade_invalid_pair_never_crashes_reporting_or_calls_a_provider(bad):
    pair = selected()
    client, service, calls, at = provider(pair)
    data = dict(pair, **bad) if isinstance(bad, dict) else bad
    report = qc.diagnostic(client, service, ["SPY"], option_pair=data, capture_fn=capture, clock=lambda: at)
    assert report["status"] == "UNAVAILABLE" and report["option_pair_comparison"]["status"] == "FAIL"
    assert calls == [] and not service.identities


def test_expired_selection_is_refused_even_on_the_same_et_day():
    pair = selected()
    at = RTH.replace(hour=21)  # after regular close on this expiry date
    with pytest.raises(QuoteUnavailable, match="OPTION_PAIR_EXPIRED"):
        dp.validate_pair(pair, environment="production", now=at)


def test_one_metadata_source_is_disclosed_and_never_called_two_source_confirmation():
    pair = selected()
    pair["call"]["contract_size"] = pair["put"]["contract_size"] = None
    pair = dp.validate_pair(pair, environment="production", now=RTH + timedelta(seconds=10))
    clock = Clock(RTH + timedelta(seconds=10))
    report = tc.diagnostic(tc.TradierClient(TOKEN, request=Fake(clock), clock=clock), ["SPY"],
                           option_pair=pair, rounds=1, sleep=lambda _: None)
    observed = report["rounds"][-1]["observations"][CALL]
    assert observed["multiplier"]["metadata_sources"] == {
        "quote.contract_size": 100, "chain_or_selection.contract_size": None}
    assert observed["multiplier"]["value"] == 100  # unchanged single-source diagnostic exposure behavior
    assert observed["pair_terms"]["status"] == "INCOMPLETE"


def test_tradier_quote_terms_are_compared_with_the_explicit_selection():
    pair = selected()
    clock = Clock(RTH + timedelta(seconds=10))
    def changed(n, rows):
        return [dict(r, contract_size=10) if r["symbol"] == CALL else r for r in rows]
    client = tc.TradierClient(TOKEN, request=Fake(clock, quote_hook=changed), clock=clock)
    report = tc.diagnostic(client, ["SPY"], option_pair=pair, rounds=2, sleep=lambda _: None)
    assert report["option_pair_comparison"]["status"] == "FAIL"
    assert report["status"] == "PARTIAL_OBSERVATIONS"
    assert report["option_pair_comparison"]["by_symbol"][CALL]["observed"] == 10


@pytest.mark.parametrize("change,reason", [({"environment": "sandbox"}, "OPTION_PAIR_ENVIRONMENT_MISMATCH"),
                                          ({"strike": "781"}, "OPTION_PAIR_IDENTITY_MISMATCH"),
                                          ({"selected_at": (RTH - timedelta(days=1)).isoformat()}, "OPTION_PAIR_NOT_CURRENT_SESSION"),
                                          ({"selected_at": (RTH + timedelta(days=1)).isoformat()}, "OPTION_PAIR_SELECTION_IN_FUTURE")])
def test_invalid_selection_is_refused_before_provider_calls(change, reason):
    pair = dict(selected(), **change)
    clock = Clock(RTH + timedelta(seconds=10))
    fake = Fake(clock)
    report = tc.diagnostic(tc.TradierClient(TOKEN, request=fake, clock=clock), ["SPY"], option_pair=pair)
    assert report["reason"] == reason and fake.calls == []


def test_selection_file_round_trip_and_bad_file_are_structured(tmp_path):
    pair = selected()
    file = tmp_path / "pair.json"
    file.write_text(json.dumps({"option_pair": pair}))
    loaded = dp.load_pair(file, environment="production", now=RTH + timedelta(seconds=10))
    assert loaded["selection_id"] == pair["selection_id"]
    file.write_text("[]")
    with pytest.raises(QuoteUnavailable, match="OPTION_PAIR_FILE_INVALID"):
        dp.load_pair(file, environment="production", now=RTH + timedelta(seconds=10))


def test_non_100_size_is_bound_from_metadata_not_a_universal_default():
    pair = selected()
    for right in ("call", "put"):
        pair[right]["contract_size"] = 10
    pair = dp.validate_pair(pair, environment="production", now=RTH + timedelta(seconds=10))
    client, service, calls, at = provider(pair)
    report = qc.diagnostic(client, service, ["SPY"], option_pair=pair, capture_fn=capture, clock=lambda: at)
    assert report["option_pair_comparison"]["status"] == "MATCHED_REPORTED_FIELDS"
    assert report["option_pair_comparison"]["by_symbol"][CALL]["observed"] == 10
