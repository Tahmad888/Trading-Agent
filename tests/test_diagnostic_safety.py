"""Local CLI/request ceilings; no provider request is sent."""
from io import BytesIO
import pytest

from desk import provider_check
from desk.tastytrade_quotes import QuoteUnavailable
from desk.tastytrade_transport import request_json
from desk.tradier_client import RequestBudgetExceeded, TradierClient
from desk.tradier_option_check import diagnostic
from tests.test_tradier_option_check import Clock, RTH, Fake


@pytest.mark.parametrize("argv,code", [(["--help"], 0), (["--unknown"], 2)])
def test_provider_help_and_bad_arguments_never_construct_client(monkeypatch, argv, code):
    def forbidden():
        raise AssertionError("client construction happened before argument validation")
    monkeypatch.setattr(provider_check.WebullData, "from_env", forbidden)
    with pytest.raises(SystemExit) as exc:
        provider_check.main(argv)
    assert exc.value.code == code


def test_default_provider_command_retains_actual_check(monkeypatch):
    seen = []
    monkeypatch.setattr(provider_check.WebullData, "from_env", lambda: seen.append("client") or object())
    monkeypatch.setattr(provider_check, "check", lambda source: {"checks": [{"status": "PARTIAL_EVIDENCE_ONLY"}]})
    assert provider_check.main([]) == 0 and seen == ["client"]


class QuotaOpener:
    def open(self, *args, **kwargs):
        raise RequestBudgetExceeded()


def test_external_typed_budget_survives_http_transport():
    with pytest.raises(RequestBudgetExceeded, match="^REST_REQUEST_BUDGET$"):
        request_json("GET", "https://fixture.invalid", {}, None, opener=QuotaOpener())


def test_unknown_exception_is_not_inferred_from_secret_bearing_text():
    class UnknownOpener:
        def open(self, *args, **kwargs):
            raise RuntimeError("quota exhausted: confidential credential")
    with pytest.raises(QuoteUnavailable, match="^REST_TRANSPORT_FAILURE$"):
        request_json("GET", "https://fixture.invalid", {}, None, opener=UnknownOpener())


def test_internal_budget_refusal_sends_no_second_request():
    calls = []
    client = TradierClient("fixture", max_requests=1, request=lambda *a: calls.append(a) or {})
    client.get("quotes", {"symbols": "SPY"})
    with pytest.raises(RequestBudgetExceeded):
        client.get("quotes", {"symbols": "SPY"})
    assert len(calls) == client.requests == 1


def test_final_external_budget_failure_never_reuses_first_round():
    clock = Clock(RTH)
    fake = Fake(clock)
    calls = []
    def capped(*args):
        if calls:
            raise RequestBudgetExceeded()
        calls.append(True)
        return fake(*args)
    client = TradierClient("fixture", request=capped, clock=clock)
    result = diagnostic(client, equities=["SPY"], rounds=2, interval_seconds=0, sleep=lambda _: None)
    assert result["reason"] == "REST_REQUEST_BUDGET"
    assert result["failure_category"] == "LOCAL_REQUEST_BUDGET"
    assert result["current"] == "NONE_AFTER_FAILURE"
    assert result["rounds"][0]["observations"]["SPY"]
    assert result["rounds"][1]["failed"] == "REST_REQUEST_BUDGET"
    assert result["verdicts"]["stock_quote_freshness"]["verdict"] == "FAIL"
    assert len(fake.calls) == 1
