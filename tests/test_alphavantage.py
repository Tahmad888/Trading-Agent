"""Offline cases: the error fixture was reported; success envelopes are synthetic."""
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import traceback
from urllib import error, parse

import pytest

from desk.alphavantage import AlphaVantageActions, AlphaVantageError, _NoRedirect
from desk import alphavantage_check

FAKE_KEY = "TESTKEYNOTREAL123"
NOW = datetime(2026, 10, 1, 6, 10, tzinfo=timezone.utc)


class Transport:
    def __init__(self, payload=None, exc=None, raw=None):
        self.payload, self.exc, self.raw = payload, exc, raw
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if self.exc:
            raise self.exc
        return self.raw if self.raw is not None else json.dumps(self.payload).encode()


def client(transport):
    return AlphaVantageActions(FAKE_KEY, transport=transport, clock=lambda: NOW)


def response(rows=None):
    return {"symbol": "NVDA", "data": rows if rows is not None else
            [{"effective_date": "2024-06-10", "split_factor": "10.0"}]}


def test_documented_request_and_synthetic_split_observation():
    transport = Transport(response())
    snapshot = client(transport).fetch("NVDA", "SPLITS")
    req = transport.requests[0]
    assert req.get_method() == "GET" and req.data is None
    url = parse.urlsplit(req.full_url)
    assert (url.scheme, url.netloc, url.path) == ("https", "www.alphavantage.co", "/query")
    assert parse.parse_qs(url.query) == {"function": ["SPLITS"], "symbol": ["NVDA"],
                                       "datatype": ["json"], "apikey": [FAKE_KEY]}
    assert snapshot.rows[0].value == Decimal("10") and snapshot.received_at == NOW
    result = snapshot.report(include_records=True)
    assert result["coverage"] == "UNKNOWN" and result["records"][0]["event_date"] == "2024-06-10"
    assert FAKE_KEY not in json.dumps(result) and "price_basis" not in result


def test_reported_200_rate_limit_fixture_is_safe_and_stops_following_calls():
    fixture = Path(__file__).parent / "fixtures/alphavantage_rate_limit_redacted.json"
    payload = json.loads(fixture.read_text().replace("<redacted>", FAKE_KEY))
    transport = Transport(payload)
    source = client(transport)
    for function in ("SPLITS", "DIVIDENDS"):
        with pytest.raises(AlphaVantageError, match="RATE_LIMITED") as caught:
            source.fetch("NVDA", function)
        assert FAKE_KEY not in "".join(traceback.format_exception(caught.value))
    assert len(transport.requests) == 1


@pytest.mark.parametrize("field", ["Information", "Note", "Error Message"])
def test_any_error_field_rejects_even_when_data_is_present_or_error_text_empty(field):
    for message in ("", "private message " + FAKE_KEY):
        transport = Transport({**response(), field: message})
        with pytest.raises(AlphaVantageError, match="PROVIDER_REJECTED") as caught:
            client(transport).fetch("NVDA", "SPLITS")
        assert FAKE_KEY not in str(caught.value)


@pytest.mark.parametrize("payload", [None, [], {}, {"symbol": "NVDA"},
                                    {"symbol": "SPY", "data": []},
                                    {"symbol": "NVDA", "data": {}},
                                    {"symbol": "NVDA", "data": [None]}])
def test_bad_envelopes_do_not_become_no_actions(payload):
    with pytest.raises(AlphaVantageError):
        client(Transport(payload)).fetch("NVDA", "SPLITS")


def test_valid_empty_response_does_not_attest_coverage():
    result = client(Transport(response([]))).fetch("NVDA", "SPLITS").report()
    assert result["status"] == "EMPTY_UNVERIFIED" and result["coverage"] == "UNKNOWN"


def test_numeric_json_preserves_exact_decimal_economics():
    raw = b'{"symbol":"NVDA","data":[{"ex_dividend_date":"2024-06-11","amount":0.12345678901234567890123456789}]}'
    snapshot = client(Transport(raw=raw)).fetch("NVDA", "DIVIDENDS")
    assert snapshot.rows[0].value == Decimal("0.12345678901234567890123456789")


def test_success_body_containing_credential_is_never_returned():
    with pytest.raises(AlphaVantageError, match="UNSAFE_RESPONSE"):
        client(Transport({**response(), "extra": FAKE_KEY})).fetch("NVDA", "SPLITS")


