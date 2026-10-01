"""Synthetic access/schema cases; live provider availability is checked separately."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from desk.earnings_sources import collect, main, sec_summary
from desk.sec import SecData, SecError, _NoRedirect
from desk.webull import WebullData, WebullHTTPError, FINANCIAL_ALERT_PATH

NOW = datetime(2026, 10, 1, 20, tzinfo=timezone.utc)
CONTACT = "Test application test@example.invalid"


def submissions(symbol="NVDA", cik="0001045810"):
    return {"cik": cik, "name": "Synthetic", "tickers": [symbol], "filings": {"recent": {
        "accessionNumber": ["synthetic"], "form": ["10-Q"], "reportDate": ["2026-07-26"],
        "filingDate": ["2026-08-27"], "acceptanceDateTime": ["2026-08-27T12:00:00Z"]}}}


def facts():
    return {"cik": 1045810, "entityName": "Synthetic", "facts": {"us-gaap": {
        "EarningsPerShareDiluted": {"units": {"USD/shares": [
            {"start": "2026-04-27", "end": "2026-07-26", "val": 2, "filed": "2026-08-27", "fy": 2027, "fp": "Q2"},
            {"start": "2026-01-26", "end": "2026-07-26", "val": 4, "filed": "2026-08-27", "fy": 2027, "fp": "Q2"}
        ]}}}}}


def test_financial_alert_signed_official_route_and_alias():
    calls = []
    def transport(req, timeout):
        calls.append(req)
        return b'{}'
    source = WebullData("key", "secret", transport=transport, min_interval=0)
    assert source.financial_alert("BRK.B") == {}
    req = calls[0]
    assert req.method == "GET" and urlparse(req.full_url).path == FINANCIAL_ALERT_PATH
    assert parse_qs(urlparse(req.full_url).query) == {"symbol": ["BRK B"], "category": ["US_STOCK"]}
    assert req.get_header("X-version") == "v3" and req.get_header("X-signature")
    with pytest.raises(ValueError):  # WebullError derives from BarDataError/ValueError
        source.financial_alert("")


class FakeWebull:
    _host = "api.sandbox.webull.com"
    _key, _secret, _token = "private-key", 'private-"secret', "private-token"
    def __init__(self, alert=None):
        self.calls = []
        self.alert = [] if alert is None else alert
    def financial_alert(self, symbol):
        self.calls.append((symbol, "alert"))
        if isinstance(self.alert, Exception):
            raise self.alert
        return self.alert
    def earnings_calendar(self, symbol):
        self.calls.append((symbol, "calendar"))
        return [{"expected_publish_date": "2026-11-01", "untrusted_text": self._secret}]
    def quarterly_income(self, symbol):
        self.calls.append((symbol, "income"))
        return []


def sec_transport(calls):
    def fetch(req, timeout):
        calls.append(req)
        if "company_tickers" in req.full_url:
            payload = {"0": {"ticker": "NVDA", "cik_str": 1045810, "title": "Synthetic"}}
        elif "submissions" in req.full_url:
            payload = submissions()
        else:
            payload = facts()
        return json.dumps(payload).encode()
    return fetch


def fake_sec(calls):
    return SecData(CONTACT, transport=sec_transport(calls), clock=lambda: NOW, sleep=lambda n: None)


def test_collection_preserves_payload_hash_timing_and_redacts_before_disk(tmp_path):
    source = FakeWebull()
    report = collect(["NVDA"], tmp_path, webull=source, clock=lambda: NOW)
    assert report["status"] == "INCOMPLETE"  # empty is not verified no-event coverage
    assert [r["status"] for r in report["checks"]] == ["EMPTY_UNVERIFIED", "OBSERVATIONS_ONLY", "EMPTY_UNVERIFIED"]
    row = report["checks"][1]
    raw = Path(row["file"]).read_bytes()
    assert row["sha256"] == hashlib.sha256(raw).hexdigest()
    assert json.loads(raw)["received_at"] == NOW.isoformat()
    assert Path(row["file"]).stat().st_mode & 0o777 == 0o600
    for path in Path(report["run_directory"]).glob("*.json"):
        text = path.read_text()
        for secret in (source._key, source._secret, source._token):
            assert secret not in text and json.dumps(secret)[1:-1] not in text
    assert "<redacted>" in raw.decode()


@pytest.mark.parametrize("failure", [
    {"Information": "private-key"}, {"error_code": "DENIED"}, {"success": False},
    {"code": "429"}, "html error", URLError("private-key"),
    WebullHTTPError(429, "api.sandbox.webull.com", FINANCIAL_ALERT_PATH),
])
def test_provider_failure_stops_webull_but_sec_still_runs(tmp_path, failure):
    webull, calls = FakeWebull(failure), []
    result = collect(["NVDA"], tmp_path, webull=webull, sec=fake_sec(calls), clock=lambda: NOW)
    assert len(webull.calls) == 1 and len(calls) == 3
    assert result["checks"][0]["status"] == "UNAVAILABLE"
    assert result["checks"][1]["status"] == "NOT_RUN"
    assert result["checks"][-1]["status"] == "OBSERVATIONS_ONLY"
    assert not list(Path(result["run_directory"]).glob("webull-*.json"))


def test_sec_http_failure_closes_error_and_stops_without_retry(tmp_path):
    calls, body = [], io.BytesIO(b"private response")
    def fetch(req, timeout):
        calls.append(req)
        raise HTTPError(req.full_url, 403, "private reason", {}, body)
    source = SecData(CONTACT, transport=fetch)
    result = collect(["NVDA", "AAPL"], tmp_path, sec=source, clock=lambda: NOW)
    assert len(calls) == 1 and body.closed
    assert result["source_errors"] == [{"provider": "sec", "stage": "ticker_index", "reason": "HTTP_ERROR", "http_status": 403}]
    assert all(r["status"] == "NOT_RUN" for r in result["checks"])
    assert "private response" not in json.dumps(result) and "private reason" not in json.dumps(result)
    assert CONTACT not in json.dumps(result)


def test_sec_success_keeps_ytd_and_quarter_distinct_observations(tmp_path):
    calls = []
    result = collect(["NVDA"], tmp_path, sec=fake_sec(calls), clock=lambda: NOW)
    assert len(calls) == 3 and result["status"] == "OBSERVATIONS_ONLY"
    summary = result["checks"][-1]["summary"]
    assert summary["normalized_quarter_pair"] is False
    rows = summary["concept_observations"]["EarningsPerShareDiluted"]["USD/shares"]["sample"]
    assert len(rows) == 2 and len({r["start"] for r in rows}) == 2
    assert all(req.get_header("User-agent") == CONTACT for req in calls)
    assert all("api_key" not in req.full_url for req in calls)


@pytest.mark.parametrize("field,value,code", [("cik", "0000000001", "IDENTITY_MISMATCH"),
    ("cik", True, "IDENTITY_MISMATCH"), ("tickers", ["AAPL"], "TICKER_MISMATCH"),
    ("filings", [], "FILINGS_SCHEMA_UNAVAILABLE")])
def test_sec_identity_and_schema_checks(field, value, code):
    data = submissions(); data[field] = value
    source = SecData(CONTACT, transport=lambda *args: json.dumps(data).encode())
    with pytest.raises(SecError, match=code):
        source.observations("0001045810", "submissions", "NVDA")


def test_ticker_resolution_alias_and_duplicates():
    index = {"payload": {"0": {"ticker": "BRK-B", "cik_str": 1067983}}}
    assert SecData.resolve(index, "BRK.B") == "0001067983"
    index["payload"]["1"] = index["payload"]["0"]
    with pytest.raises(SecError, match="AMBIGUOUS"):
        SecData.resolve(index, "BRK.B")


@pytest.mark.parametrize("body", [b'[]', b'<html>blocked</html>', b'{"value": NaN}', b'{"error":"secret"}'])
def test_sec_http200_invalid_payload_is_not_data(body):
    with pytest.raises(SecError, match="INVALID_JSON_RESPONSE"):
        SecData(CONTACT, transport=lambda *args: body).ticker_index()


def test_sec_pacing_and_no_redirect():
    waits = []
    source = SecData(CONTACT, transport=lambda *args: b'{}', monotonic=lambda: 1., sleep=waits.append)
    source.ticker_index(); source.ticker_index()
    assert waits == [.25]
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None


@pytest.mark.parametrize("ua", ["", "fake", "Contact a@b\nHeader: injected"])
def test_sec_contact_validation(ua):
    with pytest.raises(SecError, match="SEC_USER_AGENT_REQUIRED"):
        SecData(ua)


def test_cli_missing_contact_still_reports_without_printing_exception(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SEC_USER_AGENT", "invalid")
    assert main(["--sources", "sec", "--output-dir", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["source_errors"][0]["reason"] == "SEC_USER_AGENT_REQUIRED"


def test_repeat_collection_retains_original_receipt_without_overwrite(tmp_path):
    first = collect(["NVDA"], tmp_path, webull=FakeWebull(), clock=lambda: NOW)
    second = collect(["NVDA"], tmp_path, webull=FakeWebull(), clock=lambda: NOW)
    assert first["run_directory"] != second["run_directory"]
    assert Path(first["checks"][0]["file"]).exists()


def test_reject_malformed_parallel_filing_columns():
    data = submissions(); data["filings"]["recent"]["form"] = []
    with pytest.raises(SecError, match="FILINGS_COLUMNS_MISMATCH"):
        sec_summary("submissions", data)
