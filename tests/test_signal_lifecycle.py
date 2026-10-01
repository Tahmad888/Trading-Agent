"""Deterministic lifecycle acceptance; synthetic prices, no trading-edge claims."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime
import json

import pandas as pd
import pytest

from desk import scanner as sc
from desk.signal_state import SignalStore
from tests.test_scanner import DAY, ET, Fake, m15, sig


def now(hour=10, minute=0, day=DAY):
    return datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(hour=hour, minute=minute)


BASE = [(99, 101, 98, 100), (100, 102, 99.5, 101.5)]


def mirror(rows):
    return [(200-o, 200-l, 200-h, 200-c) for o,h,l,c in rows]


def setup(direction):
    return sig("8_raschke_holy_grail", direction, 100, 95 if direction == "long" else 105)


def process(store, rows=BASE, at=None, direction="long", signal=None):
    return sc.observe_signal(store, signal or setup(direction),
                             m15(rows if direction == "long" else mirror(rows)), at or now())


@pytest.mark.parametrize("direction", ["long", "short"])
def test_duplicate_restart_and_first_trigger_time(tmp_path, direction):
    path = tmp_path / "signals.sqlite"
    first = process(SignalStore(path), direction=direction)[0]
    assert first["state"] == "triggered" and first["eligible"]
    assert pd.Timestamp(first["trigger_at"]) == pd.Timestamp(now())
    restarted = SignalStore(path)
    assert process(restarted, at=now(minute=1), direction=direction) == []
    assert len(restarted.events(now(minute=1))) == 1
    assert len(restarted.history(first["id"])) == 1


@pytest.mark.parametrize("direction", ["long", "short"])
def test_failed_breakout_then_new_crossing_is_a_new_event(tmp_path, direction):
    store = SignalStore(tmp_path / "s.db")
    first = process(store, direction=direction)[0]
    failed = BASE + [(101.5, 102, 99.5, 100)]
    assert process(store, failed, now(minute=15), direction) == []
    assert store.get(first["id"], now(minute=15))["state"] == "invalidated"
    recovered = failed + [(100, 102, 99.8, 101.8)]
    second = process(store, recovered, now(minute=30), direction)[0]
    assert first["id"] != second["id"]
    assert len(store.events(now(minute=30))) == 2


@pytest.mark.parametrize("direction", ["long", "short"])
def test_stop_blocks_rebound_until_new_setup_evidence(tmp_path, direction):
    store = SignalStore(tmp_path / "s.db")
    first = process(store, direction=direction)[0]
    stopped = BASE + [(101.5, 102, 94, 100)]
    assert process(store, stopped, now(minute=15), direction) == []
    assert store.get(first["id"], now(minute=15))["state"] == "invalidated"
    assert process(store, stopped + [(100, 102, 99, 101.5)], now(minute=30), direction) == []


@pytest.mark.parametrize("direction", ["long", "short"])
def test_ambiguous_stop_and_trigger_bar_never_eligible(tmp_path, direction):
    rows = [BASE[0], (100, 102, 94, 101.5)]
    store = SignalStore(tmp_path / "s.db")
    assert process(store, rows, direction=direction) == []
    assert store.events(now()) == []


def test_failed_historical_hit_not_reissued(tmp_path):
    store = SignalStore(tmp_path / "s.db")
    failed = BASE + [(101.5, 102, 99, 100)]
    assert process(store, failed, now(minute=15)) == []
    events = store.events(now(minute=15))
    assert len(events) == 1 and events[0]["state"] == "invalidated"
    assert pd.Timestamp(events[0]["trigger_at"]) == pd.Timestamp(now())
    assert pd.Timestamp(events[0]["observed_at"]) == pd.Timestamp(now(minute=15))


def test_freshness_deadline_and_session_expiry_without_bars(tmp_path):
    log = sc.ScanLog(tmp_path)
    event = process(log.signals)[0]
    assert log.signals.get(event["id"], now(minute=14))["eligible"]
    assert not log.signals.get(event["id"], now(minute=15))["eligible"]
    assert sc.run(Fake({}), [], log, now(hour=20)) is None
    assert log.signals.get(event["id"], now(hour=20))["state"] == "expired"


def test_missing_bars_suspend_then_valid_data_recovers_same_event(tmp_path):
    log = sc.ScanLog(tmp_path)
    event = process(log.signals)[0]
    rec = sc.intraday_scan(Fake({}), [setup("long")], now(minute=1), store=log.signals)
    assert rec.skipped and not log.signals.get(event["id"], now(minute=1))["eligible"]
    assert process(log.signals, at=now(minute=2)) == []
    assert log.signals.get(event["id"], now(minute=2))["eligible"]


def test_correction_invalidates_instead_of_rewriting_trigger_history(tmp_path):
    store = SignalStore(tmp_path / "s.db")
    event = process(store)[0]
    corrected = [BASE[0], (100, 103, 99.5, 101.5)]
    assert process(store, corrected, now(minute=1)) == []
    current = store.get(event["id"], now(minute=1))
    assert current["state"] == "invalidated" and "revised" in current["reason"]
    assert process(store, at=now(minute=2)) == []


def test_closed_event_requires_reset_then_new_crossing(tmp_path):
    store = SignalStore(tmp_path / "s.db")
    event = process(store)[0]
    store.close(event["id"], now(minute=1), "review completed without order")
    above = BASE + [(101.5, 103, 101.1, 102)]
    assert process(store, above, now(minute=15)) == []
    reset = above + [(102, 103, 99.5, 100)]
    assert process(store, reset, now(minute=30)) == []
    new = process(store, reset + [(100, 102, 99, 101.5)], now(minute=45))[0]
    assert new["id"] != event["id"]


def test_concurrent_replay_creates_one_event(tmp_path):
    path = tmp_path / "s.db"
    stores = [SignalStore(path), SignalStore(path)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(process, stores))
    assert sum(map(len, results)) == 1
    assert len(stores[0].events(now())) == 1


def test_transaction_rolls_back_partial_observations(tmp_path):
    store = SignalStore(tmp_path / "s.db")
    obs = sc.entry_observations(setup("long"), m15(BASE))
    broken = [*obs, {**obs[-1], "bar_end": now(minute=15).isoformat()}]
    with pytest.raises(ValueError, match="Future"):
        store.observe(setup("long"), DAY, broken, now())
    assert store.events(now()) == []
    assert len(process(store)) == 1


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("fault", ["ok", "chase", "stop", "wrong_side", "stale", "future", "symbol", "missing", "nan"])
def test_delayed_review_direction_and_fresh_data(tmp_path, direction, fault):
    log = sc.ScanLog(tmp_path)
    event = process(log.signals, direction=direction)[0]
    price = {"chase":104, "stop":94, "wrong_side":100, "nan":float("nan")}.get(fault,101.5)
    if direction == "short":
        price = 200-price
    rows = BASE if direction == "long" else mirror(BASE)
    source = Fake({} if fault == "missing" else {("LEAD","M15"):m15(rows)})
    at = now(minute=1)
    q = now(hour=9,minute=59) if fault == "stale" else now(minute=2) if fault == "future" else at
    result = sc.revalidate_signal(source,log,event["id"],at,symbol="OTHER" if fault=="symbol" else "LEAD",price=price,quote_at=q)
    assert result["eligible"] == (fault == "ok"), result


def test_delayed_review_cannot_reuse_stale_bars(tmp_path):
    log=sc.ScanLog(tmp_path)
    event=process(log.signals)[0]
    result=sc.revalidate_signal(Fake({("LEAD","M15"):m15(BASE)}),log,event["id"],now(minute=15),symbol="LEAD",price=101.5,quote_at=now(minute=15))
    assert not result["eligible"]


def test_early_close_and_next_day_expiry(tmp_path):
    day=date(2026,11,27)
    s=replace(setup("long"), as_of=pd.Timestamp("2026-11-25",tz=ET))
    store=SignalStore(tmp_path/"s.db")
    event=sc.observe_signal(store,s,m15(BASE,day),now(day=day))[0]
    assert pd.Timestamp(event["expires_at"]) == pd.Timestamp(now(hour=13,day=day))
    assert store.get(event["id"],now(hour=13,day=day))["state"]=="expired"


def test_legacy_armed_lists_migrate_only_candidates_and_preserve_original(tmp_path):
    log=sc.ScanLog(tmp_path)
    p=tmp_path/f"armed-{DAY}.json"
    p.write_text(json.dumps({"market":"full","signals":[sc._sig(setup("long"))]}))
    before=p.read_text()
    assert len(log.load_armed(DAY)[1])==1
    assert log.signals.events(now())==[] and p.read_text()==before
    log.add_armed(DAY,sc.MarketSize.FULL,[setup("long")])
    assert len(sc.ScanLog(tmp_path).load_armed(DAY)[1])==1


def test_full_scanner_restart_no_duplicate_and_funnel_counts_unique(tmp_path):
    log=sc.ScanLog(tmp_path)
    log.save_armed(DAY,sc.ScanRecord("close","",None,market="full",armed=[sc._sig(setup("long"))]))
    first=sc.run(Fake({("LEAD","M15"):m15(BASE)}),[],log,now())
    assert len(first.triggered)==1, first
    extended=BASE+[(101.5,103,101.2,102)]
    second=sc.run(Fake({("LEAD","M15"):m15(extended)}),[],sc.ScanLog(tmp_path),now(minute=15))
    assert second.triggered==[] and second.error is None
    assert sc.funnel([*log.records(),log.records()[0]],DAY,DAY)["entries_triggered"]==1


@pytest.mark.parametrize("value", [float("nan"),float("inf"),0,-1])
def test_signal_numeric_validation(value):
    with pytest.raises(ValueError):
        replace(setup("long"),trigger=value)


def test_card_version_change_removes_eligibility(tmp_path,monkeypatch):
    store=SignalStore(tmp_path/"s.db")
    e=process(store)[0]
    card=sc.CARDS[setup("long").setup_id]
    monkeypatch.setitem(sc.CARDS,card.id,card.model_copy(update={"entry":"changed terms"}))
    assert not store.get(e["id"],now())["eligible"]


def test_replacement_evidence_cannot_recycle_historical_crossing(tmp_path):
    store=SignalStore(tmp_path/"s.db")
    original=process(store)[0]
    changed=replace(setup("long"),stop=94)
    assert process(store,at=now(minute=1),signal=changed)==[]
    assert store.get(original["id"],now(minute=1))["state"]=="invalidated"
    reset=BASE+[(101.5,102,99,100)]
    assert process(store,reset,now(minute=15),signal=changed)==[]
    events=process(store,reset+[(100,102,99,101.5)],now(minute=30),signal=changed)
    assert len(events)==1 and events[0]["id"]!=original["id"]
    # Replaying an obsolete candidate must not invalidate its replacement.
    assert process(store,reset,now(minute=15))==[]
    assert store.get(events[0]["id"],now(minute=30))["eligible"]


def test_preclosure_reset_bar_does_not_count_as_new_reset(tmp_path):
    store=SignalStore(tmp_path/"s.db")
    event=process(store)[0]
    store.close(event["id"],now(minute=20),"closed after missing scan")
    rows=BASE+[(101.5,102,99,100),(100,102,99,101.5)]
    assert process(store,rows,now(minute=30))==[]
    assert len(store.events(now(minute=30)))==1


def test_ep_alternatives_share_one_event_and_no_11am_deadline(tmp_path):
    from tests.test_ep_timing import ep, flat_session
    store=SignalStore(tmp_path/"s.db")
    frame=flat_session(8).astype(float)
    # Fresh 11:15 bar crosses both the 15-minute and 60-minute ranges.
    frame.loc[frame.index[-1], ["open","high","low","close"]]=[100,103,99.5,102]
    signal=ep()
    events=sc.observe_signal(store,signal,frame,now(hour=11,minute=30))
    assert len(events)==1
    assert sc.observe_signal(store,signal,frame,now(hour=11,minute=31))==[]


def test_action_basis_change_suspends_existing_event(tmp_path):
    log=sc.ScanLog(tmp_path)
    event=process(log.signals)[0]
    frame=m15(BASE)
    frame.attrs["bar_provenance"]["price_basis"]["coverage_complete"]=False
    rec=sc.intraday_scan(Fake({("LEAD","M15"):frame}),[setup("long")],now(minute=1),store=log.signals)
    assert rec.skipped and not log.signals.get(event["id"],now(minute=1))["eligible"]


def test_event_survives_failure_to_write_scan_log(tmp_path,monkeypatch):
    log=sc.ScanLog(tmp_path)
    log.save_armed(DAY,sc.ScanRecord("close","",None,market="full",armed=[sc._sig(setup("long"))]))
    def fail(_):
        raise OSError("disk write failed")
    monkeypatch.setattr(log,"write",fail)
    source=Fake({("LEAD","M15"):m15(BASE)})
    with pytest.raises(OSError):
        sc.run(source,[],log,now())
    recovered=sc.ScanLog(tmp_path)
    assert len(recovered.signals.events(now()))==1
    assert sc.run(source,[],recovered,now()).triggered==[]
    assert len(recovered.signals.events(now()))==1


def test_near_close_rsi_estimate_is_same_session_only(tmp_path):
    day=DAY
    rows=[(99,101,98,100)]*25
    frame=m15(rows)
    signal=replace(sig("10_connors_rsi2"),symbol="LEAD",as_of=pd.Timestamp(day,tz=ET))
    store=SignalStore(tmp_path/"s.db")
    result=sc.observe_signal(store,signal,frame,now(hour=15,minute=45))
    assert len(result)==1
    assert store.get(result[0]["id"],now(hour=16))["state"]=="expired"


def test_invalid_short_equal_stop_is_rejected():
    with pytest.raises(ValueError):
        replace(setup("short"),stop=100)


def test_loaded_old_card_cannot_be_promoted_by_current_code(tmp_path,monkeypatch):
    log=sc.ScanLog(tmp_path)
    old=setup("long")
    log.save_armed(DAY,sc.ScanRecord("close","",None,market="full",armed=[sc._sig(old)]))
    card=sc.CARDS[old.setup_id]
    monkeypatch.setitem(sc.CARDS,card.id,card.model_copy(update={"entry":"changed terms"}))
    restored=sc.ScanLog(tmp_path).load_armed(DAY)[1]
    rec=sc.intraday_scan(Fake({("LEAD","M15"):m15(BASE)}),restored,now(),store=log.signals)
    assert not rec.triggered and "version" in rec.skipped["LEAD"]


def test_unversioned_legacy_candidate_requires_rebuild(tmp_path):
    payload=sc._sig(setup("long"))
    payload.pop("setup_version")
    (tmp_path/f"armed-{DAY}.json").write_text(json.dumps({"market":"full","signals":[payload]}))
    log=sc.ScanLog(tmp_path)
    rec=sc.intraday_scan(Fake({("LEAD","M15"):m15(BASE)}),log.load_armed(DAY)[1],now(),store=log.signals)
    assert not rec.triggered and "version" in rec.skipped["LEAD"]


def test_armed_history_preserved_when_current_list_is_replaced(tmp_path):
    log=sc.ScanLog(tmp_path)
    rec=sc.ScanRecord("close","",None,market="full",armed=[sc._sig(setup("long"))])
    log.save_armed(DAY,rec)
    log.save_armed(DAY,rec)
    log.save_armed(DAY,sc.ScanRecord("close","",None,market="full"))
    assert len(log.signals.armed_history(DAY))==2
    assert log.load_armed(DAY)[1]==[]


def test_stage4_direct_entry_and_luk_reclaim(tmp_path):
    for s,rows in [(sig("9_weinstein_stage4_breakdown","short",100,105),[(100,101,98,99)]),
                   (sig("7_luk_pullback_reclaim"),[(100.5,101,99,99.5),(99.5,101,99,100.5)])]:
        store=SignalStore(tmp_path/(s.setup_id+".db"))
        at=now(hour=9,minute=45) if len(rows)==1 else now()
        assert len(sc.observe_signal(store,s,m15(rows),at))==1


def test_clock_cannot_read_future_validated_state(tmp_path):
    store=SignalStore(tmp_path/"s.db")
    event=process(store)[0]
    process(store,BASE+[(101.5,103,101.2,102)],now(minute=16))
    assert not store.get(event["id"],now(minute=15))["eligible"]
    with pytest.raises(ValueError,match="backwards"):
        process(store,at=now(minute=1))


def test_expired_review_uses_no_provider_calls(tmp_path):
    log=sc.ScanLog(tmp_path)
    e=process(log.signals)[0]
    source=Fake({})
    result=sc.revalidate_signal(source,log,e["id"],now(hour=16),symbol="LEAD",price=101.5,quote_at=now(hour=16))
    assert not result["eligible"] and source.calls==[]


def test_stale_stop_quote_cannot_invalidate_but_fresh_stop_can(tmp_path):
    log=sc.ScanLog(tmp_path)
    e=process(log.signals)[0]
    source=Fake({("LEAD","M15"):m15(BASE)})
    sc.revalidate_signal(source,log,e["id"],now(minute=2),symbol="LEAD",price=94,quote_at=now())
    assert log.signals.get(e["id"],now(minute=2))["state"]=="triggered"
    sc.revalidate_signal(source,log,e["id"],now(minute=3),symbol="LEAD",price=94,quote_at=now(minute=3))
    assert log.signals.get(e["id"],now(minute=3))["state"]=="invalidated"
