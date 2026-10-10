"""G5 backlog repair R1: publication chronology for candidates armed after the close job.

Taz picks and delayed-volume setups prepared during the target session get the same
no-replay floor as a mid-session close publication: the actual publication clock,
read inside the final SQLite transaction after lock waits and provider preparation.
Synthetic fixtures only; no network, no order.
"""
from datetime import date
import json
import sqlite3
import threading

import pytest

from desk import scanner as sc
from desk.calendar import clock
from desk.signal_state import SignalStateError, SignalStore, _time, candidate_id, signal_payload
from tests.test_close_jobs import BREAKOUT, LISTS, MON, TUE, at, charts
from tests.test_scanner import Fake, m15, sig

FLAT = (147, 148.5, 146.5, 148)
LATE_BREAK = [FLAT, FLAT, FLAT, BREAKOUT[1]]          # crossing completes 10:30


def floors(store):
    with store._db() as db:
        return {r["candidate_id"]: r["start_after"] for r in db.execute("SELECT * FROM armed_floors")}


def pick(root, *names):
    (root / "taz-picks.json").write_text(json.dumps({"add": list(names), "remove": []}))


def lead(log, day=TUE):
    return [s for s in log.load_armed(day)[1] if s.symbol == "LEAD"]


def lead_hits(rec):
    return [h for h in rec.triggered if h["symbol"] == "LEAD"]


# ---- Taz picks through scanner.run -----------------------------------------------------------
def test_a_pick_prepared_mid_session_never_replays_an_earlier_crossing(tmp_path):
    log = sc.ScanLog(tmp_path)
    src = Fake({**charts(MON), ("LEAD", "M15"): m15(BREAKOUT, TUE)}, lists=LISTS)
    sc.run(src, ["IWM"], log, at(MON, 16, 14))                  # LEAD not on the list at the close
    assert not lead(log)
    pick(tmp_path, "LEAD")
    rec = sc.run(src, ["IWM"], log, at(TUE, 10, 1))              # prepared and published at 10:01
    assert not rec.error and lead(log)
    assert not lead_hits(rec)                                    # the 10:00 crossing is not replayed
    assert {floors(log.signals)[candidate_id(s, TUE)] for s in lead(log)} == {_time(at(TUE, 10, 1))}


def test_control_a_pick_present_at_the_close_still_takes_the_first_crossing(tmp_path):
    log = sc.ScanLog(tmp_path)
    pick(tmp_path, "LEAD")
    src = Fake({**charts(MON), ("LEAD", "M15"): m15(BREAKOUT, TUE)}, lists=LISTS)
    sc.run(src, ["IWM"], log, at(MON, 16, 14))                  # pre-open publication
    rec = sc.run(src, ["IWM"], log, at(TUE, 10, 1))
    assert lead_hits(rec) and all(clock(h["trigger_at"]) == at(TUE, 10, 0) for h in lead_hits(rec))
    assert not {candidate_id(s, TUE) for s in lead(log)} & set(floors(log.signals))


def test_a_later_crossing_triggers_after_a_process_reopen_and_the_floor_is_not_revised(tmp_path):
    src = Fake({**charts(MON), ("LEAD", "M15"): m15(LATE_BREAK, TUE)}, lists=LISTS)
    sc.run(src, ["IWM"], sc.ScanLog(tmp_path), at(MON, 16, 14))
    pick(tmp_path, "LEAD")
    first = sc.run(src, ["IWM"], sc.ScanLog(tmp_path), at(TUE, 10, 1))
    assert not lead_hits(first)
    before = floors(SignalStore(tmp_path / "signals.sqlite"))
    assert before
    later = sc.run(src, ["IWM"], sc.ScanLog(tmp_path), at(TUE, 10, 31))   # a new process
    hits = lead_hits(later)
    assert hits and all(clock(h["trigger_at"]) == at(TUE, 10, 30) for h in hits)
    assert floors(SignalStore(tmp_path / "signals.sqlite")) == before


