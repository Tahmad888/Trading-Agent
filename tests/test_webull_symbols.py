"""Observed metadata identity; generated bars test mapping, not live BRK bar access."""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from desk import scanner
from desk.security import securities
from desk.symbols import canonical_symbol, webull_symbol
from desk.watchlist import movers, overlay_picks
from desk.webull import INSTRUMENTS_PATH, WebullError
from tests.test_webull import client, bar, FakeTransport

OBSERVED = {"symbol":"BRK B", "instrument_id":"916040668",
    "name":"BERKSHIRE HATHAWAY INC DEL", "category":"US_STOCK",
    "sub_category":"COMMON_STOCK", "exchange_code":"NYSE", "currency":"USD"}


def transport_fixture(timespan="D", identity="916040668"):
    requests=[]
    def transport(req, timeout):
        requests.append(req)
        if INSTRUMENTS_PATH in req.full_url:
            result={"data":[OBSERVED]}
        elif "screeners" in req.full_url:
            result=[{"symbol":"BRK B", "price":500, "change_ratio":.12}]
        else:
            row=bar("2026-09-29T13:30:00+0000",500)
            if timespan == "M15":
                row["trading_session"]="RTH"
            result={"result":[{"symbol":"BRK B", "instrument_id":identity,
                     "delay_minutes":0, "result":[row]}]}
        return json.dumps(result).encode()
    return transport, requests


@pytest.mark.parametrize("alias",["BRK.B","BRK-B","BRK B"," brk b "])
def test_observed_metadata_alias_and_cache(alias):
    t=FakeTransport({"data":[OBSERVED]})
    source=client(t)
    errors={}
    result=securities(source,[alias],errors)
    assert not errors
    assert result["BRK.B"].instrument_id=="916040668"
    assert result["BRK.B"].provider_symbol=="BRK B"
    source.security_metadata(["BRK B","BRK-B","BRK.B"])
    assert len(t.requests)==1
    query=parse_qs(urlparse(t.requests[0].full_url).query)
    assert query["symbols"]==["BRK B"]


@pytest.mark.parametrize("timespan",["D","M15"])
def test_scanner_fetch_preserves_normalized_and_raw_bar_identities(timespan):
    transport, requests=transport_fixture(timespan)
    errors={}
    frames=scanner.fetch(client(transport),["BRK.B","BRK B","BRK-B"],timespan,2,errors)
    assert not errors and list(frames)==["BRK.B"]
    frame=frames["BRK.B"]
    assert frame.attrs["provider_identity"]=={"symbol":"BRK.B","instrument_id":"916040668"}
    assert frame.attrs["provider_identity_raw"]=={"symbol":"BRK B","instrument_id":"916040668"}
    assert frame.attrs["security_metadata"]["symbol"]=="BRK.B"
    payload=json.loads(requests[1].data)
    assert payload["symbols"]==["BRK B"] and payload["timespan"]==timespan


@pytest.mark.parametrize("identity",[None,"wrong-class-id"])
def test_metadata_alias_requires_observed_instrument_id(identity):
    errors={}
    source=client(FakeTransport({"data":[{**OBSERVED,"instrument_id":identity}]}))
    assert securities(source,["BRK.B"],errors)=={}
    assert "identity differs" in errors["BRK.B"]


def test_bar_alias_requires_observed_instrument_id():
    transport,_=transport_fixture(identity="wrong-class-id")
    errors={}
    assert scanner.fetch(client(transport),["BRK.B"],"D",2,errors)=={}
    assert "identity differs" in errors["BRK.B"]


def test_provider_alias_duplicates_remain_ambiguous():
    source=client(FakeTransport({"data":[OBSERVED,{**OBSERVED,"symbol":"BRK.B"}]}))
    errors={}
    assert securities(source,["BRK.B"],errors)=={}
    assert "duplicate" in errors["BRK.B"]


def test_user_sources_merge_and_alias_removals_win():
    result=overlay_picks({"BRK B":["leader scan"],"BRK-B":["mover"]},{"add":["BRK.B"]})
    assert result["BRK.B"]==["leader scan","mover","Taz"]
    assert "BRK.B" not in overlay_picks(result,{"remove":["BRK B"],"add":["BRK-B"]})


def test_ranking_provider_spelling_reaches_fetch():
    transport,_=transport_fixture()
    source=client(transport)
    errors={}
    names=movers(source,errors)
    assert names==["BRK.B"] and not errors
    assert "BRK.B" in scanner.fetch(source,names,"D",2,errors)
    assert not errors


def test_unverified_share_classes_are_not_rewritten():
    for name in ["BRK.A","BF-B","ABC B"]:
        assert webull_symbol(name)==name
        assert canonical_symbol(name)==name


def test_identity_probe_uses_repaired_adapter_without_decision_profiles():
    from desk.metadata_check import check
    transport,requests=transport_fixture("M15")
    report=check(client(transport),["BRK-B","BRK B"],timespans=("D","M15"))
    assert report["status"]=="PASS"
    assert len(requests)==3
    result=report["checks"][0]
    assert result["symbol"]=="BRK.B" and result["provider_symbol"]=="BRK B"
    assert set(result["bars"])=={"D","M15"}


def test_probe_stops_on_first_provider_error_and_suppresses_details():
    from desk.metadata_check import check
    calls=[]
    class Source:
        def security_metadata(self,names):
            calls.append(names)
            raise RuntimeError("SECRET_IN_PROVIDER_ERROR")
    result=check(Source(),["NVDA","SPY"])
    assert result["status"]=="FAIL" and len(calls)==1
    assert "SECRET" not in json.dumps(result)


def test_probe_reports_missing_metadata_without_guessing():
    from desk.metadata_check import check
    source=client(FakeTransport({"data":[]}))
    result=check(source,["BRK.B"])
    assert result["status"]=="FAIL"
    assert result["skipped"]=={"BRK.B":"security metadata missing"}


def test_probe_rejects_conflicting_bar_id():
    from desk.metadata_check import check
    transport,_=transport_fixture(identity="WRONG")
    result=check(client(transport),["BRK.B"])
    assert result["status"]=="FAIL" and result["stopped_on_error"]


@pytest.mark.parametrize("method",["snapshot","earnings_calendar","dividend_calendar","fund_splits"])
def test_other_read_only_requests_use_same_provider_spelling(method):
    transport=FakeTransport([])
    source=client(transport)
    if method=="snapshot":
        source.snapshot(["BRK-B"],category="US_STOCK")
    else:
        getattr(source,method)("BRK.B")
    query=parse_qs(urlparse(transport.requests[0].full_url).query)
    assert query.get("symbol",query.get("symbols"))==["BRK B"]


def test_alias_removal_blocks_canonical_signal_review(tmp_path):
    from dataclasses import replace
    from tests.test_signal_lifecycle import setup, now, BASE
    from tests.test_scanner import Fake, DAY, m15
    log=scanner.ScanLog(tmp_path)
    signal=replace(setup("long"),symbol="BRK.B")
    observations=scanner.entry_observations(signal,m15(BASE))
    event=log.signals.observe(signal,DAY,observations,now())[0]
    (tmp_path/"taz-picks.json").write_text('{"remove":["BRK B"]}')
    result=scanner.revalidate_signal(Fake({}),log,event["id"],now(),
        symbol="BRK.B",price=101.5,quote_at=now())
    assert not result["eligible"] and "removed" in result["reasons"][0]
