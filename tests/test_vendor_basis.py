"""G3 synthetic vendor observations; no universal action or feed guarantees."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
import sqlite3

import pandas as pd
import pytest

from desk import scanner as sc
from desk.action_source import configured_source
from desk.bar_contract import completed_daily, completed_intraday, check_price_scale
from desk.bars import BarDataError
from desk.calendar import ET, session
from desk.data_basis import PriceHistoryChanged, price_basis, volume_basis
from desk.signal_state import SignalStore
from desk.vendor_basis import VendorBasisSource, VendorHistoryStore
from tests.basis_support import price_evidence
from tests.test_scanner import sig

NOW = datetime(2026,9,29,10,tzinfo=ET)
HOST = "api.sandbox.webull.com"


class Native:
    def __init__(self):
        self.now = NOW
        self.ids = {}
        self.factor = {}
        self.gap = {}
        self.bad = {}
        self.calls = []
        self.missing = set()
        self.outage = False

    def security_metadata(self,symbols):
        return [dict(symbol=s,instrument_id=self.ids.get(s,"id:"+s),name=s,currency="USD",
                     category="US_STOCK",sub_category="ETF" if s in {"SPY","QQQ"} else "COMMON_STOCK",
                     exchange_code="TEST",observed_at=self.now.isoformat()) for s in symbols]

    def bars(self,symbols,*,timespan,category,count=1000,**kwargs):
        self.calls.append((tuple(symbols),timespan,kwargs))
        if self.outage:
            raise BarDataError("sensitive provider response must not be logged")
        out = {}
        for symbol in symbols:
            if symbol in self.missing:
                continue
            factor = self.factor.get(symbol,1)
            if timespan == "D":
                idx = pd.DatetimeIndex(["2026-09-24","2026-09-25","2026-09-28"],tz=ET).tz_convert("UTC")
                rows = [[990*factor,1010*factor,980*factor,1000*factor,10000]]*3
            elif "start_time" in kwargs and pd.Timestamp(kwargs["start_time"],unit="ms",tz="UTC").tz_convert(ET).date() < NOW.date():
                opened,closed = session(NOW.date()-timedelta(days=1))
                idx = pd.date_range(opened,closed-pd.Timedelta(minutes=15),freq="15min").tz_convert("UTC")
                anchor_factor = factor + 0.01 if self.bad.get(symbol) == "mismatch" else factor
                rows = [[990*anchor_factor,1010*anchor_factor,980*anchor_factor,1000*anchor_factor,1000]]*len(idx)
            else:
                idx = pd.date_range(pd.Timestamp("2026-09-29 09:30",tz=ET),periods=2,freq="15min").tz_convert("UTC")
                scale = factor*self.gap.get(symbol,1)
                rows = [[990*scale,1010*scale,980*scale,1000*scale,1000],
                        [1000*scale,1020*scale,990*scale,1015*scale,1000]]
            frame = pd.DataFrame(rows,index=idx,columns=["open","high","low","close","volume"])
            if self.bad.get(symbol) == "volume" and timespan == "D":
                frame.iloc[0,4] += 1
            if self.bad.get(symbol) == "missing_history" and timespan == "D":
                frame = frame.iloc[1:].copy()
            if self.bad.get(symbol) == "incomplete_anchor" and "start_time" in kwargs:
                frame = frame.iloc[1:].copy()
            frame.attrs = {"webull_host":HOST,"provider_identity":{"symbol":symbol,"instrument_id":self.ids.get(symbol,"id:"+symbol)},
                "received_at":self.now.isoformat(),"bar_provenance":{"delay_minutes":0},
                "webull_request":{"timespan":timespan,"category":category,**({"trading_sessions":"RTH"} if timespan=="M15" else {})},
                "provider_sessions":["RTH"]}
            if self.bad.get(symbol) == "host":
                frame.attrs["webull_host"] = "api.webull.com"
            if self.bad.get(symbol) == "identity":
                frame.attrs["provider_identity"]["instrument_id"] = "wrong"
            if self.bad.get(symbol) == "delayed":
                frame.attrs["bar_provenance"]["delay_minutes"] = 15
            if self.bad.get(symbol) == "future":
                frame.attrs["received_at"] = (self.now+timedelta(seconds=1)).isoformat()
            if self.bad.get(symbol) == "stale":
                frame.attrs["received_at"] = (self.now-timedelta(seconds=61)).isoformat()
            out[symbol] = frame
        return out


def source(tmp_path,native=None,fallback=None):
    native = native or Native()
    return VendorBasisSource(native,VendorHistoryStore(tmp_path/"vendor.sqlite"),host=HOST,
                             clock_fn=lambda:native.now,fallback=fallback)


def daily(src,symbol="LEAD"):
    return completed_daily(src.bars([symbol],timespan="D",category="US_STOCK")[symbol],src._clock())


def minutes(src,symbol="LEAD"):
    return completed_intraday(src.bars([symbol],timespan="M15",category="US_STOCK",count=40)[symbol],src._clock())


def test_unenrolled_ticker_gets_price_basis_without_fabricated_action_or_volume_coverage(tmp_path):
    src = source(tmp_path)
    d,m = daily(src),minutes(src)
    old = price_basis(d,NOW).model_dump(mode="json")
    assert old["method"] == "webull-history-v1" and "coverage_complete" not in old
    assert check_price_scale(old,m,NOW,symbol="LEAD")
    with pytest.raises(BarDataError,match="Unverified volume"):
        volume_basis(d)
    assert src.store.latest(HOST,"LEAD")["status"] == "CONSISTENT"


@pytest.mark.parametrize("factor", [0.1,0.999,0.8,10])
def test_split_and_ordinary_or_special_dividend_revisions_cannot_validate_old_signal(tmp_path,factor):
    src = source(tmp_path)
    old = price_basis(daily(src),NOW).model_dump(mode="json")
    src.source.factor["LEAD"] = factor
    m = minutes(src)
    # Both NEW channels agree. Only comparison with the ARMED version reveals change.
    assert price_basis(m,NOW).daily_anchor_close == 1000*factor
    with pytest.raises(PriceHistoryChanged,match="revised"):
        check_price_scale(old,m,NOW,symbol="LEAD")
    assert src.store.latest(HOST,"LEAD")["status"] == "REVISED"
    # Updating the last cache does not erase the original armed version.
    with pytest.raises(PriceHistoryChanged):
        check_price_scale(old,minutes(src),NOW,symbol="LEAD")


@pytest.mark.parametrize("gap", [1.2,0.8])
def test_genuine_overnight_gap_does_not_trigger_history_revision(tmp_path,gap):
    src = source(tmp_path)
    old = price_basis(daily(src),NOW).model_dump(mode="json")
    src.source.gap["LEAD"] = gap
    assert check_price_scale(old,minutes(src),NOW,symbol="LEAD")
    assert src.store.latest(HOST,"LEAD")["status"] == "CONSISTENT"


@pytest.mark.parametrize("fault", ["mismatch","missing_history","incomplete_anchor","identity","delayed","future","stale","host"])
def test_bad_ticker_is_isolated_without_relabeling_unknown_data(tmp_path,fault):
    src = source(tmp_path)
    old = price_basis(daily(src),NOW).model_dump(mode="json")
    src.source.bad["LEAD"] = fault
    result = src.bars(["LEAD","OK"],timespan="M15",category="US_STOCK",count=40)
    assert "OK" in result
    if fault == "missing_history":
        with pytest.raises(BarDataError):
            check_price_scale(old,completed_intraday(result["LEAD"],NOW),NOW)
    else:
        assert "LEAD" not in result and "LEAD" in src.last_errors


def test_volume_revision_rebuilds_old_signal_without_calling_it_a_split(tmp_path):
    src = source(tmp_path)
    old = price_basis(daily(src),NOW).model_dump(mode="json")
    src.source.bad["LEAD"] = "volume"
    with pytest.raises(PriceHistoryChanged):
        check_price_scale(old,minutes(src),NOW)
    assert "split" not in src.store.latest(HOST,"LEAD")["detail"].lower()


def test_identity_change_stays_blocked_after_restart(tmp_path):
    native = Native(); src = source(tmp_path,native)
    daily(src)
    native.ids["LEAD"] = "replacement-company"
    again = source(tmp_path,native)
    assert not again.bars(["LEAD"],timespan="D",category="US_STOCK")
    assert "IDENTITY_CHANGED" in again.last_errors["LEAD"]


def test_failed_provider_batch_not_retried_or_exposed(tmp_path):
    native = Native(); native.outage = True; src = source(tmp_path,native)
    skipped = {}
    assert sc.fetch(src,["A","B"],"D",1000,skipped) == {}
    assert set(skipped) == {"A","B"}
    assert len(native.calls) == 1
    assert all("sensitive" not in v for v in skipped.values())


def test_scanner_keeps_healthy_ticker_and_specific_error(tmp_path):
    native = Native(); native.bad["BAD"] = "mismatch"
    src = source(tmp_path,native); skipped = {}
    result = sc.fetch(src,["OK","BAD"],"D",1000,skipped)
    assert set(result) == {"OK"} and "DAILY_RAW_CLOSE_MISMATCH" in skipped["BAD"]


def test_revision_invalidates_exact_event_and_new_detection_uses_fresh_history(tmp_path):
    src = source(tmp_path)
    old = price_basis(daily(src),NOW).model_dump(mode="json")
    candidate = replace(sig(),trigger=1000,stop=950,price_basis=old)
    store = SignalStore(tmp_path/"signals.sqlite")
    event = sc.observe_signal(store,candidate,minutes(src),NOW)[0]
    src.source.factor["LEAD"] = 0.1
    changed = minutes(src)
    with pytest.raises(PriceHistoryChanged):
        sc.observe_signal(store,candidate,changed,NOW)
    assert store.get(event["id"],NOW)["state"] == "invalidated"
    # Explicit fresh detection on rebuilt prices; never recycle the consumed crossing.
    rebuilt = replace(candidate,trigger=100,stop=95,price_basis=price_basis(changed,NOW).model_dump(mode="json"))
    assert sc.observe_signal(store,rebuilt,changed,NOW) == []
    assert not store.get(event["id"],NOW)["eligible"]


def test_identical_history_is_content_deduplicated_and_survives_restart(tmp_path):
    src = source(tmp_path); d = daily(src)
    old = price_basis(d,NOW).model_dump(mode="json")
    src.source.now += timedelta(seconds=1)
    daily(src)
    restarted = source(tmp_path,src.source)
    check_price_scale(old,minutes(restarted),src.source.now)
    with closing(sqlite3.connect(src.store.path)) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1


def test_malformed_optional_review_does_not_disable_automatic_prices(tmp_path):
    native = Native()
    wrapped = configured_source(native,{"DESK_VENDOR_BASIS_DB":str(tmp_path/"s.db"),"WEBULL_HOST":HOST,
        "DESK_ACTION_LEDGER":"missing", "DESK_ACTION_CHANNELS":"missing"},clock=lambda:NOW)
    assert wrapped.fallback_issue
    assert daily(wrapped) is not None


class ReviewedFallback:
    channels = {}
    def __init__(self):
        self.ledger, self.calls = self, 0

    def basis(self,symbol,now,**kwargs):
        from desk.data_basis import PriceBasis
        self.calls += 1
        raw = price_evidence("2026-09-29",symbol)
        raw["security_id"] = "id:"+symbol
        return PriceBasis.model_validate(raw)

    def bars(self,*args,**kwargs):
        raise AssertionError("A dated review must not bypass a contradictory price pair")


def test_reviewed_source_is_diagnostic_only_for_unresolved_price_pair(tmp_path):
    native = Native(); fallback = ReviewedFallback(); src = source(tmp_path,native,fallback)
    daily(src); assert fallback.calls == 0
    native.factor["LEAD"] = 0.99; native.bad["LEAD"] = "mismatch"
    assert not src.bars(["LEAD"],timespan="D",category="US_STOCK")
    assert fallback.calls == 1
    assert "evidence available for reconciliation" in src.last_errors["LEAD"]
    assert src.store.latest(HOST,"LEAD")["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("fault", ["delay","missing","duplicate","bad_rows"])
def test_real_webull_partial_parser_preserves_valid_peers(fault):
    from tests.test_webull import bar, client, FakeTransport
    a = {"symbol":"A","instrument_id":"1","delay_minutes":0,"result":[bar("2026-09-28T04:00:00Z",100)]}
    b = {**a,"symbol":"B","instrument_id":"2"}
    rows = [a,b]
    if fault == "delay": b["delay_minutes"] = 15
    if fault == "missing": rows = [a]
    if fault == "duplicate": rows.append(b.copy())
    if fault == "bad_rows": b["result"] = []
    transport = FakeTransport({"result":rows})
    assert set(client(transport).bars_partial(["A","B"],category="US_STOCK",timespan="D")) == {"A"}
    assert len(transport.requests) == 1
    with pytest.raises(BarDataError):
        client(FakeTransport({"result":rows})).bars(["A","B"],category="US_STOCK",timespan="D")


def test_probe_reports_price_pass_and_volume_limit_separately(tmp_path):
    from desk.vendor_check import check
    src = source(tmp_path)
    result = check(src,["LEAD"],clock_fn=lambda:NOW)
    assert result["status"] == "PASS"
    assert result["checks"][0]["volume_window"] == "UNAVAILABLE_SEPARATE_EVIDENCE_REQUIRED"
    assert result["action_coverage"] == "NOT_ATTESTED"


def test_conflicting_known_metadata_row_isolated_in_partial_lookup():
    from tests.test_webull import client, FakeTransport
    from desk.security import securities
    rows = [{"symbol":"AAPL","instrument_id":"1","name":"Apple","currency":"USD",
             "exchange_code":"NSQ","category":"US_STOCK","sub_category":"COMMON_STOCK"},
            {"symbol":"BRK B","instrument_id":"wrong"}]
    raw = client(FakeTransport({"data":rows}))
    class Partial:
        security_metadata = raw.security_metadata_partial
    skipped = {}
    assert set(securities(Partial(),["AAPL","BRK.B"],skipped)) == {"AAPL"}
    assert "BRK.B" in skipped


def test_new_vendor_basis_cannot_silently_accept_legacy_signal(tmp_path):
    src = source(tmp_path)
    old = price_evidence("2026-09-28","LEAD"); old["security_id"] = "id:LEAD"
    with pytest.raises(PriceHistoryChanged):
        check_price_scale(old,minutes(src),NOW)


def test_revision_before_first_event_retires_candidate_and_prevents_replay(tmp_path):
    src = source(tmp_path)
    old = price_basis(daily(src),NOW).model_dump(mode="json")
    candidate = replace(sig(),trigger=1000,stop=950,price_basis=old)
    store = SignalStore(tmp_path/"signals.sqlite")
    src.source.factor["LEAD"] = 0.1
    changed = minutes(src)
    with pytest.raises(PriceHistoryChanged):
        sc.observe_signal(store,candidate,changed,NOW)
    rebuilt = replace(candidate,trigger=100,stop=95,
        price_basis=price_basis(changed,NOW).model_dump(mode="json"))
    assert sc.observe_signal(store,rebuilt,changed,NOW) == []
    # Even a later vendor reversal cannot resurrect the retired reference.
    src.source.factor["LEAD"] = 1
    assert sc.observe_signal(store,candidate,minutes(src),NOW) == []


def test_automatic_source_arms_and_triggers_breakout_without_manual_enrollment(tmp_path):
    from tests.test_scanner import frames, m15
    charts = frames(end=NOW.date()-timedelta(days=1))
    class WatchlistNative(Native):
        def bars(self,symbols,*,timespan,category,count=1000,**kwargs):
            output = super().bars(symbols,timespan=timespan,category=category,count=count,**kwargs)
            for name,raw in list(output.items()):
                if name == "BAD":
                    raw.attrs["provider_identity"]["instrument_id"] = "wrong"
                    continue
                attrs = raw.attrs.copy()
                if timespan == "D":
                    output[name] = charts[(name,"D")].copy()
                    output[name].attrs = attrs
                elif "start_time" in kwargs:
                    price = float(charts[(name,"D")].close.iloc[-1])
                    raw.loc[:,["open","high","low","close"]] = price
                else:
                    output[name] = m15([(147,148.5,146.5,148),(148,153,148,152.5)])
                    output[name].attrs = attrs
            return output
    native = WatchlistNative(); src = source(tmp_path,native)
    rec,armed = sc.close_scan(src,["LEAD","BAD"],NOW,preparing=True)
    assert not rec.error and "BAD" in rec.skipped
    candidate = next(s for s in armed if s.setup_id == "1_qullamaggie_breakout")
    assert candidate.price_basis["method"] == "webull-history-v1"
    store = SignalStore(tmp_path/"signals.sqlite")
    result = sc.intraday_scan(src,[candidate],NOW,store=store)
    assert len(result.triggered) == 1
    assert result.triggered[0]["qualified_for_analysis"]
    assert result.triggered[0]["stop"] == 146.5


def test_daily_provider_request_excludes_forming_session(tmp_path):
    src = source(tmp_path)
    daily(src)
    request = next(c for c in src.source.calls if c[1] == "D")
    assert request[2]["end_time"] == int(session(NOW.date()-timedelta(days=1))[1].timestamp()*1000)-1


def test_rebuilt_candidate_cannot_replay_gap_since_last_observation(tmp_path):
    src = source(tmp_path)
    old = price_basis(daily(src),NOW).model_dump(mode="json")
    candidate = replace(sig(),trigger=1000,stop=950,price_basis=old)
    store = SignalStore(tmp_path/"signals.sqlite")
    assert sc.observe_signal(store,candidate,minutes(src),NOW)
    src.source.now += timedelta(minutes=15)
    src.source.factor["LEAD"] = 0.1
    changed = src.bars(["LEAD"],timespan="M15",category="US_STOCK",count=40)["LEAD"]
    attrs = changed.attrs.copy()
    changed.loc[changed.index[-1]+pd.Timedelta(minutes=15)] = [101.5,103,101,103,1000]
    changed.attrs = attrs
    with pytest.raises(PriceHistoryChanged):
        sc.observe_signal(store,candidate,changed,src.source.now)
    rebuilt = replace(candidate,trigger=102.5,stop=95,
        price_basis=price_basis(changed,src.source.now).model_dump(mode="json"))
    assert sc.observe_signal(store,rebuilt,changed,src.source.now) == []


def test_unchanged_refresh_preserves_candidate_and_live_event_identity(tmp_path):
    from desk.signal_state import candidate_id
    src = source(tmp_path)
    old = price_basis(daily(src),NOW).model_dump(mode="json")
    candidate = replace(sig(),trigger=1000,stop=950,price_basis=old)
    store = SignalStore(tmp_path/"signals.sqlite")
    event = sc.observe_signal(store,candidate,minutes(src),NOW)[0]
    src.source.now += timedelta(seconds=1)
    fresh = price_basis(daily(src),src.source.now).model_dump(mode="json")
    assert fresh["verified_at"] != old["verified_at"]
    rebuilt = replace(candidate,price_basis=fresh)
    assert candidate_id(rebuilt,NOW.date()) == candidate_id(candidate,NOW.date())
    assert sc.observe_signal(store,rebuilt,minutes(src),src.source.now) == []
    assert store.get(event["id"],src.source.now)["eligible"]


def test_history_revision_retires_older_volume_review_across_restart(tmp_path):
    from desk.action_source import PriceChannelReview
    fallback = ReviewedFallback()
    fallback.channels = {("LEAD","D"):PriceChannelReview(source="fixture",evidence_ref="fixture",host=HOST,
        symbol="LEAD",timeframe="D",normalization="split_dividend_adjusted",
        volume_policy="native_no_split_window",volume_evidence_ref="fixture")}
    native = Native(); src = source(tmp_path,native,fallback)
    volume_basis(daily(src))
    native.now += timedelta(seconds=1)
    native.factor["LEAD"] = 0.1
    with pytest.raises(BarDataError,match="Unverified volume"):
        volume_basis(daily(src))
    # A subsequent consistent refresh cannot revive the pre-revision attestation.
    restarted = source(tmp_path,native,fallback)
    with pytest.raises(BarDataError,match="Unverified volume"):
        volume_basis(daily(restarted))


class Malformed(Native):
    """Corrupts one ticker's frames in ways that break the adapter contract."""
    def __init__(self, fault):
        super().__init__()
        self.fault = fault

    def bars(self, symbols, **kwargs):
        out = super().bars(symbols, **kwargs)
        if "BAD" in out:
            frame = out["BAD"]
            if self.fault == "received_garbage":
                frame.attrs["received_at"] = "not a time"
            elif self.fault == "provenance_none":
                frame.attrs["bar_provenance"] = None
            elif self.fault == "request_none":
                frame.attrs["webull_request"] = None
            elif self.fault == "not_a_frame":
                out["BAD"] = None
            elif self.fault == "naive_index":
                frame.index = frame.index.tz_localize(None)
            elif self.fault == "no_attrs":
                frame.attrs = {}
            elif self.fault == "string_prices":
                out["BAD"] = frame.astype(str)
                out["BAD"].attrs = frame.attrs
        return out


