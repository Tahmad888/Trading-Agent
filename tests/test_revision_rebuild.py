"""G3a real detector and durable scanner integration, synthetic feed only."""
from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from desk import scanner as sc
from desk.data_basis import PriceHistoryChanged, price_basis, volume_basis
from desk.bars import BarDataError
from desk.revision_rebuild import rebuild_pending
from desk.signal_state import candidate_id, signal_payload
from tests.test_vendor_basis import Native, NOW, source, daily, minutes
from tests.test_scanner import frames, m15


class RevisedCharts(Native):
    def __init__(self):
        super().__init__()
        self.charts = frames(end=NOW.date()-timedelta(days=1))
        self.flat = False
        self.raw_anchor_unchanged = False

    def gainers(self,*args,**kwargs):
        return []

    def bars(self,symbols,*,timespan,category,count=1000,**kwargs):
        output = super().bars(symbols,timespan=timespan,category=category,count=count,**kwargs)
        for name,raw in list(output.items()):
            attrs = raw.attrs.copy()
            scale = self.factor.get(name,1)
            if timespan == "D":
                output[name] = self.charts[(name,"D")].copy()
                output[name].loc[:,["open","high","low","close"]] *= scale
                if self.flat and name == "LEAD":
                    output[name].loc[:,["open","high","low","close"]] = 100.0
                output[name].attrs = attrs
            elif "start_time" in kwargs:
                price = float(self.charts[(name,"D")].close.iloc[-1])
                price *= 1 if self.raw_anchor_unchanged else scale
                if self.flat and name == "LEAD":
                    price = 100.0
                raw.loc[:,["open","high","low","close"]] = price
            else:
                rows = [(147,148.5,146.5,148),(148,153,148,152.5),(152.5,153,149,150),
                        (150,151,148,149),(149,154,149,153)]
                n = int((pd.Timestamp(self.now)-pd.Timestamp(NOW.replace(hour=9,minute=30))).total_seconds()//900)
                output[name] = m15(rows[:n])
                prices = ["open","high","low","close"]
                output[name][prices] = output[name][prices].astype(float)*scale
                output[name].attrs = attrs
        return output


def seed(tmp_path):
    native = RevisedCharts(); src = source(tmp_path,native)
    rec,armed = sc.close_scan(src,["LEAD"],NOW,preparing=True)
    candidate = next(s for s in armed if s.setup_id == "1_qullamaggie_breakout")
    rec.armed = [signal_payload(candidate)]
    log = sc.ScanLog(tmp_path/"scan")
    log.save_armed(NOW.date(),rec)
    first = sc.run(src,["LEAD"],log,NOW)
    assert first.triggered
    return src,log,candidate,first.triggered[0]["event_id"]


def test_revision_rebuilds_in_same_scan_and_only_future_crossing_triggers(tmp_path):
    src,log,old,event = seed(tmp_path)
    src.source.factor["LEAD"] = 0.9975
    src.source.now += timedelta(minutes=15)
    result = sc.run(src,["LEAD"],log,src.source.now)
    assert result.discovery["revision_rebuilds"][0]["status"] == "REBUILT"
    assert not result.triggered
    assert len(result.armed) == 1
    assert log.signals.get(event,src.source.now)["state"] == "invalidated"
    _,armed = log.load_armed(NOW.date())
    assert len(armed) == 1
    assert armed[0].trigger == pytest.approx(old.trigger*0.9975,abs=.011)
    assert candidate_id(armed[0],NOW.date()) != candidate_id(old,NOW.date())
    assert not log.signals.pending_rebuilds(NOW.date())
    # Restart: no duplicate rebuild; old crossing is not emitted under new prices.
    log = sc.ScanLog(tmp_path/"scan")
    src.source.now += timedelta(minutes=15)
    assert not sc.run(src,["LEAD"],log,src.source.now).triggered
    src.source.now += timedelta(minutes=15)
    later = sc.run(src,["LEAD"],log,src.source.now)
    assert len(later.triggered) == 1
    assert later.triggered[0]["event_id"] != event
    assert later.triggered[0]["stop"] == pytest.approx(146.5*0.9975)


def test_rebuild_outage_waits_and_recovers_without_replaying_gap(tmp_path):
    src,log,old,event = seed(tmp_path)
    src.source.factor["LEAD"] = .99
    src.source.now += timedelta(minutes=15)
    sc.intraday_scan(src,[old],src.source.now,store=log.signals)
    src.source.missing.add("QQQ")
    result = rebuild_pending(src,log,src.source.now)
    assert result[0]["status"] == "PENDING"
    assert log.signals.pending_rebuilds(NOW.date())
    src.source.missing.clear()
    src.source.now += timedelta(minutes=15)
    assert rebuild_pending(src,log,src.source.now)[0]["status"] == "REBUILT"
    _,armed = log.load_armed(NOW.date())
    assert not sc.intraday_scan(src,armed,src.source.now,store=log.signals).triggered
    assert not log.signals.get(event,src.source.now)["eligible"]


def test_unqualified_rebuild_removes_only_retired_candidate(tmp_path):
    src,log,old,event = seed(tmp_path)
    other = replace(old,symbol="OTHER")
    log.signals.save_armed(NOW.date(),"full",[signal_payload(other)],append=True)
    src.source.flat = True
    src.source.now += timedelta(minutes=15)
    sc.intraday_scan(src,[old],src.source.now,store=log.signals)
    result = rebuild_pending(src,log,src.source.now)
    assert result[0]["status"] == "NO_SETUP"
    assert [s.symbol for s in log.load_armed(NOW.date())[1]] == ["OTHER"]


def test_removed_ticker_is_not_resurrected_or_fetched_by_rebuild(tmp_path):
    src,log,old,event = seed(tmp_path)
    src.source.factor["LEAD"] = .99
    src.source.now += timedelta(minutes=15)
    sc.intraday_scan(src,[old],src.source.now,store=log.signals)
    before = len(src.source.calls)
    assert rebuild_pending(src,log,src.source.now,removed={"LEAD"})[0]["status"] == "REMOVED"
    assert len(src.source.calls) == before
    assert not log.load_armed(NOW.date())[1]


def test_actual_ex_dividend_shape_remains_conflict_not_false_rebuild_success(tmp_path):
    src,log,old,event = seed(tmp_path)
    src.source.factor["LEAD"] = .9975
    src.source.raw_anchor_unchanged = True
    src.source.now += timedelta(minutes=15)
    result = sc.run(src,["LEAD"],log,src.source.now)
    assert "DAILY_RAW_CLOSE_MISMATCH" in result.skipped["LEAD"]
    assert "revision_rebuilds" not in result.discovery
    assert not result.triggered
    assert not log.signals.get(event,src.source.now)["eligible"]


def test_first_download_and_unchanged_history_do_not_prove_volume_units(tmp_path):
    src = source(tmp_path)
    for _ in range(2):
        frame = daily(src)
        with pytest.raises(BarDataError,match="Unverified volume"):
            volume_basis(frame)


def test_genuine_gap_does_not_queue_rebuild(tmp_path):
    src,log,old,event = seed(tmp_path)
    # Independently verified history stays unchanged; a new intraday move is not
    # a revision request (normal stop/crossing lifecycle still applies).
    src.source.now += timedelta(minutes=15)
    sc.intraday_scan(src,[old],src.source.now,store=log.signals)
    assert not log.signals.pending_rebuilds(NOW.date())


@pytest.mark.parametrize("setup", ["2_minervini_vcp", "5_qullamaggie_episodic_pivot"])
def test_missing_volume_stays_pending_for_dependent_rebuild(tmp_path,setup):
    from desk.playbook.cards import CARDS
    from tests.test_scanner import daily as chart_daily
    from tests.test_triggers import VCP
    src,log,old,event = seed(tmp_path)
    if setup == "2_minervini_vcp":
        from tests.charts import line
        cl = VCP.copy()
        cl[:401] = line((0,60),(200,65),(400,150))
        cl[-1] = 145
        src.source.charts[("LEAD","D")] = chart_daily(cl,end=NOW.date()-timedelta(days=1))
    dependent = replace(old,setup_id=setup,setup_version=CARDS[setup].fingerprint())
    log.signals.save_armed(NOW.date(),"full",[signal_payload(dependent)])
    log.signals.invalidate_candidate(dependent,NOW.date(),NOW,"fixture revision",rebuild=True)
    result = rebuild_pending(src,log,NOW)
    assert result[0]["status"] == "PENDING"
    assert "volume" in result[0]["reason"].lower()


def test_empty_rebuild_queue_performs_no_provider_requests(tmp_path):
    src,log,old,event = seed(tmp_path)
    before = len(src.source.calls)
    assert rebuild_pending(src,log,NOW) == []
    assert len(src.source.calls) == before


def test_retired_history_reversal_does_not_abort_healthy_scan(tmp_path):
    src,log,old,event = seed(tmp_path)
    src.source.factor["LEAD"] = .99
    src.source.now += timedelta(minutes=15)
    sc.intraday_scan(src,[old],src.source.now,store=log.signals)
    src.source.factor["LEAD"] = 1
    result = rebuild_pending(src,log,src.source.now)
    assert result[0]["status"] == "PENDING"
    assert "retired history" in result[0]["reason"]
    assert log.signals.get(event,src.source.now)["state"] == "invalidated"


def test_dividend_only_daily_adjustment_reconciles_rebuilds_and_triggers_future_crossing(tmp_path):
    from desk.batch_actions import BatchActions, POLICY
    from tests.test_batch_actions import Feed, dividend, KEY
    src,log,old,event = seed(tmp_path)
    cash = float(src.source.charts[("LEAD","D")].close.iloc[-1])*.0025
    feed = Feed(dividends=[dividend(amount=cash)])
    src.actions = BatchActions(tmp_path/'auto.sqlite',KEY,clock_fn=lambda:src.source.now,
                              transport=feed,volume_policy=POLICY)
    src.source.factor['LEAD']=.9975
    src.source.raw_anchor_unchanged=True
    src.source.now+=timedelta(minutes=15)
    result=sc.run(src,['LEAD'],log,src.source.now)
    assert result.discovery['revision_rebuilds'][0]['status']=='REBUILT'
    assert not result.triggered
    _,armed=log.load_armed(NOW.date())
    proof=armed[0].price_basis['anchor_adjustment']
    assert proof['kind']=='cash_dividend' and proof['value']==str(cash)
    assert armed[0].price_basis['raw_anchor_close'] == 148.0
    assert armed[0].price_basis['daily_anchor_close'] == pytest.approx(148*.9975)
    assert not log.signals.get(event,src.source.now)['eligible']
    # All symbols share one split snapshot + one dividend snapshot.
    assert len(feed.calls)==2
    src.source.now+=timedelta(minutes=15)
    assert not sc.run(src,['LEAD'],log,src.source.now).triggered
    src.source.now+=timedelta(minutes=15)
    result=sc.run(src,['LEAD'],log,src.source.now)
    assert len(result.triggered)==1 and result.triggered[0]['event_id']!=event
    assert result.triggered[0]['stop']==pytest.approx(146.5*.9975)


def test_rebuild_does_not_resurrect_candidate_withdrawn_during_fetch(tmp_path):
    src,log,old,event=seed(tmp_path)
    src.source.factor['LEAD']=.99
    src.source.now+=timedelta(minutes=15)
    sc.intraday_scan(src,[old],src.source.now,store=log.signals)
    _,fresh=sc.close_scan(src,['LEAD'],src.source.now,preparing=True)
    replacement=next(s for s in fresh if s.setup_id==old.setup_id)
    log.signals.save_armed(NOW.date(),'full',[])
    assert not log.signals.finish_rebuild(candidate_id(old,NOW.date()),src.source.now,'REBUILT','new chart',replacement)
    assert not log.load_armed(NOW.date())[1]
    assert not log.signals.pending_rebuilds(NOW.date())
