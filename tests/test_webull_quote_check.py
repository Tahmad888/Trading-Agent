"""Source diagnostic regressions: synthetic data, no providers or host-clock calls."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from urllib import error, parse

import pytest

from desk.quote_measure import write_report
from desk.webull import INSTRUMENTS_PATH, SNAPSHOT_PATH
from desk.webull_quote_check import BoundedData, NoRedirect, ProbeStop, check
from desk import webull_quote_check as probe

AT = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
MS = int(AT.timestamp() * 1000)
IDS = {"SPY": "913243251", "QQQ": "913243249", "NVDA": "913257561", "BRK B": "916040668"}


def metadata(symbol):
    return dict(symbol=symbol, instrument_id=IDS[symbol], name=symbol, category="US_STOCK",
                sub_category="ETF" if symbol in {"SPY", "QQQ"} else "COMMON_STOCK",
                exchange_code="NSQ", currency="USD")


def snapshot(symbol):
    return dict(symbol=symbol, instrument_id=IDS[symbol], bid="100.10", ask="100.20",
                bid_size="25", ask_size="40", price="100.15", volume="150000.25",
                quote_time=MS, last_trade_time=MS - 100)


class Transport:
    def __init__(self, *, alter=None, fail=None, paginate=False):
        self.calls, self.alter, self.fail, self.paginate = [], alter, fail, paginate

    def __call__(self, request, timeout):
        path = parse.urlsplit(request.full_url).path
        query = parse.parse_qs(parse.urlsplit(request.full_url).query)
        self.calls.append((path, query, request.get_method()))
        if self.fail and path == SNAPSHOT_PATH:
            raise self.fail()
        names = query["symbols"][0].split(",")
        if path == INSTRUMENTS_PATH:
            rows = [metadata(n) for n in names]
            if self.paginate:
                reply = {"data": rows[:1] if "pagination_key" not in query else rows[1:]}
                if "pagination_key" not in query:
                    reply["pagination_key"] = "next"
                return json.dumps(reply).encode()
        else:
            rows = [snapshot(n) for n in names]
            if self.alter:
                rows = self.alter(rows)
        return json.dumps(rows).encode()


def source(transport=None, *, max_requests=5):
    return BoundedData("fixture-key", "fixture-secret", transport=transport or Transport(),
                       host="api.sandbox.webull.com", min_interval=0, clock=lambda: AT,
                       max_requests=max_requests)


def run(data=None, names=None, **kwargs):
    return check(data or source(), names or ["SPY", "QQQ", "NVDA"], clock=lambda: AT,
                 sleep=lambda _: None, **kwargs)


def test_observations_group_etfs_and_stock_and_never_attest_eligibility():
    transport = Transport()
    report = run(source(transport))
    assert report["status"] == "OBSERVATIONS_ONLY" and report["requests"] == 5
    assert [call[1].get("category") for call in transport.calls] == [
        ["US_STOCK"], ["US_STOCK"], ["US_ETF"], ["US_STOCK"], ["US_ETF"]]
    assert all(call[2] == "GET" for call in transport.calls)
    assert len(report["checks"]) == 6
    assert report["LIVE_TIMING"] == "REGULAR_SESSION_OBSERVATIONS_REQUIRE_REVIEW"
    assert report["nbbo_coverage"] == report["bid_ask_side_time_contract"] == "NOT_ESTABLISHED"
    assert report["option_coverage"] == "NOT_TESTED"
    spy = next(item for item in report["checks"] if item["symbol"] == "SPY")
    assert spy["fields"]["volume"] == {"state": "VALUE", "value": "150000.25"}
    assert spy["quote_time"]["at"] == AT.isoformat()
    assert "bidTime" not in spy and "askTime" not in spy


def test_request_receipt_bracket_is_independent_of_source_time():
    stamps = iter([AT, AT + timedelta(seconds=1), AT + timedelta(seconds=3), AT + timedelta(seconds=4)])
    report = check(source(), ["NVDA"], rounds=1, clock=lambda: next(stamps), sleep=lambda _: None)
    item = report["checks"][0]
    assert item["request_started_at"] == (AT + timedelta(seconds=1)).isoformat()
    assert item["received_at"] == (AT + timedelta(seconds=3)).isoformat()
    assert item["quote_time"]["at"] == AT.isoformat()
    assert item["quote_time"]["receipt_minus_source_ms"] == 3000
    assert report["rounds"][0]["groups"][0]["request_elapsed_ms"] == 2000


def test_share_class_uses_reviewed_alias_and_exact_instrument_id():
    transport = Transport()
    report = run(source(transport), ["BRK.B"], rounds=1)
    assert report["checks"][0]["symbol"] == "BRK.B"
    assert report["checks"][0]["provider_symbol"] == "BRK B"
    assert report["checks"][0]["identity"] == "MATCH"
    assert all(call[1]["symbols"] == ["BRK B"] for call in transport.calls)


@pytest.mark.parametrize("change, issue", [
    (lambda rows: rows + [rows[0]], "SNAPSHOT_IDENTITY_AMBIGUOUS"),
    (lambda rows: rows[1:], "SNAPSHOT_MISSING"),
    (lambda rows: [dict(rows[0], instrument_id="different")] + rows[1:], "INSTRUMENT_ID_MISMATCH_OR_MISSING"),
    (lambda rows: [dict(rows[0], bid=True)] + rows[1:], "BID_ASK_NONNUMERIC_OR_MISSING"),
    (lambda rows: [dict(rows[0], ask="99")] + rows[1:], "CROSSED_BID_ASK"),
    (lambda rows: [dict(rows[0], bid="0")] + rows[1:], "BID_ASK_NONPOSITIVE"),
])
def test_symbol_failures_remain_visible_and_keep_healthy_peer_observations(change, issue):
    report = run(source(Transport(alter=change)), ["SPY", "QQQ"], rounds=1)
    spy, qqq = report["checks"]
    assert report["status"] == "PARTIAL" and issue in spy["issues"]
    assert qqq["status"] == "FIELDS_OBSERVED" and not qqq["issues"]


@pytest.mark.parametrize("value, state", [
    (0, "ZERO"), (None, "NULL"), (True, "INVALID"), (1.5, "INVALID"),
    ("not-a-time", "INVALID"), (MS + 4, "FUTURE"), (str(MS), "VALUE"),
])
def test_timestamp_states_are_not_fabricated_or_clamped(value, state):
    transport = Transport(alter=lambda rows: [dict(rows[0], quote_time=value)])
    report = run(source(transport), ["NVDA"], rounds=1)
    stamp = report["checks"][0]["quote_time"]
    assert stamp["state"] == state
    assert report["status"] == ("OBSERVATIONS_ONLY" if state == "VALUE" else "PARTIAL")
    if state == "FUTURE":
        assert stamp["raw"] == MS + 4 and stamp["lead_ms"] == 4


def test_absent_time_stays_absent_even_when_trade_time_is_available():
    def change(rows):
        row = dict(rows[0])
        del row["quote_time"]
        return [row]
    report = run(source(Transport(alter=change)), ["NVDA"], rounds=1)
    assert report["checks"][0]["quote_time"]["state"] == "ABSENT"
    assert report["checks"][0]["last_trade_time"]["state"] == "VALUE"
    assert report["status"] == "PARTIAL"


@pytest.mark.parametrize("http_status", [401, 403, 404, 429, 500])
def test_provider_error_stops_before_peer_or_next_round_calls_and_suppresses_text(http_status):
    secret = "fixture-secret"
    transport = Transport(fail=lambda: error.HTTPError("https://example.invalid/" + secret, http_status,
                          secret, {"authorization": secret}, BytesIO(secret.encode())))
    report = run(source(transport))
    assert len(transport.calls) == report["requests"] == 2
    assert report["stopped_on_error"] and report["http_status"] == http_status
    assert report["status"] == "UNAVAILABLE" and secret not in json.dumps(report)


def test_malformed_snapshot_stops_without_logging_body():
    transport = Transport(alter=lambda _: {"Information": "fixture-secret"})
    report = run(source(transport))
    assert report["stopped_on_error"] and report["requests"] == 2
    assert "fixture-secret" not in json.dumps(report)


def test_metadata_pagination_consumes_actual_call_budget():
    transport = Transport(paginate=True)
    report = run(source(transport, max_requests=2), ["SPY", "QQQ"], rounds=1)
    assert report["requests"] == len(transport.calls) == 2
    assert report["stop_reason"] == "REQUEST_BUDGET_EXHAUSTED"
    assert all(call[0] == INSTRUMENTS_PATH for call in transport.calls)


def test_budget_exhaustion_in_metadata_does_not_call_snapshot():
    transport = Transport(paginate=True)
    report = run(source(transport, max_requests=1), ["SPY", "QQQ"], rounds=1)
    assert report["requests"] == len(transport.calls) == 1
    assert report["stopped_on_error"] and not report["checks"]


def test_probe_allowlist_refuses_any_other_route_without_calling_transport():
    transport = Transport()
    data = source(transport)
    for method, path in [("POST", SNAPSHOT_PATH), ("GET", "/accounts"), ("POST", "/orders")]:
        with pytest.raises(ProbeStop, match="ROUTE_NOT_ALLOWED"):
            data._call(method, path)
    assert data.requests == 0 and not transport.calls


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirects_close_the_body_and_cannot_forward_signed_headers(code):
    body = BytesIO(b"fixture-secret")
    handler = getattr(NoRedirect(), "http_error_" + str(code))
    with pytest.raises(ProbeStop, match="HTTP_REDIRECT_REFUSED"):
        handler(None, body, code, "fixture-secret", {"Location": "https://other.invalid/"})
    assert body.closed


def test_default_transport_installs_redirect_refusal_and_sends_one_get(monkeypatch):
    seen = []

    class Opener:
        def open(self, req, *, timeout):
            seen.append((req, timeout))
            return BytesIO(b"[]")

    def factory(handler):
        assert isinstance(handler, NoRedirect)
        return Opener()

    monkeypatch.setattr(probe.request, "build_opener", factory)
    data = BoundedData("fixture-key", "fixture-secret", min_interval=0)
    assert data._call("GET", SNAPSHOT_PATH) == []
    assert len(seen) == data.requests == 1 and seen[0][0].get_method() == "GET"


@pytest.mark.parametrize("arguments", [dict(rounds=0), dict(rounds=4), dict(rounds=True),
                                       dict(interval_seconds=float("nan")), dict(interval_seconds=True)])
def test_invalid_arguments_make_zero_calls(arguments):
    transport = Transport()
    report = run(source(transport), **arguments)
    assert report["requests"] == 0 and not transport.calls
    assert report["stop_reason"] == "INVALID_PROBE_ARGUMENTS"


def test_closed_market_labels_and_clock_evidence_are_observations_only():
    clock_evidence = dict(status="MEASURED", offset_seconds_as_printed="+0.004", applied=False, tolerance="NONE")
    report = check(source(), ["NVDA"], rounds=1, clock=lambda: AT + timedelta(hours=8), host_clock=clock_evidence)
    assert report["LIVE_TIMING"] == "NOT_TESTED_MARKET_CLOSED_OR_SESSION_BOUNDARY"
    assert report["host_clock"] == clock_evidence
    assert report["checks"][0]["quote_time"]["at"] == AT.isoformat()


def test_backwards_local_clock_stops_without_next_call():
    stamps = iter([AT, AT + timedelta(seconds=1), AT])
    transport = Transport()
    report = check(source(transport), ["NVDA"], rounds=2, clock=lambda: next(stamps))
    assert report["stop_reason"] == "LOCAL_CLOCK_MOVED_BACKWARDS"
    assert report["requests"] == 2 and not report["checks"]


def test_saved_report_credential_guard_refuses_even_whitelisted_bad_values(tmp_path):
    transport = Transport(alter=lambda rows: [dict(rows[0], instrument_id="sensitive-example-secret")])
    report = run(source(transport), ["NVDA"], rounds=1)
    output = tmp_path / "observations.json"
    result = write_report(report, output, env={"WEBULL_APP_SECRET": "sensitive-example-secret"})
    assert result["status"] == "REPORT_REJECTED"
    assert "sensitive-example-secret" not in output.read_text()


def test_unrequested_or_unattributable_rows_do_not_attest_the_batch():
    transport = Transport(alter=lambda rows: rows + [{"symbol": "OTHER", "instrument_id": "x"}, None])
    report = run(source(transport), ["NVDA"], rounds=1)
    assert report["status"] == "PARTIAL"
    assert "SNAPSHOT_BATCH_UNATTRIBUTABLE" in report["checks"][0]["issues"]
    assert report["rounds"][0]["groups"][0]["unrequested_or_unattributable_rows"] == 2