@pytest.mark.parametrize("fault", ["received_garbage", "provenance_none", "request_none", "not_a_frame",
                                   "naive_index", "no_attrs", "string_prices"])
@pytest.mark.parametrize("timespan", ["D", "M15"])
def test_one_malformed_ticker_does_not_take_down_a_healthy_peer(tmp_path, fault, timespan):
    """Outside Claude #13 (G5.B3): per-ticker isolation also covers malformed frames."""
    src = source(tmp_path, Malformed(fault))
    out = src.bars(["BAD", "LEAD"], timespan=timespan, category="US_STOCK", count=40)
    assert "LEAD" in out and "BAD" not in out
    assert src.last_errors["BAD"] and src.store.latest(HOST, "BAD")["status"] == "UNAVAILABLE"
    assert src.store.latest(HOST, "LEAD")["status"] == "CONSISTENT"


class Sessions(Native):
    """Daily history that grows by one session per day; 2026-09-29 is a 10% gap-up day."""
    ROWS = {"2026-09-24": [990, 1010, 980, 1000, 10000], "2026-09-25": [990, 1010, 980, 1000, 10000],
            "2026-09-28": [990, 1010, 980, 1000, 10000], "2026-09-29": [1100, 1125, 1095, 1110, 30000],
            "2026-09-30": [1110, 1130, 1100, 1120, 15000]}

    def __init__(self):
        super().__init__()
        self.edit = {}

    def bars(self, symbols, *, timespan, category, count=1000, **kwargs):
        out = super().bars(symbols, timespan=timespan, category=category, count=count, **kwargs)
        today = self.now.date()
        last = max(d for d in self.ROWS if pd.Timestamp(d).date() < today)
        for symbol, raw in out.items():
            attrs = raw.attrs
            if timespan == "D":
                days = [d for d in self.ROWS if d <= last]
                rows = [list(self.edit.get(d, self.ROWS[d])) for d in days]
                frame = pd.DataFrame(rows, index=pd.DatetimeIndex(days, tz=ET).tz_convert("UTC"),
                                     columns=["open", "high", "low", "close", "volume"], dtype=float)
            elif "start_time" in kwargs:
                opened, closed = session(pd.Timestamp(last).date())
                idx = pd.date_range(opened, closed - pd.Timedelta(minutes=15), freq="15min").tz_convert("UTC")
                close = self.edit.get(last, self.ROWS[last])[3]
                frame = pd.DataFrame([[close] * 4 + [1000]] * len(idx), index=idx,
                                     columns=["open", "high", "low", "close", "volume"], dtype=float)
            else:
                idx = pd.date_range(pd.Timestamp(f"{today} 09:30", tz=ET), periods=2, freq="15min").tz_convert("UTC")
                frame = pd.DataFrame([[1110, 1130, 1100, 1120, 1000]] * 2, index=idx,
                                     columns=["open", "high", "low", "close", "volume"], dtype=float)
            frame.attrs = attrs
            out[symbol] = frame
        return out