def test_the_floor_is_the_clock_after_provider_preparation(tmp_path, monkeypatch):
    now = {"t": at(TUE, 10, 1)}

    class Slow(Fake):                                            # each daily fetch takes two minutes
        def bars(self, symbols, *, timespan, **kw):
            if timespan == "D" and clock(now["t"]) >= at(TUE, 9, 30):
                now["t"] = clock(now["t"]) + (at(TUE, 10, 3) - at(TUE, 10, 1))
            return super().bars(symbols, timespan=timespan, **kw)

    seen = []
    publish = SignalStore.publish_armed
    def spy(self, day, market, payload, **kw):
        seen.append((clock(kw["now"]), clock(now["t"])))         # (evaluation, clock at publication)
        return publish(self, day, market, payload, **kw)
    monkeypatch.setattr(SignalStore, "publish_armed", spy)
    log = sc.ScanLog(tmp_path)
    src = Slow({**charts(MON), ("LEAD", "M15"): m15(BREAKOUT, TUE)}, lists=LISTS)
    sc.run(src, ["IWM"], log, at(MON, 16, 14), decision_clock=lambda: at(MON, 16, 14))
    pick(tmp_path, "LEAD")
    rec = sc.run(src, ["IWM"], log, at(TUE, 10, 1), decision_clock=lambda: now["t"])
    assert not rec.error, rec.error
    [(evaluated, published)] = seen
    assert at(TUE, 10, 1) < evaluated <= published               # after the provider's daily fetches
    assert {floors(log.signals)[candidate_id(s, TUE)] for s in lead(log)} == {_time(published)}
    assert not lead_hits(rec)


def test_a_failed_preparation_adds_neither_candidate_nor_floor_and_retries_later(tmp_path):
    log = sc.ScanLog(tmp_path)
    src = Fake({**charts(MON), ("LEAD", "M15"): m15(LATE_BREAK, TUE)}, lists=LISTS)
    sc.run(src, ["IWM"], log, at(MON, 16, 14))
    pick(tmp_path, "LEAD")
    src.fail = {"SPY"}                                           # market read fails: preparation error
    rec = sc.run(src, ["IWM"], log, at(TUE, 10, 1))
    assert rec.discovery["user_preparation"]["error"]
    assert not lead(log) and not floors(log.signals) and "LEAD" not in log.prepared_users(TUE)
    src.fail = set()
    sc.run(src, ["IWM"], log, at(TUE, 10, 16))
    assert {floors(log.signals)[candidate_id(s, TUE)] for s in lead(log)} == {_time(at(TUE, 10, 16))}


def test_a_refused_publication_writes_nothing_and_leaves_the_pick_unprepared(tmp_path):
    log = sc.ScanLog(tmp_path)
    src = Fake({**charts(MON), ("LEAD", "M15"): m15(BREAKOUT, TUE)}, lists=LISTS)
    sc.run(src, ["IWM"], log, at(MON, 16, 14))
    pick(tmp_path, "LEAD")
    calls = []

    def backwards():                                             # the publication clock precedes evaluation
        calls.append(1)
        return at(TUE, 10, 1) if len(calls) == 1 else at(TUE, 9, 59)
    history = len(log.signals.armed_history(TUE))
    rec = sc.run(src, ["IWM"], log, at(TUE, 10, 1), decision_clock=backwards)
    assert rec.error and "precedes" in rec.error
    assert not lead(log) and not floors(log.signals) and "LEAD" not in log.prepared_users(TUE)
    assert len(log.signals.armed_history(TUE)) == history


# ---- volume-pending candidates ---------------------------------------------------------------
def test_volume_setups_prepared_after_the_open_get_a_floor_and_close_peers_keep_none(tmp_path, monkeypatch):
    from desk.playbook.filters import GateResult
    from tests.alpaca_support import AlpacaSim, Clock, dry_daily, with_volume
    from tests.test_scanner import daily, frames
    from tests.test_triggers import CUP
    monkeypatch.setattr(sc, "trend_template", lambda f, spy: GateResult({"fixture template": True}))
    day0, entry = date(2026, 9, 29), date(2026, 9, 30)
    fresh = {**frames(day0), ("LEAD", "D"): daily(CUP, end=day0)}
    sim = AlpacaSim(symbols=["LEAD", "SPY", "QQQ", "IWM"], daily=dry_daily(day0, days=12))
    tick, log = Clock(at(day0, 16, 12)), sc.ScanLog(tmp_path / "scan")
    watch = {"LEAD": ["leader scan"]}
    close = sc.run(with_volume(Fake(fresh), tmp_path / "volume.sqlite", sim, tick), watch, log, tick.at)
    assert not close.error, close.error
    close_ids = {candidate_id(s, entry) for s in log.load_armed(entry)[1]}
    assert close_ids and close.discovery["volume_pending"] == ["LEAD"]
    tick.at = at(entry, 9, 47)
    morning = sc.run(with_volume(Fake(fresh), tmp_path / "volume.sqlite", sim, tick), watch,
                     sc.ScanLog(tmp_path / "scan"), tick.at)
    armed = morning.discovery["volume_preparation"]["armed"]
    assert {a["setup_id"] for a in armed} == {"2_minervini_vcp", "3_oneil_cup_with_handle"}
    got = floors(log.signals)
    volume_ids = {candidate_id(s, entry) for s in log.load_armed(entry)[1]} - close_ids
    assert volume_ids and {got[c] for c in volume_ids} == {_time(at(entry, 9, 47))}
    assert not close_ids & set(got)                              # close peers keep no floor


