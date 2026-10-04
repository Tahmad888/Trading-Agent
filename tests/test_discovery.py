"""Step 08 coverage and failure semantics; all market fixtures are synthetic."""
from datetime import date, datetime, timedelta
import json

import numpy as np
import pytest

from desk import scanner as sc
from desk.bars import BarDataError
from desk.security import securities
from desk.watchlist import universe, movers, bearish_candidates, build_watchlist, overlay_picks
from tests.test_scanner import Fake, DAY, ET, daily, frames, m15, UP
from tests.test_triggers import GRAIL_DOWN
from tests.test_webull import client, FakeTransport


def now(hour=10, minute=0, day=DAY):
    return datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(hour=hour, minute=minute)


def ep_frames(n=60):
    return {("NEW", "D"):daily(np.full(n,50.),spread=.03,end=date(2026,9,28)),
            ("NEW", "M15"):m15([(57.5,58.5,57,58),(58,59,57.5,58.8)]).assign(volume=1e6)}


def picks(log, add=(), remove=()):
    (log.root/"taz-picks.json").write_text(json.dumps({"add":list(add),"remove":list(remove)}))


def test_share_class_security_and_non_optionable_stock_are_kept():
    class Source(Fake):
        def security_metadata(self,names):
            return [{**r,"optionable":False} for r in super().security_metadata(names)]
    source=Source({},lists={"MONTH_3":[{"symbol":"BRK.B","price":"480"}],
                            "PRE_MARKET":[{"symbol":"NEW","price":"50","change_ratio":".15"}]})
    errors={}
    assert universe(source,errors)==["BRK.B"]
    assert movers(source,errors)==["NEW"] and not errors


@pytest.mark.parametrize("fault",["missing","duplicate","currency","unknown_type","unrequested","bad_id","missing_name"])
def test_metadata_faults_are_observable_and_block_bar_fetch(fault):
    class Source(Fake):
        def security_metadata(self,names):
            rows=super().security_metadata(names)
            if fault=="missing": return []
            if fault=="duplicate": return rows+rows
            edits={"currency":{"currency":"EUR"},"unknown_type":{"sub_category":"??"},
                   "unrequested":{"symbol":"OTHER"},"bad_id":{"instrument_id":None},"missing_name":{"name":""}}
            return [{**r,**edits[fault]} for r in rows]
    source=Source({("LEAD","D"):daily(UP)})
    errors={}
    assert sc.fetch(source,["LEAD"],"D",1000,errors)=={}
    assert "LEAD" in errors and source.calls==[]


def test_noncore_etf_routes_by_metadata_not_ticker_list():
    class Source(Fake):
        def security_metadata(self,names):
            return [{**r,"sub_category":"ETF"} for r in super().security_metadata(names)]
    source=Source({("CUSTOM","D"):daily(UP)})
    errors={}
    assert "CUSTOM" in sc.fetch(source,["CUSTOM"],"D",1000,errors)
    assert source.calls[0][1]=="US_ETF"


def test_bars_with_conflicting_identity_are_rejected():
    frame=daily(UP)
    frame.attrs["provider_identity"]={"symbol":"LEAD","instrument_id":"wrong"}
    errors={}
    assert sc.fetch(Fake({("LEAD","D"):frame}),["LEAD"],"D",1000,errors)=={}
    assert "identity" in errors["LEAD"]


def test_loser_discovery_is_separate_from_leadership():
    source=Fake({},lists={("losers","DAY_1"):[{"symbol":"DOWN","price":"100","change_ratio":"-.10"}],
                         "MONTH_3":[{"symbol":"UP","price":"100"}]})
    errors={}
    assert universe(source,errors)==["UP"]
    assert bearish_candidates(source,errors)==["DOWN"]
    wl=build_watchlist(["UP"],bearish=["DOWN"],movers=["UP"],added=["MINE"])
    assert wl["DOWN"]==["bearish"] and wl["UP"]==["leader scan","mover"]


