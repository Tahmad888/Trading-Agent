from io import BytesIO
from urllib.error import HTTPError

import pytest

from desk import provider_check
from desk.webull import WebullData, WebullError, WebullHTTPError, HOST, SANDBOX_HOST, DISPLAY_ACTIONS_PATH
from tests.test_webull import FakeTransport, client
from tests.test_data_contracts import daily, intraday, now
from desk.bar_contract import developing_daily_from_m15


@pytest.mark.parametrize("host,spacing", [(HOST, 1.05), (SANDBOX_HOST, 2.1)])
def test_environment_pacing_with_fake_clock(host, spacing, monkeypatch):
    clock = [0.0]
    slept = []
    monkeypatch.setattr("desk.webull.time.monotonic", lambda: clock[0])
    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr("desk.webull.time.sleep", sleep)
    source = WebullData("key", "secret", host=host, transport=FakeTransport([]))
    source.dividend_calendar("NVDA")
    assert not slept  # first call has no phantom startup delay
    source.dividend_calendar("AAPL")
    assert slept == [spacing]
    source.fund_splits("SPY")
    assert slept == [spacing]  # independent endpoint quota


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True, "2"])
def test_invalid_pacing_rejected(value):
    with pytest.raises(WebullError):
        WebullData("key", "secret", min_interval=value)


def test_direct_constructor_cannot_silently_switch_to_display_host():
    with pytest.raises(WebullError, match="different API product"):
        WebullData("key", "secret", host="us-global-openapi.uat.webullbroker.com")


@pytest.mark.parametrize("status,expected", [(404, "Display Solution"), (403, "access denied"), (429, "shared")])
def test_http_diagnostics_preserve_route_and_close_response_without_exposing_body(status, expected):
    failure = HTTPError("https://example?secret=secret-value", status, "private diagnostic", {}, BytesIO(b"secret-value"))
    transport = FakeTransport(exc=failure)
    source = client(transport)
    with pytest.raises(WebullHTTPError) as caught:
        source._call("GET", DISPLAY_ACTIONS_PATH, {"private_query": "secret-value"})
    exc = caught.value
    assert expected in str(exc)
    assert exc.status == status and exc.host == HOST and exc.path == DISPLAY_ACTIONS_PATH
    assert failure.fp.closed and "secret-value" not in str(exc)
    assert len(transport.requests) == 1  # no endpoint guessing or automatic retries


def test_partial_actions_use_documented_paths_and_categories():
    transport = FakeTransport([{"ex_date": "2026-09-18"}])
    source = client(transport)
    assert source.dividend_calendar("NVDA") == [{"ex_date": "2026-09-18"}]
    source.fund_splits("SPY")
    assert "/dividend-calendars/list?symbol=NVDA&category=US_STOCK" in transport.requests[0].full_url
    assert "/fund-splits/get?symbol=SPY&category=US_STOCK" in transport.requests[1].full_url


@pytest.mark.parametrize("reply", [None, {"error": "unavailable"}, [None], ["bad"]])
def test_malformed_action_reply_does_not_become_no_actions(reply):
    with pytest.raises(WebullError, match="partial corporate-action"):
        client(FakeTransport(reply)).dividend_calendar("NVDA")


def test_probe_empty_or_populated_results_never_claim_complete_coverage():
    for rows in ([], [{"ex_date": "2026-09-18", "private_value": "never print this"}]):
        result = provider_check.check(client(FakeTransport(rows)))
        assert result["corporate_action_coverage"] == "UNKNOWN"
        assert all(r["status"] == "PARTIAL_EVIDENCE_ONLY" for r in result["checks"])
        assert "never print this" not in str(result)


def test_probe_missing_keys_is_actionable_and_does_not_print_credentials(monkeypatch, capsys):
    monkeypatch.delenv("WEBULL_APP_KEY", raising=False)
    monkeypatch.delenv("WEBULL_APP_SECRET", raising=False)
    assert provider_check.main() == 1
    assert "NOT_CONFIGURED" in capsys.readouterr().out


def test_snapshot_explicitly_marks_all_five_composed_fields():
    snapshot = developing_daily_from_m15(daily([100] * 60), intraday(), now(time="15:45"))
    components = snapshot.attrs["developing_components"]
    assert all(field in components for field in ("open", "high", "low", "close", "volume"))
    assert "not provider daily high" in components["high"]
