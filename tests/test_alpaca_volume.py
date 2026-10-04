"""G5 checkpoint 1: Alpaca SIP volume producer and acceptance probe.

All provider replies here are synthetic fixtures (labelled), not Alpaca data.
No test sends a network request.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pandas as pd
import pytest

from desk import alpaca_probe
from desk.alpaca_volume import (MAPPING, POLICY, AlpacaVolumeClient, AlpacaVolumeError, BarRequest,
                                HttpReply, RequestBudget, VolumeCache, VolumeObservation, definition_id,
                                ep_volume_component, evaluate, prior_sessions, revalidate, share_basis_id)
from desk.calendar import ET, session, sessions

KEY, SECRET = "fixture-key-id-0000", "fixture-secret-9999"
NOW = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)
ENTRY = date(2026, 10, 2)
SYMBOLS = ("NVDA", "SPY", "QQQ", "AAPL")


def z(stamp) -> str:
    return pd.Timestamp(stamp).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def daily_rows(first: date, last: date, volume=lambda i: 1000) -> list[dict]:
    return [{"t": z(pd.Timestamp(d).tz_localize(ET)), "o": 1, "h": 1, "l": 1, "c": 1, "v": volume(i), "n": 1}
            for i, d in enumerate(sessions(first, last))]


def rth_rows(day: date, volumes=(300, 200)) -> list[dict]:
    opened = session(day)[0]
    return [{"t": z(opened + timedelta(minutes=15 * i)), "v": v} for i, v in enumerate(volumes)]


def page(bars: dict, token=None) -> HttpReply:
    body = json.dumps({"bars": bars, "next_page_token": token}).encode()
    return HttpReply(200, {"x-request-id": "fixture"}, body)


class Transport:
    """Synthetic provider: returns queued replies and records what was sent."""

    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def __call__(self, req, timeout):
        self.sent.append(req)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def client(transport, *, budget=6, cache=None, now=NOW):
    return AlpacaVolumeClient(KEY, SECRET, budget=RequestBudget(budget), transport=transport,
                              clock_fn=lambda: now, cache=cache)


def requests_for(symbols=SYMBOLS, adjustment="split", entry=ENTRY, start=date(2026, 7, 1)):
    return alpaca_probe.windows(entry, start, symbols, adjustment)


def healthy_pages(symbols=SYMBOLS, entry=ENTRY):
    first, last = date(2026, 7, 1), prior_sessions(entry)[-1]
    return (page({s: daily_rows(first, last) for s in symbols}), page({s: rth_rows(entry) for s in symbols}))


def fetch_both(transport, symbols=SYMBOLS, adjustment="split", **kw):
    c = client(transport, **kw)
    daily_req, rth_req = requests_for(symbols, adjustment)
    return c, c.fetch(daily_req), c.fetch(rth_req)


def observation(timeframe, bars, *, adjustment="split", symbol="NVDA", start=None, end=None, received=NOW):
    channel = "native-daily" if timeframe == "1Day" else "rth-m15"
    values = dict(requested_symbol=symbol, returned_symbol=symbol, provider_identity=None, mapping_provenance=MAPPING,
                  feed="sip", adjustment=adjustment, timeframe=timeframe, channel=channel,
                  definition_id=definition_id(adjustment, channel), share_basis_id=share_basis_id(adjustment),
                  requested_start=start or datetime(2000, 1, 3, tzinfo=timezone.utc),
                  requested_end=end or datetime(2028, 1, 1, tzinfo=timezone.utc),
                  first_sent_at=received, received_at=received,
                  bars=tuple((r["t"], Decimal(r["v"])) for r in bars), page_digests=("fixture",))
    return VolumeObservation(**values, content_digest=VolumeObservation.content(values))


# ---------------------------------------------------------------- request ----

def test_probe_windows_match_the_handoff_bounds():
    daily, rth = requests_for()
    assert dict(daily.params()) == {"symbols": "NVDA,SPY,QQQ,AAPL", "timeframe": "1Day",
                                    "start": "2026-07-01T04:00:00Z", "end": "2026-10-02T03:59:59Z",
                                    "limit": "10000", "adjustment": "split", "feed": "sip", "sort": "asc"}
    assert dict(rth.params())["start"] == "2026-10-02T13:30:00Z"
    assert dict(rth.params())["end"] == "2026-10-02T13:59:59Z"
    assert dict(rth.params())["timeframe"] == "15Min"


def test_requests_use_documented_headers_host_and_explicit_sip():
    transport = Transport(*healthy_pages())
    _, daily, _ = fetch_both(transport)
    sent = transport.sent[0]
    assert sent.full_url.startswith("https://data.alpaca.markets/v2/stocks/bars?")
    assert "feed=sip" in sent.full_url and "adjustment=split" in sent.full_url
    assert sent.get_header("Apca-api-key-id") == KEY and sent.get_header("Apca-api-secret-key") == SECRET
    text = json.dumps(daily.report())
    assert KEY not in text and SECRET not in text


def test_request_model_refuses_other_feeds_limits_and_bad_symbols():
    base = dict(timeframe="1Day", start=NOW - timedelta(days=9), end=NOW - timedelta(days=1), adjustment="split")
    for bad in (dict(feed="iex"), dict(limit=1000), dict(sort="desc"), dict(symbols=("nvda",)),
                dict(symbols=("SPY", "SPY")), dict(adjustment="all")):
        with pytest.raises(ValueError):
            BarRequest(**{"symbols": ("SPY",), **base, **bad})


def test_end_must_be_15_minutes_old_and_nothing_is_sent_otherwise():
    transport = Transport()
    c = client(transport, now=datetime(2026, 10, 2, 14, 5, tzinfo=timezone.utc))
    result = c.fetch(requests_for()[1])
    assert result.error == "END_NOT_15_MINUTES_OLD" and transport.sent == []


def test_not_configured_without_both_keys():
    with pytest.raises(AlpacaVolumeError, match="NOT_CONFIGURED"):
        AlpacaVolumeClient.from_env({"APCA_API_KEY_ID": KEY}, budget=RequestBudget(6))


# ----------------------------------------------------- complete / partial ----

def test_complete_reply_gives_each_ticker_a_volume_component():
    _, daily, rth = fetch_both(Transport(*healthy_pages()))
    assert daily.status == rth.status == "COMPLETE" and len(daily.pages) == len(rth.pages) == 1
    out = evaluate(ENTRY, daily, rth)
    assert set(out) == set(SYMBOLS)
    c = out["SPY"]
    assert c.prior_count == 50 and c.prior_sessions[0] == date(2026, 7, 23) and c.prior_sessions[-1] == date(2026, 10, 1)
    assert c.prior50_average == Decimal(1000) and c.first30_volume == Decimal(500)
    assert c.ratio == Decimal("0.5") and c.threshold == Decimal("0.5") and c.threshold_met
    assert c.first30_intervals == (("2026-10-02T13:30:00Z", Decimal(300)), ("2026-10-02T13:45:00Z", Decimal(200)))
    assert c.policy == POLICY.id and c.numerator_definition != c.denominator_definition


def test_paginated_reply_is_followed_and_joined():
    first, last = date(2026, 7, 1), date(2026, 10, 1)
    rows = daily_rows(first, last)
    transport = Transport(page({"AAPL": rows[:40]}, token="tok1"), page({"AAPL": rows[40:], "SPY": rows}),
                          page({s: rth_rows(ENTRY) for s in ("AAPL", "SPY")}))
    _, daily, rth = fetch_both(transport, ("AAPL", "SPY"))
    assert daily.status == "COMPLETE" and len(daily.pages) == 2
    assert "page_token=tok1" in transport.sent[1].full_url
    assert len(daily.observations["AAPL"].bars) == len(rows)
    assert len(daily.observations["AAPL"].page_digests) == 2
    assert all(isinstance(v, object) and not isinstance(v, str) for v in evaluate(ENTRY, daily, rth).values())


def test_missing_symbol_is_isolated():
    first, last = date(2026, 7, 1), date(2026, 10, 1)
    transport = Transport(page({s: daily_rows(first, last) for s in ("SPY", "QQQ", "AAPL")}),
                          page({s: rth_rows(ENTRY) for s in SYMBOLS}))
    _, daily, rth = fetch_both(transport)
    assert daily.status == "PARTIAL" and daily.failures == {"NVDA": "SYMBOL_MISSING"}
    out = evaluate(ENTRY, daily, rth)
    assert out["NVDA"] == "SYMBOL_MISSING" and not isinstance(out["SPY"], str)


@pytest.mark.parametrize("mutate, code", [
    (lambda rows: rows + [dict(rows[-1])], "DUPLICATE_TIMESTAMP"),
    (lambda rows: rows[:-1] + [{**rows[-1], "v": -1}], "NONFINITE_OR_NEGATIVE_VOLUME"),
    (lambda rows: rows[:-1] + [{**rows[-1], "v": float("nan")}], "NONFINITE_OR_NEGATIVE_VOLUME"),
    (lambda rows: rows[:-1] + [{**rows[-1], "v": True}], "MALFORMED_VOLUME"),
    (lambda rows: rows[:-1] + [{**rows[-1], "v": "1000"}], "MALFORMED_VOLUME"),
    (lambda rows: rows[:-1] + [{"t": rows[-1]["t"]}], "MALFORMED_BAR"),
    (lambda rows: list(reversed(rows)), "UNSORTED_BARS"),
    (lambda rows: rows + [{"t": "2026-10-03T04:00:00Z", "v": 5}], "OUTSIDE_REQUEST_BOUNDS"),
    (lambda rows: [{"t": "2026-07-04T04:00:00Z", "v": 5}] + rows[2:], "WRONG_CHANNEL_ROW"),   # holiday
    (lambda rows: [{"t": "2026-07-05T04:00:00Z", "v": 5}] + rows[2:], "WRONG_CHANNEL_ROW"),   # Sunday
    (lambda rows: [{"t": "2026-07-01T13:30:00Z", "v": 5}] + rows[1:], "WRONG_CHANNEL_ROW"),   # not a daily stamp
])
def test_malformed_ticker_does_not_erase_healthy_tickers(mutate, code):
    first, last = date(2026, 7, 1), date(2026, 10, 1)
    rows = daily_rows(first, last)
    bars = {"NVDA": mutate(rows), "SPY": rows, "QQQ": rows, "AAPL": rows}
    body = json.dumps({"bars": bars, "next_page_token": None}).encode()
    transport = Transport(HttpReply(200, {}, body), page({s: rth_rows(ENTRY) for s in SYMBOLS}))
    _, daily, rth = fetch_both(transport)
    assert daily.failures == {"NVDA": code} and set(daily.observations) == {"SPY", "QQQ", "AAPL"}
    out = evaluate(ENTRY, daily, rth)
    assert out["NVDA"] == code and all(not isinstance(out[s], str) for s in ("SPY", "QQQ", "AAPL"))


def test_unrequested_symbol_is_never_attributed_to_a_requested_ticker():
    pages = healthy_pages(("SPY",))
    extra = json.loads(pages[0].body)
    extra["bars"]["SPYX"] = extra["bars"]["SPY"]
    _, daily, _ = fetch_both(Transport(HttpReply(200, {}, json.dumps(extra).encode()), pages[1]), ("SPY",))
    assert daily.issues == ("UNREQUESTED_SYMBOL",) and set(daily.observations) == {"SPY"}


def test_pre_market_bar_is_a_wrong_channel_row_for_rth():
    rows = rth_rows(ENTRY)
    req = BarRequest(symbols=("SPY",), timeframe="15Min", adjustment="split",
                     start=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
                     end=datetime(2026, 10, 2, 13, 59, 59, tzinfo=timezone.utc))
    result = client(Transport(page({"SPY": [{"t": "2026-10-02T13:15:00Z", "v": 7}] + rows}))).fetch(req)
    assert result.failures == {"SPY": "WRONG_CHANNEL_ROW"}


def test_off_grid_rth_bar_is_a_wrong_channel_row():
    result = client(Transport(page({"SPY": [{"t": "2026-10-02T13:31:00Z", "v": 7}]}))).fetch(requests_for(("SPY",))[1])
    assert result.failures == {"SPY": "WRONG_CHANNEL_ROW"}


def test_currency_or_envelope_problems_make_the_request_unavailable():
    for body, code in ((b"not json", "INVALID_JSON"), (b"[]", "INVALID_ENVELOPE"),
                       (json.dumps({"bars": [], "next_page_token": None}).encode(), "INVALID_ENVELOPE"),
                       (json.dumps({"bars": {}, "next_page_token": ""}).encode(), "INVALID_ENVELOPE"),
                       (json.dumps({"bars": {}, "next_page_token": None, "currency": "EUR"}).encode(), "CURRENCY_MISMATCH")):
        result = client(Transport(HttpReply(200, {}, body))).fetch(requests_for(("SPY",))[0])
        assert result.status == "UNAVAILABLE" and result.error == code and result.failures == {"SPY": code}


# ----------------------------------------------------------- stop / budget ----

@pytest.mark.parametrize("status, code", [(401, "AUTH_OR_ENTITLEMENT_FAILURE"), (403, "AUTH_OR_ENTITLEMENT_FAILURE"),
                                          (429, "RATE_LIMITED")])
def test_auth_entitlement_and_rate_limit_stop_the_run(status, code, tmp_path):
    transport = Transport(HttpReply(status, {"x-request-id": "fixture"}, b""))
    c = client(transport)
    budget = c._budget
    report = alpaca_probe.run(SYMBOLS, ENTRY, date(2026, 7, 1), tmp_path, client=c, budget=budget)
    assert len(transport.sent) == 1 and report["stopped"] == code and report["http_requests"] == 1
    assert report["requests"]["D"]["error"] == code and "RTH30" not in report["requests"]
    assert all(v["status"] == "UNAVAILABLE" for v in report["tickers"].values())
    assert c.fetch(requests_for()[1]).error == "RUN_STOPPED_AFTER_" + code and len(transport.sent) == 1
    saved = (tmp_path / "result.json").read_text()
    assert KEY not in saved and SECRET not in saved


def test_other_http_and_transport_failures_are_unavailable_not_stops():
    c = client(Transport(HttpReply(500, {}, b""), OSError("fixture")))
    assert c.fetch(requests_for()[0]).error == "HTTP_FAILURE" and c.stopped is None
    assert c.fetch(requests_for()[1]).error == "TRANSPORT_FAILURE"


def test_remaining_pagination_is_incomplete_not_a_truncated_success():
    rows = daily_rows(date(2026, 7, 1), date(2026, 10, 1))
    transport = Transport(page({"SPY": rows[:20]}, "a"), page({"SPY": rows[20:40]}, "b"), page({"SPY": rows[40:]}))
    result = client(transport, budget=2).fetch(requests_for(("SPY",))[0])
    assert result.status == "UNAVAILABLE" and result.error == "INCOMPLETE_PAGINATION"
    assert result.observations == {} and len(transport.sent) == 2


def test_pagination_loop_is_refused():
    rows = daily_rows(date(2026, 7, 1), date(2026, 10, 1))
    transport = Transport(page({"SPY": rows[:20]}, "a"), page({"SPY": rows[20:]}, "a"))
    assert client(transport).fetch(requests_for(("SPY",))[0]).error == "PAGINATION_LOOP"


def test_probe_never_exceeds_six_requests():
    rows = daily_rows(date(2026, 7, 1), date(2026, 10, 1))
    transport = Transport(*[page({"SPY": rows[i:i + 10]}, f"t{i}") for i in range(0, 70, 10)])
    c = client(transport)
    daily = c.fetch(requests_for(("SPY",))[0])
    rth = c.fetch(requests_for(("SPY",))[1])
    assert len(transport.sent) == 6 and daily.error == "INCOMPLETE_PAGINATION"
    assert rth.error == "REQUEST_BUDGET_EXHAUSTED"


def test_credential_echo_is_refused_and_not_saved(tmp_path):
    body = json.dumps({"bars": {}, "next_page_token": None, "message": SECRET}).encode()
    c = client(Transport(HttpReply(200, {}, body), *healthy_pages()[1:]))
    report = alpaca_probe.run(SYMBOLS, ENTRY, date(2026, 7, 1), tmp_path, client=c, budget=c._budget)
    assert report["requests"]["D"]["error"] == "UNSAFE_RESPONSE"
    assert not (tmp_path / "raw" / "D_page1.json").exists()
    assert SECRET not in (tmp_path / "result.json").read_text()


# ------------------------------------------------------------ calculation ----

def _daily(entry=ENTRY, first=date(2026, 7, 1), last=None, adjustment="split", volume=lambda i: 1000, **kw):
    return observation("1Day", daily_rows(first, last or prior_sessions(entry)[-1], volume), adjustment=adjustment, **kw)


def _rth(entry=ENTRY, volumes=(300, 200), adjustment="split", **kw):
    return observation("15Min", rth_rows(entry, volumes), adjustment=adjustment, **kw)


def test_exactly_50_sessions_pass_and_49_fail():
    prior = prior_sessions(ENTRY)
    assert ep_volume_component(ENTRY, _daily(first=prior[0]), _rth()).prior_count == 50
    with pytest.raises(AlpacaVolumeError, match="MISSING_DAILY_SESSIONS"):
        ep_volume_component(ENTRY, _daily(first=prior[1]), _rth())


def test_gap_inside_the_window_is_not_zero_filled():
    prior = prior_sessions(ENTRY)
    rows = [r for r in daily_rows(prior[0], prior[-1]) if not r["t"].startswith(str(prior[25]))]
    with pytest.raises(AlpacaVolumeError, match="MISSING_DAILY_SESSIONS"):
        ep_volume_component(ENTRY, observation("1Day", rows), _rth())


def test_entry_session_row_is_excluded_from_the_baseline():
    big = lambda i: 10**9 if i == len(sessions(date(2026, 7, 1), ENTRY)) - 1 else 1000
    daily = _daily(last=ENTRY, volume=big)
    assert daily.bars[-1][0] == "2026-10-02T04:00:00Z" and daily.bars[-1][1] == 10**9
    c = ep_volume_component(ENTRY, daily, _rth())
    assert ENTRY not in c.prior_sessions and c.prior50_average == 1000


def test_stale_observations_fail_despite_a_fresh_receipt():
    with pytest.raises(AlpacaVolumeError, match="MISSING_RTH_INTERVALS"):
        ep_volume_component(ENTRY, _daily(), _rth(entry=date(2026, 10, 1)))
    with pytest.raises(AlpacaVolumeError, match="MISSING_DAILY_SESSIONS"):
        ep_volume_component(ENTRY, _daily(last=date(2026, 9, 29)), _rth())
    with pytest.raises(AlpacaVolumeError, match="MISSING_RTH_INTERVALS"):
        ep_volume_component(ENTRY, _daily(), _rth(volumes=(300,)))


def test_receipt_before_the_intervals_complete_is_refused():
    early = datetime(2026, 10, 2, 13, 50, tzinfo=timezone.utc)
    with pytest.raises(AlpacaVolumeError, match="RTH_NOT_COMPLETED_AT_RECEIPT"):
        ep_volume_component(ENTRY, _daily(), _rth(received=early))
    evening = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
    with pytest.raises(AlpacaVolumeError, match="DAILY_NOT_COMPLETED_AT_RECEIPT"):
        ep_volume_component(ENTRY, _daily(received=evening), _rth())


def test_dst_change_and_holidays_use_exchange_sessions():
    entry = date(2026, 11, 30)  # after DST ends (Nov 1) and Thanksgiving (Nov 26)
    prior = prior_sessions(entry)
    assert date(2026, 11, 26) not in prior and date(2026, 11, 27) in prior and len(prior) == 50
    later = datetime(2026, 12, 1, 2, 0, tzinfo=timezone.utc)
    daily = _daily(entry=entry, first=prior[0], received=later)
    stamps = dict(daily.bars)
    assert "2026-10-30T04:00:00Z" in stamps and "2026-11-02T05:00:00Z" in stamps
    rth = _rth(entry=entry, received=later)
    assert [t for t, _ in rth.bars] == ["2026-11-30T14:30:00Z", "2026-11-30T14:45:00Z"]
    assert ep_volume_component(entry, daily, rth).threshold_met
    edt_stamps = observation("15Min", [{"t": "2026-11-30T13:30:00Z", "v": 300}, {"t": "2026-11-30T13:45:00Z", "v": 200}],
                              received=later)
    with pytest.raises(AlpacaVolumeError, match="MISSING_RTH_INTERVALS"):
        ep_volume_component(entry, daily, edt_stamps)


def test_shortened_session_rows_after_the_early_close_are_wrong_channel():
    req = BarRequest(symbols=("SPY",), timeframe="15Min", adjustment="split",
                     start=datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc),
                     end=datetime(2026, 11, 27, 21, 0, tzinfo=timezone.utc))
    after_close = [{"t": "2026-11-27T14:30:00Z", "v": 1}, {"t": "2026-11-27T18:00:00Z", "v": 1}]
    later = datetime(2026, 11, 28, tzinfo=timezone.utc)
    assert client(Transport(page({"SPY": after_close})), now=later).fetch(req).failures == {"SPY": "WRONG_CHANNEL_ROW"}


def test_threshold_boundary_is_exact_decimal():
    assert ep_volume_component(ENTRY, _daily(), _rth(volumes=(250, 250))).threshold_met
    below = ep_volume_component(ENTRY, _daily(), _rth(volumes=(250, 249)))
    assert not below.threshold_met and below.ratio == Decimal("0.499")
    # A non-integer average: 50 sessions totalling 50,001 shares; half is 500.01.
    odd = _daily(volume=lambda i: 1001 if i == 64 else 1000)
    assert ep_volume_component(ENTRY, odd, _rth(volumes=(300, 200))).threshold_met is False
    assert ep_volume_component(ENTRY, odd, _rth(volumes=(300, 201))).threshold_met is True


def test_zero_baseline_is_unavailable():
    with pytest.raises(AlpacaVolumeError, match="ZERO_BASELINE"):
        ep_volume_component(ENTRY, _daily(volume=lambda i: 0), _rth())


def test_wrong_channel_and_identity_are_refused():
    with pytest.raises(AlpacaVolumeError, match="WRONG_CHANNEL"):
        ep_volume_component(ENTRY, _rth(), _daily())
    with pytest.raises(AlpacaVolumeError, match="IDENTITY_MISMATCH"):
        ep_volume_component(ENTRY, _daily(), _rth(symbol="SPY"))


def test_raw_and_split_evidence_never_share_a_basis():
    with pytest.raises(AlpacaVolumeError, match="SHARE_BASIS_MISMATCH"):
        ep_volume_component(ENTRY, _daily(adjustment="raw"), _rth())
    with pytest.raises(AlpacaVolumeError, match="SHARE_BASIS_MISMATCH"):
        ep_volume_component(ENTRY, _daily(), _rth(adjustment="raw"))
    with pytest.raises(AlpacaVolumeError, match="RAW_DIAGNOSTIC_ONLY"):
        ep_volume_component(ENTRY, _daily(adjustment="raw"), _rth(adjustment="raw"))
    assert share_basis_id("raw") != share_basis_id("split")
    assert definition_id("raw", "native-daily") != definition_id("split", "native-daily")


def test_observation_refuses_inconsistent_labels_or_digest():
    good = _daily()
    data = good.model_dump()
    for change in (dict(feed="iex"), dict(channel="rth-m15"), dict(share_basis_id=share_basis_id("raw")),
                   dict(definition_id=definition_id("split", "rth-m15")), dict(content_digest="0" * 64)):
        with pytest.raises(ValueError):
            VolumeObservation(**{**data, **change})


def test_known_split_raw_versus_split_adjusted_offline_fixture():
    """SYNTHETIC: NVDA's 10-for-1 split took effect 2024-06-10 (Sourced: NVIDIA press
    release, 2024-05-22). Volumes are made up; only the shape of the two bases matters."""
    entry = date(2024, 6, 12)
    prior = prior_sessions(entry)
    split_day = date(2024, 6, 10)
    raw = lambda i: 40_000 if prior[i] < split_day else 400_000
    adjusted = lambda i: 400_000 + i  # provider-adjusted values, retained exactly
    raw_daily = _daily(entry=entry, first=prior[0], adjustment="raw", volume=raw)
    split_daily = _daily(entry=entry, first=prior[0], volume=adjusted)
    split_rth = _rth(entry=entry, volumes=(150_000, 60_000))
    c = ep_volume_component(entry, split_daily, split_rth)
    assert c.prior50_total == sum(Decimal(400_000 + i) for i in range(50))  # no home-made multiplier
    assert c.threshold_met is True
    with pytest.raises(AlpacaVolumeError, match="SHARE_BASIS_MISMATCH"):
        ep_volume_component(entry, raw_daily, split_rth)
    with pytest.raises(AlpacaVolumeError, match="RAW_DIAGNOSTIC_ONLY"):
        ep_volume_component(entry, raw_daily, _rth(entry=entry, adjustment="raw"))


# ------------------------------------------------------------------ cache ----

def test_same_content_is_reused_without_a_request(tmp_path):
    cache = VolumeCache(tmp_path / "v.db")
    _, daily, rth = fetch_both(Transport(*healthy_pages()), cache=cache)
    assert {v["status"] for v in daily.cache_status.values()} == {"NEW"}
    later = NOW + timedelta(hours=1)
    _, again, _ = fetch_both(Transport(*healthy_pages()), cache=cache, now=later)
    assert {v["status"] for v in again.cache_status.values()} == {"UNCHANGED"}
    transport = Transport()
    reused = client(transport, cache=cache, now=later).fetch(requests_for()[0], reuse=True)
    # The stored snapshot is the first receipt of that content; later confirmations
    # only advance the cache's last-received time.
    assert reused.from_cache and transport.sent == [] and reused.observations == daily.observations
    assert reused.observations["SPY"].content_digest == again.observations["SPY"].content_digest


def test_changed_content_is_a_revision_and_invalidates_an_earlier_result(tmp_path):
    cache = VolumeCache(tmp_path / "v.db")
    _, daily, rth = fetch_both(Transport(*healthy_pages()), cache=cache)
    old = ep_volume_component(ENTRY, daily.observations["SPY"], rth.observations["SPY"])
    pages = healthy_pages()
    changed = json.loads(pages[0].body)
    changed["bars"]["SPY"][-1]["v"] = 1234
    later = NOW + timedelta(hours=1)
    _, revised, rth2 = fetch_both(Transport(HttpReply(200, {}, json.dumps(changed).encode()), pages[1]),
                                  cache=cache, now=later)
    assert revised.cache_status["SPY"] == {"status": "REVISED", "revision": 1,
                                           "changed_bars": ["2026-10-01T04:00:00Z"]}
    assert revised.cache_status["QQQ"]["status"] == "UNCHANGED"
    assert cache.revisions(requests_for()[0], "SPY")[0]["changed_bars"] == ["2026-10-01T04:00:00Z"]
    with pytest.raises(AlpacaVolumeError, match="VOLUME_EVIDENCE_REVISED"):
        revalidate(old, revised.observations["SPY"], rth2.observations["SPY"])
    revalidate(old, daily.observations["SPY"], rth.observations["SPY"])  # unchanged inputs still match


def test_identity_change_and_clock_regression_are_unavailable(tmp_path):
    cache = VolumeCache(tmp_path / "v.db")
    first = _daily()
    cache.record(requests_for()[0], first)
    moved = first.model_dump()
    moved["provider_identity"] = "fixture-other-asset"
    moved["content_digest"] = VolumeObservation.content(moved)
    with pytest.raises(AlpacaVolumeError, match="IDENTITY_CHANGED"):
        cache.record(requests_for()[0], VolumeObservation(**moved))
    earlier = _daily(received=NOW - timedelta(hours=1))
    with pytest.raises(AlpacaVolumeError, match="CLOCK_MOVED_BACKWARDS"):
        cache.record(requests_for()[0], earlier)


def test_failures_are_kept_per_ticker_in_the_cache(tmp_path):
    cache = VolumeCache(tmp_path / "v.db")
    first, last = date(2026, 7, 1), date(2026, 10, 1)
    transport = Transport(page({s: daily_rows(first, last) for s in ("SPY",)}))
    client(transport, cache=cache).fetch(requests_for(("SPY", "QQQ"))[0])
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(tmp_path / "v.db")) as db:
        assert db.execute("SELECT symbol, state, code FROM events ORDER BY sequence").fetchall() == [
            ("SPY", "OK", None), ("QQQ", "FAILED", "SYMBOL_MISSING")]


# ------------------------------------------------------------ probe / scope ----

def test_probe_writes_sanitized_evidence(tmp_path):
    c = client(Transport(*healthy_pages()))
    report = alpaca_probe.run(SYMBOLS, ENTRY, date(2026, 7, 1), tmp_path, client=c, budget=c._budget)
    assert report["http_requests"] == 2 and report["stopped"] is None
    assert {v["status"] for v in report["tickers"].values()} == {"AVAILABLE"}
    assert (tmp_path / "raw" / "D_page1.json").exists() and (tmp_path / "raw" / "RTH30_page1.json").exists()
    saved = (tmp_path / "result.json").read_text()
    assert KEY not in saved and SECRET not in saved and "APCA" not in saved


def test_probe_cli_refuses_without_keys(capsys, tmp_path):
    code = alpaca_probe.main(["--symbols", "SPY", "--entry-session", "2026-10-02", "--daily-start", "2026-07-01",
                              "--out", str(tmp_path)], env={})
    assert code == 2 and json.loads(capsys.readouterr().out) == {"status": "NOT_RUN", "code": "NOT_CONFIGURED"}


def test_daily_start_must_cover_the_first_required_session():
    with pytest.raises(AlpacaVolumeError, match="DAILY_START_AFTER_FIRST_REQUIRED_SESSION"):
        alpaca_probe.windows(ENTRY, date(2026, 7, 24), ("SPY",), "split")


def test_producer_is_not_wired_into_any_consumer():
    root = Path(__file__).resolve().parents[1] / "src" / "desk"
    users = [p.name for p in root.rglob("*.py") if "alpaca_volume" in p.read_text()]
    assert users == ["alpaca_probe.py"]