def test_gap_day_inside_the_compared_history_is_not_a_revision(tmp_path):
    """Outside Claude #16 (G5.B4): a genuine gap that has become history (EP day 2 and
    later) stays CONSISTENT; an edit to an already-seen row is REVISED."""
    native = Sessions()
    src = source(tmp_path, native)
    src.bars(["LEAD"], timespan="D", category="US_STOCK")             # 09-29: history to 09-28
    assert src.store.latest(HOST, "LEAD")["status"] == "CONSISTENT"
    native.now = NOW + timedelta(days=1)                               # 09-30: gap day 09-29 appended
    src.bars(["LEAD"], timespan="D", category="US_STOCK")
    assert src.store.latest(HOST, "LEAD")["status"] == "CONSISTENT"
    native.now = NOW + timedelta(days=2)                               # 10-01: gap day is in the overlap
    frame = src.bars(["LEAD"], timespan="D", category="US_STOCK")["LEAD"]
    assert src.store.latest(HOST, "LEAD")["status"] == "CONSISTENT"
    assert frame.loc[pd.Timestamp("2026-09-29", tz=ET).tz_convert("UTC"), "open"] == 1100
    # Control: the same comparison does flag a changed row that was already seen.
    native.edit["2026-09-29"] = [1100, 1125, 1095, 1110, 30001]
    native.now = NOW + timedelta(days=2, minutes=15)
    src.bars(["LEAD"], timespan="D", category="US_STOCK")
    revised = src.store.latest(HOST, "LEAD")
    assert revised["status"] == "REVISED" and revised["detail"] == "Daily OHLCV revised on 2026-09-29"
    assert src.store.latest_revision(HOST, "LEAD") == native.now