def test_short_history_bearish_candidate_reaches_real_short_setup(tmp_path):
    source=Fake({**frames(),("DOWN","D"):daily(GRAIL_DOWN[-100:])},
                lists={("losers","DAY_1"):[{"symbol":"DOWN","price":"110","change_ratio":"-.03"}]})
    rec=sc.run(source,[],sc.ScanLog(tmp_path),now(hour=16,minute=10))
    assert not rec.error
    assert rec.discovery["sources"]["DOWN"]==["bearish"]
    assert any(s["symbol"]=="DOWN" and s["direction"]=="short" for s in rec.armed)
    assert "DOWN/trend_template" in rec.skipped
    assert "need 252" in rec.skipped["DOWN/1_qullamaggie_breakout"]


@pytest.mark.parametrize("length,expected",[(59,False),(60,True),(100,True)])
def test_ep_has_its_own_complete_history_requirement(length,expected):
    errors={}
    found=sc.episodic_pivots(Fake(ep_frames(length)),sc.MarketSize.FULL,now(),errors,candidates=["NEW"])
    assert bool(found)==expected
    if expected:
        assert found[0].symbol=="NEW"
    else:
        assert "NEW" in errors


def test_user_ticker_is_prepared_on_next_intraday_scan_not_friday(tmp_path):
    log=sc.ScanLog(tmp_path)
    picks(log,add=["NEW"])
    f={**frames(date(2026,9,28)),**ep_frames()}
    rec=sc.run(Fake(f),[],log,now())
    assert not rec.error
    assert rec.discovery["user_preparation"]["prepared"]==["NEW"]
    assert any(s["symbol"]=="NEW" and s["setup_id"].startswith("5_") for s in rec.armed)
    assert log.prepared_users(DAY)=={"NEW"}


def test_user_prepared_before_10_still_gets_ep_check_at_10(tmp_path):
    log=sc.ScanLog(tmp_path)
    picks(log,add=["NEW"])
    source=Fake({**frames(date(2026,9,28)),**ep_frames()})
    first=sc.run(source,[],log,now(hour=9,minute=45))
    assert not first.error and log.prepared_users(DAY)=={"NEW"}
    second=sc.run(source,[],log,now())
    assert any(s["setup_id"].startswith("5_") for s in second.armed)


def test_failed_user_preparation_is_not_marked_done(tmp_path):
    log=sc.ScanLog(tmp_path)
    picks(log,add=["NEW"])
    rec=sc.run(Fake(frames(date(2026,9,28))),[],log,now())
    assert "NEW" in rec.discovery["user_preparation"]["skipped"]
    assert "NEW" not in log.prepared_users(DAY)
    assert "NEW" not in log.prepared_users(DAY,"ep")


def test_user_removal_wins_and_core_etfs_remain():
    wl=overlay_picks({"AMD":["leader scan"]},{"add":["AMD","MY.NEW"],"remove":["AMD","SPY"]})
    assert "AMD" not in wl and wl["MY.NEW"]==["Taz"] and "SPY" in wl


def test_user_removal_blocks_existing_signal_review_without_provider_calls(tmp_path):
    from tests.test_signal_lifecycle import process
    log=sc.ScanLog(tmp_path)
    event=process(log.signals)[0]
    picks(log,remove=["LEAD"])
    source=Fake({})
    result=sc.revalidate_signal(source,log,event["id"],now(),symbol="LEAD",price=101.5,quote_at=now())
    assert not result["eligible"] and source.calls==[]


def test_healthy_empty_weekly_build_replaces_old_list_with_core_and_user(tmp_path):
    day=date(2026,10,2)
    log=sc.ScanLog(tmp_path)
    log.write_watchlist({"OLD":["leader scan"]})
    picks(log,add=["MINE"])
    rec=sc.leader_scan_job(Fake(frames(day)),log,now(hour=16,minute=40,day=day))
    assert not rec.error and rec.discovery["watchlist_build"]["status"]=="EMPTY"
    wl=json.loads((tmp_path/"watchlist.json").read_text())
    assert "OLD" not in wl and "MINE" in wl and "SPY" in wl