def test_invalid_json_with_secret_is_not_exposed():
    with pytest.raises(AlphaVantageError) as caught:
        client(Transport(raw=("bad json " + FAKE_KEY).encode())).fetch("NVDA", "SPLITS")
    assert FAKE_KEY not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_http_failures_close_body_and_suppress_secret_bearing_exception_chain(status):
    failure = error.HTTPError("https://example?apikey=" + FAKE_KEY, status, FAKE_KEY, {}, BytesIO(FAKE_KEY.encode()))
    transport = Transport(exc=failure)
    source = client(transport)
    with pytest.raises(AlphaVantageError) as caught:
        source.fetch("NVDA", "SPLITS")
    assert failure.fp.closed
    assert FAKE_KEY not in "".join(traceback.format_exception(caught.value))
    assert len(transport.requests) == 1
    if status == 429:
        with pytest.raises(AlphaVantageError, match="RATE_LIMITED"):
            source.fetch("SPY", "DIVIDENDS")
        assert len(transport.requests) == 1


def test_redirect_handler_does_not_forward_credentialed_request():
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere") is None


def test_network_error_with_secret_is_suppressed():
    with pytest.raises(AlphaVantageError) as caught:
        client(Transport(exc=error.URLError(FAKE_KEY))).fetch("NVDA", "SPLITS")
    assert FAKE_KEY not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", None, True, "0", "1"])
def test_invalid_or_ambiguous_split_factors_rejected(value):
    with pytest.raises(AlphaVantageError):
        client(Transport(response([{"effective_date": "2024-06-10", "split_factor": value}]))).fetch("NVDA", "SPLITS")


def test_reported_dividend_quirks_preserved_as_unverified_observations():
    rows = [{"ex_dividend_date": "2024-06-11", "amount": "0.01", "declaration_date": "2024-05-22",
             "record_date": "2024-06-11", "payment_date": "2024-06-28"},
            {"ex_dividend_date": "2024-05-29", "amount": "0", "declaration_date": "None"},
            {"ex_dividend_date": "2024-03-05", "amount": "0.04"}]
    result = client(Transport(response(rows))).fetch("NVDA", "DIVIDENDS").report(include_records=True)
    assert result["row_count"] == 3 and result["coverage"] == "UNKNOWN"
    assert result["records"][0]["value"] == "0.04"  # no silent split or double price adjustment
    assert "ZERO_AMOUNT_IGNORED" in result["records"][1]["issues"]
    assert result["records"][1]["declaration_date"] is None


@pytest.mark.parametrize("day", ["None", "2024-02-30", "20240610", None])
def test_missing_or_malformed_effective_date_rejects(day):
    with pytest.raises(AlphaVantageError, match="INVALID_EVENT_DATE"):
        client(Transport(response([{"effective_date": day, "split_factor": "10"}]))).fetch("NVDA", "SPLITS")


def test_duplicates_require_review_instead_of_silent_deduplication():
    rows = [{"ex_dividend_date": "2024-06-11", "amount": "0.01"}] * 2
    with pytest.raises(AlphaVantageError, match="DUPLICATE"):
        client(Transport(response(rows))).fetch("NVDA", "DIVIDENDS")


def test_probe_stops_after_first_error_and_main_reports_safe_failure(monkeypatch, capsys):
    transport = Transport({"Information": "rate limit " + FAKE_KEY})
    monkeypatch.setattr(alphavantage_check.AlphaVantageActions, "from_env", lambda: client(transport))
    assert alphavantage_check.main([]) == 1
    output = capsys.readouterr().out
    assert FAKE_KEY not in output
    report = json.loads(output)
    assert report["stopped_on_error"] and len(report["checks"]) == len(transport.requests) == 1


def test_probe_can_be_limited_to_one_request(monkeypatch, capsys):
    transport = Transport(response())
    monkeypatch.setattr(alphavantage_check.AlphaVantageActions, "from_env", lambda: client(transport))
    assert alphavantage_check.main(["--symbols", "NVDA", "--functions", "SPLITS", "--include-records"]) == 0
    assert len(transport.requests) == 1 and "records" in json.loads(capsys.readouterr().out)["checks"][0]


def test_missing_environment_key_makes_no_request(monkeypatch, capsys):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    assert alphavantage_check.main([]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "NOT_CONFIGURED"
