"""Audit regressions; injected transport only, never a provider call."""
from datetime import timedelta

import pytest

from tests.test_tradier_option_check import CALL, PUT, RTH, ms, run
from tests.test_tradier_option_check import Clock, Fake, TOKEN
from desk import option_conventions as oc
from desk import tradier_option_check as tc


def test_future_at_receipt_does_not_become_fresh_when_the_clock_catches_up():
    report, _ = run()
    observation = report["rounds"][-1]["observations"]["SPY"]
    observation["times"]["bid_date"]["receipt"]["age_seconds"] = -0.0038
    result = tc.latest_policy_view(observation, RTH + timedelta(seconds=10))
    assert result["quote"]["verdict"] == "FAIL"
    assert result["quote"]["bid"]["reasons"] == ["FUTURE_SOURCE_TIME"]
    assert result["trade"]["verdict"] == "PASS"


def test_age_is_rechecked_at_report_completion_and_the_exact_limit_still_passes():
    report, _ = run()
    observation = report["rounds"][-1]["observations"]["SPY"]
    from datetime import datetime
    source = datetime.fromisoformat(observation["times"]["bid_date"]["utc"])
    at_limit = tc.price_time_view(observation, "bid", "bid_date", source + timedelta(seconds=60))
    after = tc.price_time_view(observation, "bid", "bid_date", source + timedelta(seconds=60.001))
    assert at_limit["verdict"] == "PASS"
    assert after["verdict"] == "FAIL" and after["reasons"] == ["OLDER_THAN_QUOTE_POLICY"]


@pytest.mark.parametrize("delta,reason", [(-900000, "OLDER_THAN_QUOTE_POLICY"),
                                         (3600000, "FUTURE_SOURCE_TIME")])
def test_advancing_old_or_future_data_never_passes_freshness(delta, reason):
    def shift(n, rows):
        if n > 1:
            for row in rows:
                for field in ("bid_date", "ask_date", "trade_date"):
                    row[field] += delta
        return rows
    report, _ = run(quote_hook=shift)
    assert report["advancing"]["SPY"]["state"] == "ADVANCED"
    for key in ("stock_quote_freshness", "option_quote_freshness", "stock_trade_freshness"):
        assert report["verdicts"][key]["verdict"] == "FAIL"
    bid = report["verdicts"]["stock_quote_freshness"]["by_symbol"]["SPY"]["bid"]
    assert bid["reasons"] == [reason]
    assert bid["max_age_seconds"] == report["quote_policy"]["max_age_seconds"] == 60
    assert (bid["time"]["at_check"]["age_seconds"] < 0) == (delta > 0)


def test_trade_advancement_does_not_freshen_stale_sides():
    def stale(n, rows):
        if n > 1:
            for row in rows:
                row["bid_date"] = row["ask_date"] = ms(RTH - timedelta(minutes=15))
        return rows
    report, _ = run(quote_hook=stale)
    assert report["advancing"]["SPY"]["fields"] == ["trade_date"]
    assert report["verdicts"]["stock_quote_freshness"]["verdict"] == "FAIL"
    assert report["verdicts"]["stock_trade_freshness"]["verdict"] == "PASS"
    assert report["verdicts"]["option_quote_freshness"]["verdict"] == "FAIL"


@pytest.mark.parametrize("stamp", ["2026-02-30 14:00:00", "2026-13-07 14:00:00",
                                  "2026-10-07 25:00:00", "2026-02-30T14:00:00Z"])
def test_impossible_greek_date_is_a_field_failure_and_preserves_quotes(stamp):
    with pytest.raises(oc.ConventionError, match="^TIME_INVALID$"):
        oc.provider_time("tradier", "greeks.updated_at", stamp)
    def bad(n, rows):
        for row in rows:
            if row["symbol"] == PUT:
                row["greeks"]["updated_at"] = stamp
        return rows
    report, _ = run(quote_hook=bad)
    latest = report["rounds"][-1]["observations"]
    assert latest[PUT]["greeks"]["updated_at"] == {"raw": stamp, "state": "TIME_INVALID"}
    assert latest[PUT]["greeks"]["fields"]["delta"]["value"] == "-0.551"
    assert latest[CALL]["greeks"]["updated_at"]["utc"]
    assert report["verdicts"]["stock_quote_freshness"]["verdict"] == "PASS"
    assert report["verdicts"]["option_quote_freshness"]["verdict"] == "PASS"
    assert report["verdicts"]["greek_timestamp_interpretation"]["verdict"] == "FAIL"


def test_invalid_occ_date_does_not_crash_or_discard_valid_chain_rows():
    class InvalidExpiry(Fake):
        def __call__(self, method, url, headers, payload):
            reply = super().__call__(method, url, headers, payload)
            if "/options/chains?" in url:
                reply["options"]["option"].append(dict(symbol="SPY261332C00780000", option_type="call",
                    underlying="SPY", root_symbol="SPY", strike=780.0, contract_size=100))
            return reply
    clock = Clock(RTH)
    client = tc.TradierClient(TOKEN, request=InvalidExpiry(clock), clock=clock)
    report = tc.diagnostic(client, ["SPY"], option_underlying="SPY", rounds=2, sleep=lambda s: None)
    assert report["option_selection"]["call"] == CALL
    assert report["option_selection"]["rejected_chain_rows"] == [{"row": 7, "reason": "OPTION_IDENTITY_INVALID"}]
    assert report["verdicts"]["option_quote_freshness"]["verdict"] == "PASS"


def test_missing_final_option_does_not_inherit_earlier_pass():
    def missing(n, rows):
        return [r for r in rows if r["symbol"] != PUT] if n == 4 else rows
    report, _ = run(quote_hook=missing, rounds=3)
    assert report["advancing"][PUT]["state"] == "ADVANCED"
    view = report["verdicts"]["option_quote_freshness"]
    assert view["verdict"] == "PARTIAL"
    assert view["by_symbol"][PUT] == {"verdict": "FAIL", "reason": "MISSING"}
    assert view["by_symbol"][CALL]["verdict"] == "PASS"


@pytest.mark.parametrize("changes", [dict(bid=None, ask=None), dict(bid=-1), dict(bid=0),
                                     dict(bid=800), dict(ask=779.85), dict(bid_date=0),
                                     dict(bid_date=True)])
def test_advancement_cannot_certify_unusable_stock_bbo(changes):
    def bad(n, rows):
        return [dict(row, **changes) if n > 1 and row["symbol"] == "SPY" else row for row in rows]
    report, _ = run(quote_hook=bad)
    result = report["verdicts"]["stock_quote_freshness"]
    assert result["verdict"] == "PARTIAL"
    assert result["by_symbol"]["SPY"]["verdict"] == "FAIL"
    assert result["by_symbol"]["QQQ"]["verdict"] == "PASS"
    assert report["verdicts"]["stock_trade_freshness"]["verdict"] == "PASS"