# ---- the store's publication transaction -----------------------------------------------------
def payloads(*setups):
    return [signal_payload(sig(s)) for s in setups]


def test_store_publication_before_and_across_the_open(tmp_path):
    day = date(2026, 9, 29)
    pre = SignalStore(tmp_path / "a.sqlite")
    t = pre.publish_armed(day, "full", payloads("1_qullamaggie_breakout"),
                          now=at(day, 9, 20), decision_clock=lambda: at(day, 9, 25))
    assert t == at(day, 9, 25) and not floors(pre)               # genuine pre-open preparation
    across = SignalStore(tmp_path / "b.sqlite")
    across.publish_armed(day, "full", payloads("1_qullamaggie_breakout"),
                         now=at(day, 9, 29), decision_clock=lambda: at(day, 9, 31))
    assert set(floors(across).values()) == {_time(at(day, 9, 31))}


def test_store_keeps_old_peers_and_their_floors(tmp_path):
    day = date(2026, 9, 29)
    store = SignalStore(tmp_path / "s.sqlite")
    store.save_armed(day, "full", payloads("2_minervini_vcp"))   # close publication, no floor
    store.publish_armed(day, "full", payloads("1_qullamaggie_breakout"), now=at(day, 10, 1))
    first = floors(store)
    store.publish_armed(day, "partial", payloads("1_qullamaggie_breakout", "7_luk_pullback_reclaim"),
                        now=at(day, 10, 20))
    after = floors(store)
    ids = {s: candidate_id(sig(s), day) for s in ("1_qullamaggie_breakout", "2_minervini_vcp", "7_luk_pullback_reclaim")}
    assert after[ids["1_qullamaggie_breakout"]] == first[ids["1_qullamaggie_breakout"]] == _time(at(day, 10, 1))
    assert after[ids["7_luk_pullback_reclaim"]] == _time(at(day, 10, 20))
    assert ids["2_minervini_vcp"] not in after
    market, armed = store.load_armed(day)
    assert market == "full" and {candidate_id(sig(p["setup_id"]), day) for p in armed} == set(ids.values())


def test_store_refuses_a_backwards_publication_clock(tmp_path):
    day = date(2026, 9, 29)
    store = SignalStore(tmp_path / "s.sqlite")
    with pytest.raises(SignalStateError, match="precedes"):
        store.publish_armed(day, "full", payloads("1_qullamaggie_breakout"),
                            now=at(day, 10, 1), decision_clock=lambda: at(day, 10, 0))
    assert store.load_armed(day) is None and not floors(store) and not store.armed_history(day)


def test_store_reads_the_publication_clock_after_a_competing_writer(tmp_path):
    day = date(2026, 9, 29)
    store = SignalStore(tmp_path / "s.sqlite")
    shared = {"t": at(day, 10, 1)}
    locked, release = threading.Event(), threading.Event()

    def writer():
        db = sqlite3.connect(tmp_path / "s.sqlite", timeout=10, isolation_level=None)
        db.execute("BEGIN EXCLUSIVE")
        locked.set()
        release.wait(5)
        shared["t"] = at(day, 10, 5)                             # time passes while the lock is held
        db.execute("COMMIT")
        db.close()
    blocker = threading.Thread(target=writer)
    blocker.start()
    assert locked.wait(5)
    threading.Timer(0.3, release.set).start()
    published = store.publish_armed(day, "full", payloads("1_qullamaggie_breakout"),
                                    now=at(day, 10, 1), decision_clock=lambda: shared["t"])
    blocker.join(5)
    assert published == at(day, 10, 5)
    assert set(floors(store).values()) == {_time(at(day, 10, 5))}


def test_store_never_revises_a_floor_row_even_after_a_withdrawal(tmp_path):
    day = date(2026, 9, 29)
    store = SignalStore(tmp_path / "s.sqlite")
    store.publish_armed(day, "full", payloads("1_qullamaggie_breakout"), now=at(day, 10, 1))
    store.save_armed(day, "full", [])                            # Taz withdraws the candidate
    store.publish_armed(day, "full", payloads("1_qullamaggie_breakout"), now=at(day, 10, 20))
    assert set(floors(store).values()) == {_time(at(day, 10, 1))}
