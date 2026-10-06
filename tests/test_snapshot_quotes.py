"""Snapshot contract regressions; synthetic data, zero provider calls."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlsplit
import json
from pathlib import Path
import re

import pytest

from desk.snapshot_quote_check import diagnostic, main
from desk.snapshot_quotes import normalize, normalize_batch, update_time
from desk.tastytrade_quotes import QuoteService, QuoteUnavailable, instrument
from desk.tastytrade_transport import Credentials, ReadClient

AT = datetime(2026, 10, 5, 14, tzinfo=timezone.utc)
AGE = timedelta(seconds=60)
CALL, PUT = "SPY   261009C00770000", "SPY   261009P00770000"


def test_snapshot_producer_has_only_the_diagnostic_consumer():
    root = Path(__file__).resolve().parents[1] / "src/desk"
    importer = re.compile(r"^\s*(from desk(\.snapshot_quotes| import snapshot_quotes)|import desk\.snapshot_quotes)", re.M)
    users = sorted(p.name for p in root.rglob("*.py") if importer.search(p.read_text()))
    assert users == ["snapshot_quote_check.py"]


def metadata(symbol="SPY", kind="Equity"):
    if kind == "Equity":
        return {"symbol": symbol.replace(".", "/"), "instrument-type": kind, "streamer-symbol": symbol,
                "cusip": "fixture:" + symbol, "active": True, "is-etf": symbol == "SPY"}
    return {"symbol": symbol, "instrument-type": kind, "streamer-symbol": "." + symbol.replace(" ", ""),
            "active": True, "underlying-symbol": "SPY", "expiration-date": "2026-10-09",
            "strike-price": "770", "option-type": "C" if "261009C" in symbol else "P"}


def identity(symbol="SPY", kind="Equity"):
    return instrument(symbol, metadata(symbol, kind), AT - timedelta(seconds=2))


def row(symbol="SPY", kind="Equity", **changes):
    out = {"symbol": symbol, "instrument-type": kind, "bid": "770.123456789123456789",
           "ask": "770.20", "bid-size": "200.5", "ask-size": "300.25", "is-trading-halted": False,
           "updated-at": (AT - timedelta(seconds=1)).isoformat()}
    out.update(changes)
    return out


def normalize_row(value=None, ident=None):
    return normalize(value if value is not None else row(), ident or identity(), "production",
                     AT - timedelta(milliseconds=50), AT)


def client_factory(*, alter=None, fail_on=None, max_requests=12):
    calls, counts = [], {"quotes": 0}
    def request(method, url, headers, payload):
        parts = urlsplit(url)
        calls.append((method, parts.path, parse_qs(parts.query)))
        if parts.path == "/oauth/token":
            assert payload["scope"] == "read"
            return {"access_token": "fixture-access", "token_type": "Bearer", "expires_in": 900}
        if parts.path.startswith("/instruments/equities/"):
            return {"data": metadata(unquote(parts.path.split("/")[-1]))}
        if parts.path.startswith("/instruments/equity-options/"):
            return {"data": metadata(unquote(parts.path.split("/")[-1]), "Equity Option")}
        if parts.path.startswith("/option-chains/"):
            return {"data": {"items": [{"underlying-symbol": "SPY", "option-chain-type": "Standard",
                "expirations": [{"expiration-date": "2026-10-09",
                "strikes": [{"strike-price": "770", "call": CALL, "put": PUT}]}]}]}}
        assert parts.path == "/market-data/by-type"
        counts["quotes"] += 1
        if fail_on == counts["quotes"]:
            raise QuoteUnavailable("REST_HTTP_429")
        result = [row(s, "Equity" if kind == "equity" else "Equity Option")
                  for kind, groups in parse_qs(parts.query).items() for s in groups[0].split(",")]
        if alter:
            result = alter(result, counts["quotes"])
        return {"data": {"items": result}, "pagination": None}
    return ReadClient(Credentials("fixture-client", "fixture-refresh"), request=request,
                      clock=lambda: AT, max_requests=max_requests), calls


def test_timestamp_meaning_precision_and_source_distinct_from_receipt():
    value = normalize_row()
    view = value.view(AT, AGE)
    assert value.bid == Decimal("770.123456789123456789")
    assert value.bid_size == Decimal("200.5")
    assert value.updated_at < value.requested_at < value.received_at
    assert view["source"] == "tastytrade-rest-snapshot"
    assert view["timestamp_meaning"] == "PROVIDER_QUOTE_LAST_UPDATE"
    assert view["side_change_times"] == "NOT_PROVIDED_NOT_INFERRED"
    assert view["coverage"] == "NOT_ATTESTED" and view["decision_eligibility"] == "NOT_EVALUATED"
    assert view["age_status"] == "WITHIN_EXISTING_QUOTE_POLICY"
    assert view["request_duration_seconds"] == .05


def test_offset_and_camelcase_are_explicit_not_guessed():
    original = row()
    original["updated-at"] = "2026-10-05T09:59:59-04:00"
    dasherized = normalize_row(original)
    other = {({"instrument-type": "instrumentType", "updated-at": "updatedAt",
               "bid-size": "bidSize", "ask-size": "askSize", "is-trading-halted": "tradingHalted"}.get(k, k)): v
             for k, v in original.items()}
    value = normalize_row(other)
    assert value.updated_at == dasherized.updated_at == AT - timedelta(seconds=1)
    assert value.wire_time_field == "updatedAt" and value.evidence_digest == dasherized.evidence_digest


@pytest.mark.parametrize("value", [None, 0, True, "", "NaN", "2026-10-05T14:00:00",
                                  "1970-01-01T00:00:00Z", "2026-02-30T00:00:00Z",
                                  "2026-10-05T14:00:00.1234567Z", "2026-10-05T14:00:00.001Z"])
def test_bad_time_never_uses_receipt(value):
    with pytest.raises(QuoteUnavailable):
        update_time(value, AT)


@pytest.mark.parametrize("key,camel", [("updated-at", "updatedAt"), ("instrument-type", "instrumentType"),
                                       ("bid-size", "bidSize"), ("is-trading-halted", "tradingHalted")])
def test_conflicting_aliases_are_not_silently_selected(key, camel):
    with pytest.raises(QuoteUnavailable, match="ALIAS_CONFLICT"):
        normalize_row(row(**{camel: "conflict"}))


@pytest.mark.parametrize("change", [{"bid": True}, {"ask": "Infinity"}, {"bid": "-1"}, {"bid": 0},
                                   {"bid-size": "NaN"}, {"ask-size": 0}, {"ask-size": None},
                                   {"bid": "771"}, {"ask": "770.123456789123456789"},
                                   {"is-trading-halted": "false"}])
def test_invalid_or_unfillable_book_is_not_normalized(change):
    with pytest.raises(QuoteUnavailable):
        normalize_row(row(**change))


def test_unchanged_prices_new_timestamp_and_old_timestamp_new_receipt():
    old = normalize_row(row(**{"updated-at": (AT - timedelta(seconds=61)).isoformat()}))
    current = normalize_row()
    assert old.bid == current.bid and old.ask == current.ask
    assert old.view(AT, AGE)["age_status"] == "OUTSIDE_EXISTING_QUOTE_POLICY"
    assert current.view(AT, AGE)["age_status"] == "WITHIN_EXISTING_QUOTE_POLICY"
    assert old.evidence_digest != current.evidence_digest
    assert current.view(AT + timedelta(seconds=61), AGE)["age_status"] == "OUTSIDE_EXISTING_QUOTE_POLICY"


def test_halt_and_unknown_are_never_active_by_default():
    assert normalize_row(row(**{"is-trading-halted": True})).view(AT, AGE)["trading_status"] == "HALTED"
    value = row(); value.pop("is-trading-halted")
    assert normalize_row(value).view(AT, AGE)["trading_status"] == "UNKNOWN"


@pytest.mark.parametrize("nested", [{"symbol": "QQQ", "instrumentType": "Equity"},
                                     {"symbol": "SPY", "instrumentType": "Equity Option"}, "wrong"])
def test_nested_identity_contradiction_refuses(nested):
    with pytest.raises(QuoteUnavailable, match="IDENTITY"):
        normalize_row(row(instrument=nested))


def test_missing_time_bad_bracket_and_environment_refuse():
    value = row(); value.pop("updated-at")
    with pytest.raises(QuoteUnavailable, match="TIME_INVALID"):
        normalize_row(value)
    for environment, sent in (("unknown", AT), ("production", AT + timedelta(seconds=1))):
        with pytest.raises(QuoteUnavailable, match="CONTEXT_INVALID"):
            normalize(row(), identity(), environment, sent, AT)


def test_peer_isolation_missing_duplicate_unexpected_and_wrong_type():
    ids = [identity(s) for s in ("SPY", "QQQ", "NVDA", "AAPL")]
    values, failures, unexpected = normalize_batch([row(), row("QQQ"), row("QQQ"),
        row("NVDA", "Equity Option"), row("OTHER"), {"symbol": False}], ids, "production", AT, AT)
    assert set(values) == {"SPY"} and unexpected == 2
    assert failures == {"QQQ": "SNAPSHOT_DUPLICATE", "NVDA": "SNAPSHOT_IDENTITY_MISMATCH", "AAPL": "SNAPSHOT_MISSING"}


def test_stock_and_actual_option_queries_preserve_provider_symbols():
    client, calls = client_factory()
    ids = [identity("BRK.B"), identity(CALL, "Equity Option")]
    rows = client.market_quotes(ids)
    assert calls[-1] == ("GET", "/market-data/by-type", {"equity": ["BRK/B"], "equity-option": [CALL]})
    values, failures, _ = normalize_batch(rows, ids, "production", AT, AT)
    assert set(values) == {"BRK.B", "SPY261009C00770000"} and not failures


@pytest.mark.parametrize("ids", [[], [identity(), identity()], [replace(identity(), provider_symbol="QQQ")],
                               [None], [identity()] * 101])
def test_bad_request_consumes_zero_calls(ids):
    client, calls = client_factory()
    with pytest.raises(QuoteUnavailable):
        client.market_quotes(ids)
    assert calls == [] and client.requests == 0


def test_request_budget_and_allowlist_remain_enforced():
    client, calls = client_factory(max_requests=1)
    with pytest.raises(QuoteUnavailable, match="REST_REQUEST_BUDGET"):
        client.market_quotes([identity()])
    assert len(calls) == 1 and calls[0][1] == "/oauth/token"
    with pytest.raises(QuoteUnavailable, match="REST_ROUTE_REFUSED"):
        client._call("POST", "/market-data/by-type")
    with pytest.raises(QuoteUnavailable, match="REST_QUERY_REFUSED"):
        client._call("GET", "/api-quote-tokens", query={"equity": "SPY"})


@pytest.mark.parametrize("reply", [{"data": []}, {"data": {"items": {}}},
                                    {"data": {"items": []}, "pagination": {"next": "refused"}}])
def test_invalid_batch_shape_or_pagination_stops(reply):
    client, _ = client_factory()
    client.request = lambda *args: reply
    client._access, client._expires = "fixture-access", AT + timedelta(minutes=10)
    with pytest.raises(QuoteUnavailable, match="SNAPSHOT_"):
        client.market_quotes([identity()])


def test_documented_hundred_symbol_batch_boundary():
    client, calls = client_factory()
    ids = [identity("A" + str(i)) for i in range(100)]
    assert len(client.market_quotes(ids)) == 100
    assert len(calls[-1][2]["equity"][0].split(",")) == 100


def test_failed_second_refresh_has_no_current_success_or_retry():
    client, calls = client_factory(fail_on=2)
    result = diagnostic(client, ["SPY", "QQQ"], rounds=3, interval_seconds=0, clock=lambda: AT, sleep=lambda _: None)
    assert result["status"] == "UNAVAILABLE" and result["reason"] == "REST_HTTP_429"
    assert result["current"] == [] and len(result["rounds"]) == 1
    assert result["rounds"][0]["current_eligible"] is False
    assert sum(c[1] == "/market-data/by-type" for c in calls) == 2


def test_option_rounds_use_resolved_chain_and_fresh_snapshot_midpoint():
    client, _ = client_factory()
    result = diagnostic(client, ["SPY", "QQQ", "NVDA"], option_underlying="SPY", interval_seconds=0,
                        clock=lambda: AT, sleep=lambda _: None)
    assert result["status"] == "OBSERVATIONS_ONLY" and len(result["current"]) == 5
    assert result["option_selection"]["reference"] == "SNAPSHOT_MIDPOINT"
    assert result["option_selection"]["call"] == CALL and result["option_selection"]["put"] == PUT
    assert result["LIVE_TIMING"] == "OBSERVATIONS_REQUIRE_REVIEW"
    assert result["requests"] == 10


def test_all_second_round_rows_invalid_do_not_reuse_first_round():
    client, _ = client_factory(alter=lambda rows, n: rows if n == 1 else [dict(r, **{"bid": True}) for r in rows])
    result = diagnostic(client, ["SPY"], rounds=2, interval_seconds=0, clock=lambda: AT, sleep=lambda _: None)
    assert result["status"] == "UNAVAILABLE" and result["current"] == []
    assert result["current_failures"] == {"SPY": "SNAPSHOT_PRICE_OR_SIZE_INVALID"}
    assert len(result["rounds"][0]["checks"]) == 1 and not result["rounds"][1]["checks"]


def test_missing_one_option_remains_partial_not_complete():
    client, _ = client_factory(alter=lambda rows, n: [r for r in rows if r["symbol"] != PUT])
    result = diagnostic(client, ["SPY"], option_underlying="SPY", option_strike=Decimal("770"),
                        rounds=1, clock=lambda: AT)
    assert result["status"] == "PARTIAL_OBSERVATIONS" and len(result["current"]) == 2
    assert result["current_failures"] == {"SPY261009P00770000": "SNAPSHOT_MISSING"}


def test_metadata_denial_stops_before_snapshot_or_other_symbols():
    client, calls = client_factory()
    original = client.request
    def deny(method, url, headers, payload):
        if "/instruments/" in url:
            raise QuoteUnavailable("REST_HTTP_403")
        return original(method, url, headers, payload)
    client.request = deny
    result = diagnostic(client, ["SPY", "QQQ"], clock=lambda: AT)
    assert result["status"] == "UNAVAILABLE" and result["reason"] == "REST_HTTP_403"
    assert client.requests == 2 and len(calls) == 1


def test_stale_option_reference_does_not_disable_equity_observations():
    client, calls = client_factory(alter=lambda rows, _: [dict(r, **{"updated-at": (AT - timedelta(minutes=2)).isoformat()}) for r in rows])
    result = diagnostic(client, ["SPY"], option_underlying="SPY", interval_seconds=0, clock=lambda: AT, sleep=lambda _: None)
    assert result["status"] == "PARTIAL_OBSERVATIONS" and len(result["current"]) == 1
    assert result["option_selection"]["reason"] == "OPTION_REFERENCE_SNAPSHOT_UNAVAILABLE"
    assert not any(c[1].startswith("/option-chains") for c in calls)


def test_explicit_option_strike_offhours_has_no_fake_live_acceptance():
    closed = AT + timedelta(hours=7)
    client, _ = client_factory()
    result = diagnostic(client, ["SPY"], option_underlying="SPY", option_strike=Decimal("770"),
                        rounds=1, clock=lambda: closed)
    assert result["status"] == "OBSERVATIONS_ONLY" and len(result["current"]) == 3
    assert result["LIVE_TIMING"] == "NOT_TESTED_MARKET_CLOSED"
    assert all(r["scope"] == "OFFHOURS_SCHEMA_AND_ACCESS_ONLY" for r in result["rounds"])


@pytest.mark.parametrize("kwargs", [{"rounds": True}, {"rounds": 0}, {"interval_seconds": float("nan")},
                                    {"interval_seconds": True}, {"option_strike": Decimal("770")}])
def test_invalid_diagnostic_arguments_never_contact_provider(kwargs):
    client, calls = client_factory()
    result = diagnostic(client, ["SPY"], clock=lambda: AT, **kwargs)
    assert result["status"] == "UNAVAILABLE" and calls == []


def test_new_route_uses_existing_safe_http_failure_translation():
    client, _ = client_factory()
    client._access, client._expires = "fixture-access", AT + timedelta(minutes=10)
    def bad(*args):
        raise RuntimeError("Bearer secret-private-body")
    client.request = bad
    with pytest.raises(QuoteUnavailable) as error:
        client.market_quotes([identity()])
    assert str(error.value) == "REST_TRANSPORT_FAILURE"


def test_cli_credential_guard_rejects_before_output(tmp_path, monkeypatch, capsys):
    client, _ = client_factory()
    monkeypatch.setattr("desk.snapshot_quote_check.ReadClient", lambda *a, **kw: client)
    monkeypatch.setattr("desk.snapshot_quote_check.diagnostic", lambda *a, **kw: {
        "purpose": "snapshot diagnostic", "status": "OBSERVATIONS_ONLY", "injected": "fixture-refresh"})
    path = tmp_path / "report.json"
    result = main(["--environment", "production", "--symbols", "SPY", "--output", str(path)],
                  env={"TASTYTRADE_REFRESH_TOKEN": "fixture-refresh"})
    assert result == 1 and json.loads(path.read_text())["status"] == "REPORT_REJECTED"
    assert "fixture-refresh" not in capsys.readouterr().out
