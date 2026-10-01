import json
from datetime import datetime, timezone
from urllib import error

import pytest

from desk.bars import BarDataError
from desk.webull import WebullData, WebullError, body_md5, sign

# Webull's own published signing example (developer.webull.com, authentication/signature).
# These are the documentation's sample values, not real keys.
DOC_QUERY = {"a1": "webull", "a2": "123", "a3": "xxx", "q1": "yyy"}
DOC_HEADERS = {"x-app-key": "776da210ab4a452795d74e726ebd74b6", "x-signature-algorithm": "HMAC-SHA1",
               "x-signature-version": "1.0", "x-signature-nonce": "48ef5afed43d4d91ae514aaeafbc29ba",
               "x-timestamp": "2022-01-04T03:55:31Z", "host": "api.webull.com"}
DOC_BODY = '{"k1":123,"k2":"this is the api request body","k3":true,"k4":{"foo":[1,2]}}'
DOC_SECRET = "0f50a2e853334a9aae1a783bee120c1f"


def test_signature_matches_webull_documented_example():
    assert body_md5(DOC_BODY) == "E296C96787E1A309691CEF3692F5EEDD"
    assert sign(path="/trade/place_order", query=DOC_QUERY, signing_headers=DOC_HEADERS,
                body=DOC_BODY, app_secret=DOC_SECRET) == "kvlS6opdZDhEBo5jq40nHYXaLvM="


def bar(t, c):
    return {"time": t, "open": str(c), "high": str(c + 1), "low": str(c - 1), "close": str(c), "volume": "100"}


class FakeTransport:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.requests = reply, exc, []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if self.exc:
            raise self.exc
        return json.dumps(self.reply).encode()


def client(transport, token=None):
    return WebullData("key", "secret", token, transport=transport, min_interval=0,
                      clock=lambda: datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc))


def test_bars_request_is_signed_and_parsed():
    # The reply shape Webull returned on 29 Sep 2026.
    t = FakeTransport({"result": [{"symbol": "SPY", "instrument_id": "1", "delay_minutes": 0,
                                   "result": [bar("2026-09-28T04:00:00.000+0000", 765.6),
                                              bar("2026-09-25T04:00:00.000+0000", 771.4)]}]})
    out = client(t, token="tok").bars(["SPY"], category="US_ETF", timespan="D", count=2)
    req = t.requests[0]
    assert req.get_method() == "POST" and req.full_url.endswith("/market-data/stocks/bars/list")
    headers = {k.lower(): v for k, v in req.header_items()}
    assert headers["x-app-key"] == "key" and headers["x-access-token"] == "tok"
    assert headers["x-timestamp"] == "2026-09-29T12:00:00Z" and headers["x-version"] == "v3"
    assert "secret" not in json.dumps(headers)
    body = req.data.decode()
    signing = {k: headers[k] for k in ("x-app-key", "x-signature-algorithm", "x-signature-version",
                                        "x-signature-nonce", "x-timestamp", "host")}
    assert headers["x-signature"] == sign(path="/market-data/stocks/bars/list", query={},
                                          signing_headers=signing, body=body, app_secret="secret")
    assert json.loads(body)["symbols"] == ["SPY"]
    assert out["SPY"]["close"].tolist() == [771.4, 765.6]


def test_missing_symbol_in_reply_fails_closed():
    t = FakeTransport([{"symbol": "SPY", "delay_minutes": 0, "result": [bar("2026-09-28T04:00:00.000+0000", 765.6)]}])
    with pytest.raises(WebullError, match="QQQ"):
        client(t).bars(["SPY", "QQQ"], category="US_ETF", timespan="D")


@pytest.mark.parametrize("exc", [
    error.HTTPError("u", 403, "Forbidden", {}, None),
    error.HTTPError("u", 429, "Too Many", {}, None),
    error.URLError("down"),
    TimeoutError(),
])
def test_http_failures_fail_closed(exc):
    with pytest.raises(BarDataError):
        client(FakeTransport(exc=exc)).bars(["SPY"], category="US_ETF", timespan="D")


def test_bad_replies_and_arguments_fail_closed():
    with pytest.raises(WebullError):
        client(FakeTransport({"error": "x"})).bars(["SPY"], category="US_ETF", timespan="D")
    with pytest.raises(WebullError):
        client(FakeTransport([])).bars([f"S{i}" for i in range(21)], category="US_STOCK", timespan="D")
    with pytest.raises(WebullError):
        client(FakeTransport([])).bars(["SPY"], category="US_ETF", timespan="H1")


def test_no_keys_means_no_client():
    with pytest.raises(WebullError, match="not set"):
        WebullData.from_env({})


def test_earnings_calendar_is_a_signed_get():
    t = FakeTransport([{"fiscal_year": 2026, "expected_publish_date": "2026-10-13"}])
    rows = client(t).earnings_calendar("JPM")
    assert rows[0]["expected_publish_date"] == "2026-10-13"
    assert t.requests[0].get_method() == "GET" and "symbol=JPM" in t.requests[0].full_url


def test_host_comes_from_env_and_is_limited_to_webull():
    env = {"WEBULL_APP_KEY": "k", "WEBULL_APP_SECRET": "s", "WEBULL_HOST": "api.sandbox.webull.com"}
    t = FakeTransport([{"expected_publish_date": "2026-10-13"}])
    WebullData.from_env(env, transport=t, min_interval=0).earnings_calendar("JPM")
    assert t.requests[0].full_url.startswith("https://api.sandbox.webull.com/")
    with pytest.raises(WebullError, match="WEBULL_HOST"):
        WebullData.from_env({**env, "WEBULL_HOST": "evil.example.com"})


def test_delayed_bars_fail_closed():
    t = FakeTransport({"result": [{"symbol": "SPY", "delay_minutes": 15,
                                   "result": [bar("2026-09-28T04:00:00.000+0000", 765.6)]}]})
    with pytest.raises(WebullError, match="15 minutes delayed"):
        client(t).bars(["SPY"], category="US_ETF", timespan="D")


def test_rankings_are_signed_gets_and_fail_closed():
    row = {"symbol": "NVDA", "price": "180.1", "change_ratio": "0.31"}
    for reply in ([row], {"data": [row]}):
        t = FakeTransport(reply)
        assert client(t).gainers("MONTH_3")[0]["symbol"] == "NVDA"
        assert "rank_type=MONTH_3" in t.requests[0].full_url and "/screeners/gainers-losers/list" in t.requests[0].full_url
    t = FakeTransport([row])
    client(t).most_active()
    assert "rank_type=TURNOVER" in t.requests[0].full_url
    with pytest.raises(WebullError):
        client(FakeTransport([{"price": "1"}])).most_active()
    with pytest.raises(WebullError):
        client(FakeTransport([])).gainers("MONTH_6")
