"""Read-only Tradier diagnostic (G5a CP3, Tradier Steps 2/3). Injected transport; no provider call."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from desk import tradier_option_check as tc
from desk.tastytrade_quotes import QuoteUnavailable

UTC = timezone.utc
TOKEN = "fixture-tradier-token-value"
RTH = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)          # Wednesday 10:00 ET
CLOSED = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)        # 04:00 ET
CALL, PUT = "SPY261007C00780000", "SPY261007P00780000"


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        self.now += timedelta(milliseconds=200)
        return self.now


def ms(at):
    return int(at.timestamp() * 1000)


def stock_row(symbol, at, bid=779.85, ask=779.95):
    return dict(symbol=symbol, type="etf" if symbol in {"SPY", "QQQ"} else "stock", bid=bid, ask=ask, last=779.9,
                bidsize=40, asksize=200, bidexch="Q", askexch="P", exch="P", lot_size=40,
                bid_date=ms(at), ask_date=ms(at), trade_date=ms(at))


def option_row(symbol, at, *, right, delta, greeks=True, **over):
    row = dict(symbol=symbol, type="option", underlying="SPY", strike=780.0, expiration_date="2026-10-07",
               option_type=right, root_symbol="SPY", contract_size=100, expiration_type="weeklys",
               bid=2.08, ask=2.10, last=2.09, bidsize=53, asksize=17, bidexch="Q", askexch="Z", exch="Z",
               bid_date=ms(at), ask_date=ms(at), trade_date=ms(at))
    if greeks:
        row["greeks"] = dict(delta=delta, gamma=0.066, theta=-0.65, vega=0.22, rho=0.0191, phi=-0.0192,
                             bid_iv=0.10, mid_iv=0.11, ask_iv=0.12, smv_vol=0.105,
                             updated_at=(at - timedelta(minutes=50)).strftime("%Y-%m-%d %H:%M:%S"))
    row.update(over)
    return row


def chain_rows():
    rows = []
    for strike in (779, 780, 781):
        for right, letter in (("call", "C"), ("put", "P")):
            rows.append(dict(symbol=f"SPY261007{letter}{strike * 1000:08d}", option_type=right, underlying="SPY",
                             root_symbol="SPY", strike=float(strike), expiration_date="2026-10-07", contract_size=100))
    rows.append(dict(symbol="SPY1261007C00780000", option_type="call", underlying="SPY", root_symbol="SPY1",
                     strike=780.0, expiration_date="2026-10-07", contract_size=100))
    return rows


class Fake:
    """Answers by route; each quotes call advances provider times by one minute."""

    def __init__(self, clock, *, quote_hook=None, fault_on=None):
        self.clock, self.calls, self.headers = clock, [], []
        self.quote_hook, self.fault_on, self.quote_calls = quote_hook, fault_on, 0

    def __call__(self, method, url, headers, payload):
        assert method == "GET" and payload is None
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.hostname == "api.tradier.com"
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append((parts.path, query))
        self.headers.append(headers)
        if self.fault_on == len(self.calls):
            return {"fault": {"faultstring": "Invalid Access Token"}}
        if parts.path.endswith("/expirations"):
            return {"expirations": {"date": ["2026-10-07", "2026-10-08"]}}
        if parts.path.endswith("/chains"):
            return {"options": {"option": chain_rows()}}
        self.quote_calls += 1
        at = self.clock.now - timedelta(seconds=2)
        rows = []
        for symbol in query["symbols"].split(","):
            if symbol == CALL:
                rows.append(option_row(symbol, at, right="call", delta=0.449))
            elif symbol == PUT:
                rows.append(option_row(symbol, at, right="put", delta=-0.551))
            else:
                rows.append(stock_row(symbol, at))
        if self.quote_hook:
            rows = self.quote_hook(self.quote_calls, rows)
        return {"quotes": {"quote": rows if len(rows) > 1 else rows[0]}}


def run(start=RTH, rounds=2, **fake):
    clock = Clock(start)
    transport = Fake(clock, **fake)
    client = tc.TradierClient(TOKEN, request=transport, clock=clock)
    report = tc.diagnostic(client, ["SPY", "QQQ"], option_underlying="SPY", rounds=rounds, interval_seconds=0,
                           sleep=lambda s: None)
    return report, transport


def test_rth_capture_normalizes_options_and_keeps_conventions_labelled():
    report, transport = run()
    assert report["status"] == "OBSERVATIONS_ONLY"
    assert report["option_selection"]["call"] == CALL and report["option_selection"]["put"] == PUT
    assert [c[0] for c in transport.calls] == ["/v1/markets/quotes", "/v1/markets/options/expirations",
                                               "/v1/markets/options/chains", "/v1/markets/quotes", "/v1/markets/quotes"]
    assert report["requests"]["count"] == 5 == report["request_plan"]["planned"]
    last = report["rounds"][-1]["observations"]
    call, put = last[CALL], last[PUT]
    assert call["sizes"]["bidsize"]["value"] == "53"
    assert call["sizes"]["bidsize"]["interpreted_unit"] == "CONTRACTS_PROVISIONAL"
    assert last["SPY"]["sizes"]["bidsize"]["size_unit_status"] == "OBSERVED_SAMPLE"
    assert last["SPY"]["lot_size_raw"] == 40 and last["SPY"]["sizes"]["bidsize"]["value"] == "40"
    assert call["multiplier"]["status"] == "VERIFIED_FROM_METADATA" and call["multiplier"]["value"] == 100
    greeks = put["greeks"]
    assert greeks["fields"]["delta"]["value"] == "-0.551"
    example = greeks["per_contract_example"]["values"]
    assert example["delta"]["value"] == "-55.100" and example["rho"]["status"] == "RAW_ONLY"
    assert example["theta"]["status"] == "PROVISIONAL"
    assert greeks["updated_at"]["interpretation"] == "NAIVE_TEXT_READ_AS_UTC"
    assert 2999 < greeks["updated_at"]["age"]["age_seconds"] < 3003   # own time, not receipt
    assert greeks["implied_volatility"]["mid_iv"]["display"] == "11%"
    pair = report["greek_pair_check"]
    assert "rho" in pair["identical_fields"] and pair["delta_call_minus_put"] == "1.000"
    v = report["verdicts"]
    assert v["stock_quote_freshness"]["verdict"] == "PASS" and v["option_quote_freshness"]["verdict"] == "PASS"
    assert v["option_size_interpretation"]["verdict"] == "PARTIAL"
    assert v["greek_normalization"]["verdict"] == "PARTIAL"
    assert v["greek_timestamp_interpretation"]["verdict"] == "PARTIAL"
    assert v["current_greek_state"]["verdict"] == "OBSERVATIONS_ONLY"
    assert v["reconnect_behavior"]["verdict"] == "NOT_RUN"
    assert report["decision_eligibility"] == "NOT_EVALUATED"
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for h in transport.headers)
    assert TOKEN not in json.dumps(report, default=str)


def test_off_hours_capture_records_schema_but_freshness_is_not_run():
    report, _ = run(start=CLOSED)
    assert report["verdicts"]["stock_quote_freshness"]["verdict"] == "NOT_RUN"
    assert report["verdicts"]["option_quote_freshness"]["reason"] == "NOT_A_REGULAR_SESSION_FOR_EVERY_ROUND"


def test_unchanged_provider_times_are_not_advancement_but_can_remain_within_policy():
    def frozen(_, rows):
        for row in rows:
            for k in ("bid_date", "ask_date", "trade_date"):
                row[k] = ms(RTH)
        return rows
    report, _ = run(quote_hook=frozen)
    assert report["advancing"]["SPY"]["state"] == "NOT_ADVANCED"
    assert report["verdicts"]["stock_quote_freshness"]["verdict"] == "PASS"


def test_http_200_fault_body_is_a_failure_and_stops_requests():
    report, transport = run(fault_on=1)
    assert report["status"] == "UNAVAILABLE" and report["reason"] == "REST_ERROR"
    assert len(transport.calls) == 1 and report["rounds"] == []
    assert report["verdicts"]["greek_normalization"]["verdict"] == "NOT_RUN"
    assert report["requests"]["log"][0]["outcome"] == "REST_ERROR"


def test_one_bad_option_does_not_discard_its_peer_or_prices():
    def bad(_, rows):
        for row in rows:
            if row["symbol"] == PUT:
                row["greeks"] = dict(row["greeks"], delta=True, updated_at="2026-10-07 10:00")
        return rows
    report, _ = run(quote_hook=bad)
    last = report["rounds"][-1]["observations"]
    assert last[PUT]["greeks"]["fields"]["delta"]["state"] == "INVALID_TYPE"
    assert last[PUT]["greeks"]["updated_at"]["state"] == "TIME_FORMAT_UNSUPPORTED"
    assert last[PUT]["prices"]["bid"]["value"] == "2.08"
    assert last[CALL]["greeks"]["fields"]["delta"]["value"] == "0.449"
    assert report["verdicts"]["greek_timestamp_interpretation"]["verdict"] == "FAIL"


def test_option_identity_mismatch_isolates_that_symbol():
    def wrong(_, rows):
        return [dict(r, strike=781.0) if r["symbol"] == PUT else r for r in rows]
    report, _ = run(quote_hook=wrong)
    assert report["rounds"][-1]["failures"][PUT] == "OPTION_IDENTITY_MISMATCH"
    assert CALL in report["rounds"][-1]["observations"]
    assert report["status"] == "PARTIAL_OBSERVATIONS"


def test_conflicting_or_missing_contract_size_gives_no_exposure():
    def conflict(_, rows):
        return [dict(r, contract_size=10) if r["symbol"] == CALL else
                {k: v for k, v in r.items() if k != "contract_size"} if r["symbol"] == PUT else r for r in rows]
    report, _ = run(quote_hook=conflict)
    last = report["rounds"][-1]["observations"]
    assert last[CALL]["multiplier"]["status"] == "CONFLICT"
    assert last[CALL]["greeks"]["per_contract_example"]["values"]["delta"]["status"] == "UNAVAILABLE"
    # the chain row still supplies 100 for the put: metadata, not a default
    assert last[PUT]["multiplier"]["value"] == 100


def test_nonstandard_root_is_not_given_a_verified_multiplier():
    def root(_, rows):
        return [dict(r, root_symbol="SPY1") if r["symbol"] == CALL else r for r in rows]
    report, _ = run(quote_hook=root)
    assert report["rounds"][-1]["observations"][CALL]["multiplier"]["status"] == "UNVERIFIED_NONSTANDARD_ROOT"


def test_seconds_magnitude_side_times_are_refused_not_rescaled():
    def seconds(_, rows):
        return [dict(r, bid_date=ms(RTH) // 1000) for r in rows]
    report, _ = run(quote_hook=seconds)
    assert report["rounds"][-1]["observations"]["SPY"]["times"]["bid_date"]["state"] == "TIME_UNIT_UNSUPPORTED"


def test_a_later_failed_round_leaves_no_current_result():
    def fail_second(n, rows):
        if n == 3:
            raise QuoteUnavailable("REST_HTTP_429")
        return rows
    report, transport = run(rounds=3, quote_hook=fail_second)
    assert report["status"] == "UNAVAILABLE" and report["current"] == "NONE_AFTER_FAILURE"
    assert report["verdicts"]["stock_quote_freshness"] == {"verdict": "FAIL", "reason": "RUN_STOPPED:REST_HTTP_429"}
    assert report["rounds"][-1] == {"round": 2, "failed": "REST_HTTP_429"}
    assert len(transport.calls) == 5


def test_request_plan_over_budget_makes_no_call():
    clock = Clock(RTH)
    transport = Fake(clock)
    client = tc.TradierClient(TOKEN, request=transport, clock=clock, max_requests=4)
    report = tc.diagnostic(client, ["SPY"], option_underlying="SPY", rounds=2, sleep=lambda s: None)
    assert report["reason"] == "REQUEST_PLAN_EXCEEDS_BUDGET" and transport.calls == []


def test_only_market_data_routes_are_allowed_and_budget_is_enforced():
    clock = Clock(RTH)
    client = tc.TradierClient(TOKEN, request=Fake(clock), clock=clock, max_requests=1)
    with pytest.raises(QuoteUnavailable, match="ROUTE_NOT_ALLOWED"):
        client.get("accounts", {})
    client.get("expirations", {"symbol": "SPY"})
    with pytest.raises(QuoteUnavailable, match="REST_REQUEST_BUDGET"):
        client.get("expirations", {"symbol": "SPY"})
    with pytest.raises(QuoteUnavailable):
        tc.TradierClient(TOKEN, max_requests=13)
    with pytest.raises(QuoteUnavailable, match="CREDENTIALS_MISSING"):
        tc.TradierClient("  ")


def test_cli_without_token_makes_no_call_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    out = tmp_path / "t.json"
    code = tc.main(["--environment", "production", "--symbols", "SPY", "--output", str(out)], env={})
    assert code == 1 and json.loads(out.read_text())["reason"] == "CREDENTIALS_MISSING"
    with pytest.raises(SystemExit):
        tc.main(["--environment", "production", "--symbols", "SPY", "--output", str(out)], env={})


def test_cli_report_guard_refuses_an_echoed_token(tmp_path, monkeypatch, capsys):
    def echo(client, *args, **kwargs):
        return {"status": "OBSERVATIONS_ONLY", "purpose": "x", "leak": client._token}
    monkeypatch.setattr(tc, "diagnostic", echo)
    out = tmp_path / "t.json"
    tc.main(["--environment", "production", "--symbols", "SPY", "--output", str(out)],
            env={"TRADIER_ACCESS_TOKEN": TOKEN})
    text = out.read_text()
    assert TOKEN not in text and json.loads(text)["status"] == "REPORT_REJECTED"
    assert TOKEN not in capsys.readouterr().out


def test_single_item_replies_are_accepted_in_their_documented_shapes():
    assert tc.listed("2026-10-08", text=True) == ["2026-10-08"]
    assert tc.listed({"symbol": "SPY"}) == [{"symbol": "SPY"}]
    with pytest.raises(QuoteUnavailable):
        tc.listed("SPY")   # text is only accepted where the route returns text items


def test_unmatched_and_missing_symbols_are_failures_not_observations():
    def drop(_, rows):
        return [r for r in rows if r["symbol"] != "QQQ"]
    report, _ = run(quote_hook=drop)
    assert report["rounds"][-1]["failures"]["QQQ"] == "MISSING"
    assert report["status"] == "PARTIAL_OBSERVATIONS"


def test_tradiers_published_quote_example_parses_under_the_conventions():
    # Verbatim rows from https://docs.tradier.com/docs/quotes (page updated 2025-09-15).
    stock = {"symbol": "NVDA", "description": "NVIDIA Corp", "exch": "Q", "type": "stock", "last": 175.23,
             "change": -2.59, "volume": 55438109, "open": 175.67, "high": 176.58, "low": 174.51, "close": None,
             "bid": 175.23, "ask": 175.24, "change_percentage": -1.46, "average_volume": 9573901,
             "last_volume": 100, "trade_date": 1757948508561, "prevclose": 177.82, "week_52_high": 184.48,
             "week_52_low": 86.62, "bidsize": 8, "bidexch": "Q", "bid_date": 1757948508000, "asksize": 5,
             "askexch": "P", "ask_date": 1757948508000, "root_symbols": "NVDA"}
    option = {"symbol": "NVDA250919C00175000", "description": "NVDA Sep 19 2025 $175.00 Call", "exch": "Z",
              "type": "option", "last": 2.87, "change": -1.78, "volume": 38156, "open": 3.25, "high": 3.7,
              "low": 2.58, "close": None, "bid": 2.86, "ask": 2.88, "underlying": "NVDA", "strike": 175.0,
              "greeks": {"delta": 0.5652816604132407, "gamma": 0.05728070476977678, "theta": -0.3341843583802417,
                         "vega": 0.07455884071440669, "rho": 0.011592433738162022, "phi": -0.011991846484704638,
                         "bid_iv": 0.354346, "mid_iv": 0.3577, "ask_iv": 0.361055, "smv_vol": 0.358,
                         "updated_at": "2025-09-15 13:59:03"},
              "change_percentage": -38.28, "average_volume": 0, "last_volume": 3, "trade_date": 1757948483351,
              "prevclose": 4.65, "week_52_high": 0.0, "week_52_low": 0.0, "bidsize": 239, "bidexch": "W",
              "bid_date": 1757948508000, "asksize": 17, "askexch": "Z", "ask_date": 1757948508000,
              "open_interest": 76023, "contract_size": 100, "expiration_date": "2025-09-19",
              "expiration_type": "standard", "option_type": "call", "root_symbol": "NVDA"}
    received = datetime(2025, 9, 15, 15, 1, 50, tzinfo=UTC)
    s = tc.observe_row(stock, dict(symbol="NVDA", wire="NVDA", kind="stock"), received, None)
    assert s["times"]["bid_date"]["utc"] == "2025-09-15T15:01:48+00:00" and s["sizes"]["bidsize"]["value"] == "8"
    o = tc.observe_row(option, dict(symbol="NVDA250919C00175000", wire="NVDA250919C00175000", kind="option"),
                       received, None)
    updated = o["greeks"]["updated_at"]
    assert updated["et"] == "2025-09-15 09:59:03 EDT" and updated["age"]["age_seconds"] == 3767.0
    assert o["sizes"]["bidsize"]["value"] == "239" and o["sizes"]["bidsize"]["interpreted_unit"] == "CONTRACTS_PROVISIONAL"
    assert Decimal(o["greeks"]["per_contract_example"]["values"]["delta"]["value"]) == Decimal("56.52816604132407")
    assert o["greeks"]["implied_volatility"]["smv_vol"]["display"] == "35.8%"
    assert o["exchanges"] == {"bidexch": "W", "askexch": "Z", "exch": "Z"}