def test_failed_weekly_build_preserves_last_list_and_labels_staleness(tmp_path):
    class Broken(Fake):
        def gainers(self,*args,**kwargs): raise BarDataError("fixture ranking unavailable")
    day=date(2026,10,2)
    log=sc.ScanLog(tmp_path)
    log.write_watchlist({"OLD":["leader scan"]})
    log.build_status(now(day=date(2026,9,25)),"READY",{},success=True)
    original=(tmp_path/"watchlist.json").read_text()
    rec=sc.leader_scan_job(Broken(frames(day)),log,now(hour=16,minute=40,day=day))
    assert rec.error and rec.discovery["watchlist_build"]["status"]=="FAILED"
    assert (tmp_path/"watchlist.json").read_text()==original
    assert log.watchlist_status(now(day=date(2026,10,5)))["stale"]


def metadata_row(symbol="ABC"):
    return {"symbol":symbol,"instrument_id":"id:"+symbol,"name":"Example class B",
            "category":"US_STOCK","sub_category":"COMMON_STOCK","currency":"USD","exchange_code":"NYS"}


def test_webull_reference_request_is_read_only_and_cached():
    transport=FakeTransport({"data":[metadata_row()]})
    source=client(transport)
    assert source.security_metadata(["ABC"])[0]["sub_category"]=="COMMON_STOCK"
    source.security_metadata(["ABC"])
    assert len(transport.requests)==1
    req=transport.requests[0]
    assert req.method=="GET" and "/trading/instruments/stocks/profiles/list?" in req.full_url
    assert "category=US_STOCK" in req.full_url and "symbols=ABC" in req.full_url
    assert req.data is None


def test_webull_metadata_pagination_and_duplicate_detection():
    replies=iter([{"data":[metadata_row("ONE")],"pagination_key":"next"},{"data":[metadata_row("TWO")]}])
    requests=[]
    def transport(req,timeout):
        requests.append(req)
        return json.dumps(next(replies)).encode()
    source=client(transport)
    errors={}
    assert set(securities(source,["ONE","TWO"],errors))=={"ONE","TWO"}
    assert "pagination_key=next" in requests[1].full_url


def test_metadata_endpoint_failure_does_not_guess_stock_type():
    from desk.webull import WebullError
    class Bad(Fake):
        def security_metadata(self,names): raise WebullError("fixture HTTP 403")
    errors={}
    assert universe(Bad({},lists={"MONTH_3":[{"symbol":"ABC","price":100}]}),errors)==[]
    assert "metadata unavailable" in errors["ABC"]


def test_malformed_and_nonfinite_rankings_are_distinct():
    errors={}
    source=Fake({},lists={"PRE_MARKET":[{"symbol":"NAN","price":"NaN","change_ratio":"inf"}],
                         "DAY_1":[{"price":"50"}]})
    assert movers(source,errors)==[]
    assert "gainers:DAY_1" in errors


def test_one_instrument_cannot_identify_two_symbols():
    class Source(Fake):
        def security_metadata(self,names):
            return [{**r,"instrument_id":"same"} for r in super().security_metadata(names)]
    errors={}
    assert securities(Source({}),["ONE","TWO"],errors)=={}
    assert set(errors)=={"ONE","TWO"}


def test_late_user_addition_gets_ep_and_failed_fetch_retries_next_slot(tmp_path):
    log=sc.ScanLog(tmp_path)
    picks(log,add=["NEW"])
    source=Fake({**frames(date(2026,9,28)),**ep_frames()})
    minutes=source.frames.pop(("NEW","M15"))
    first=sc.run(source,[],log,now(hour=11))
    assert "NEW" not in log.prepared_users(DAY,"ep")
    source.frames[("NEW","M15")]=m15(
        list(minutes[["open","high","low","close"]].itertuples(index=False,name=None))
        + [(58.8,59,58,58.8)]*5).assign(volume=1e6)
    second=sc.run(source,[],log,now(hour=11,minute=15))
    assert not second.error
    assert any(s["setup_id"].startswith("5_") for s in second.armed)
    assert "NEW" in log.prepared_users(DAY,"ep")


def test_invalid_user_picks_are_logged_as_failed_scan(tmp_path):
    log=sc.ScanLog(tmp_path)
    (tmp_path/"taz-picks.json").write_text('{"add":"ABC"}')
    result=sc.run(Fake({}),[],log,now())
    assert "add/remove lists" in result.error
    assert log.records()[-1]["error"]==result.error


def test_corrupt_watchlist_status_is_explicit_and_can_be_replaced(tmp_path):
    log=sc.ScanLog(tmp_path)
    (tmp_path/"watchlist-status.json").write_text('{broken')
    assert log.watchlist_status(now())["status"]=="INVALID"
    log.build_status(now(),"FAILED",{"source":"unavailable"})
    assert log.watchlist_status(now())["stale"]


def test_reference_cache_expires(monkeypatch):
    import desk.webull as webull
    stamp=[100.]
    monkeypatch.setattr(webull.time,"monotonic",lambda:stamp[0])
    transport=FakeTransport({"data":[metadata_row()]})
    source=client(transport)
    source.security_metadata(["ABC"])
    stamp[0]+=301
    source.security_metadata(["ABC"])
    assert len(transport.requests)==2


def test_reference_pagination_loop_rejected():
    transport=FakeTransport({"data":[metadata_row()],"pagination_key":"loop"})
    errors={}
    assert securities(client(transport),["ABC"],errors)=={}
    assert "pagination" in errors["ABC"]
    assert len(transport.requests)==2


def test_acceptance_scope_does_not_guess_other_security_categories():
    from desk.data_acceptance import check
    source=Fake({})
    result=check(source,["OTHER"],clock_fn=now)
    assert result["status"]=="FAIL" and source.calls==[]



def test_partial_weekly_build_publishes_valid_leaders_and_discloses_failures(tmp_path):
    # G5a checkpoint 2 (Taz 2026-10-04): one failed candidate no longer vetoes the build.
    day=date(2026,10,2)
    log=sc.ScanLog(tmp_path)
    log.write_watchlist({"OLD":["leader scan"]})
    source=Fake(frames(day),lists={"MONTH_3":[{"symbol":"LEAD","price":150},
                                                        {"symbol":"MISSING","price":100}]})
    rec=sc.leader_scan_job(source,log,now(hour=16,minute=40,day=day))
    status=rec.discovery["watchlist_build"]
    assert not rec.error and status["status"]=="PARTIAL" and status["generation"]==1 and not status["retained"]
    wl=json.loads((tmp_path/"watchlist.json").read_text())
    assert "OLD" not in wl and wl["LEAD"]==["leader scan"]
    build=json.loads((tmp_path/"watchlist-build.json").read_text())["build"]
    assert build["outcomes"]["MISSING"]["outcome"]=="source_failure"
    assert build["outcomes"]["LEAD"]["outcome"]=="selected" and build["population"]==["LEAD"]


def test_incomplete_weekly_build_without_valid_leaders_retains_old_list(tmp_path):
    day=date(2026,10,2)
    log=sc.ScanLog(tmp_path)
    log.write_watchlist({"OLD":["leader scan"]})
    source=Fake(frames(day),lists={"MONTH_3":[{"symbol":"MISSING","price":100}]})
    rec=sc.leader_scan_job(source,log,now(hour=16,minute=40,day=day))
    status=rec.discovery["watchlist_build"]
    # CP3 audit F1: the only candidate failed its source data: FAILED, old list retained.
    assert rec.error and status["status"]=="FAILED" and status["retained"] and status["generation"]==0
    assert "OLD" in json.loads((tmp_path/"watchlist.json").read_text())


def test_darvas_requires_full_history_at_box_top_not_only_today():
    from tests.test_triggers import DARVAS, features, ctx, run
    with pytest.raises(BarDataError,match="high_52w"):
        run("4_darvas_box",features(DARVAS[-252:]),ctx())
