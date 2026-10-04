"""G5a checkpoint 2, sections 10.8-10.9 and the scanner timing paths: partial discovery
publication, retained lists, crash consistency and next-slot volume preparation.

All Webull charts, rankings and Alpaca replies are synthetic fixtures (labelled).
"""
from datetime import date, datetime, timedelta
import json

import numpy as np
import pytest

from desk import scanner as sc
from desk.bars import BarDataError
from desk.playbook.filters import GateResult
from tests.alpaca_support import AlpacaSim, Clock, dry_daily, with_volume
from tests.test_scanner import ET, Fake, daily, frames, m15
from tests.test_triggers import CUP, EP, QULL

FRIDAY = date(2026, 10, 2)


def at(day=FRIDAY, hour=16, minute=40):
    return datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(hour=hour, minute=minute)


def universe(*names, price=150):
    return {"MONTH_3": [{"symbol": s, "price": price} for s in names]}


def fake(day=FRIDAY, leaders=("LEAD",), missing=(), lists=None, **kw):
    fr = frames(day)
    for s in leaders:
        fr[(s, "D")] = daily(QULL, spread=0.02, end=day)
    return Fake(fr, lists=lists or universe(*leaders, *missing), **kw)


def picks(log, add=(), remove=()):
    (log.root / "taz-picks.json").write_text(json.dumps({"add": list(add), "remove": list(remove)}))


def counts_reconcile(rec):
    build = rec.discovery["build"]
    return sum(build["stage_counts"].values()) == build["candidates"]


# ---------------------------------------------------------------- 10.8 ----

def test_healthy_and_failed_candidates_publish_partial_with_every_exclusion(tmp_path):
    log = sc.ScanLog(tmp_path)
    rec = sc.leader_scan_job(fake(leaders=("LEAD", "ALSO"), missing=("MISSING", "GONE")), log, at())
    build = json.loads((tmp_path / "watchlist-build.json").read_text())
    assert rec.discovery["watchlist_build"]["status"] == "PARTIAL" and not rec.error
    assert sorted(l["symbol"] for l in rec.leaders) == ["ALSO", "LEAD"]
    outcomes = build["build"]["outcomes"]
    assert outcomes["MISSING"] == {"outcome": "source_failure", "stage": "bars", "reason": "no bars returned"}
    assert outcomes["GONE"]["outcome"] == "source_failure"
    assert sorted(build["build"]["population"]) == ["ALSO", "LEAD"] and counts_reconcile(rec)
    assert build["generation"] == 1 and build["published_at"] == at().isoformat()
    assert build["last_complete_at"] is None                    # a partial is not a complete success
    assert json.loads((tmp_path / "watchlist.json").read_text()) == build["list"]


def test_ranking_math_is_unchanged_over_the_disclosed_population(monkeypatch):
    import pandas as pd
    from desk import watchlist
    monkeypatch.setattr(watchlist, "trend_template", lambda f, spy: GateResult({"fixture template": True}))
    ramp = np.linspace(0, 30, len(QULL))
    fr = {s: daily(c, spread=0.02, end=FRIDAY) for s, c in (("A", QULL), ("B", QULL + ramp), ("C", QULL - ramp))}
    spy = daily(QULL, end=FRIDAY)["close"]
    full = watchlist.leader_scan(fr, spy)
    without = watchlist.leader_scan({s: f for s, f in fr.items() if s != "C"}, spy)
    assert without.population == [s for s in full.population if s != "C"] and len(without.leaders) == 2
    # Percentile ranks are recomputed over this build's population only (no stale scores).
    table = pd.DataFrame({s: watchlist._strength(fr[s]["close"]) for s in ("A", "B")}).T
    expected = (table.rank(pct=True).mean(axis=1) * 100).round(1)
    assert {l.symbol: l.score for l in without.leaders} == {s: float(expected[s]) for s in ("A", "B")}
    assert {l.symbol: l.score for l in without.leaders} != {l.symbol: l.score for l in full.leaders if l.symbol != "C"}


def test_zero_leaders_complete_is_empty_and_incomplete_without_leaders_retains(tmp_path):
    log = sc.ScanLog(tmp_path)
    log.write_watchlist({"OLD": ["leader scan"]})
    cheap = fake(leaders=(), lists=universe("PENNY", price=150))
    cheap.frames[("PENNY", "D")] = daily(np.full(300, 5.0), end=FRIDAY)       # under $10 on our own bars
    empty = sc.leader_scan_job(cheap, log, at())
    assert empty.discovery["watchlist_build"]["status"] == "EMPTY" and not empty.error
    assert empty.discovery["build"]["stage_counts"] == {"criterion:price": 1}
    assert "OLD" not in json.loads((tmp_path / "watchlist.json").read_text())
    incomplete = sc.leader_scan_job(fake(leaders=(), missing=("MISSING",)), log, at(minute=50))
    status = incomplete.discovery["watchlist_build"]
    assert status["status"] == "INCOMPLETE" and status["retained"] and status["generation"] == 1
    assert status["published_at"] == at().isoformat() and incomplete.error


@pytest.mark.parametrize("kind", ["benchmark", "universe"])
def test_benchmark_or_universe_failure_retains_the_previous_publication(tmp_path, kind):
    log = sc.ScanLog(tmp_path)
    first = sc.leader_scan_job(fake(day=date(2026, 9, 25)), log, at(date(2026, 9, 25)))
    assert first.discovery["watchlist_build"]["status"] == "READY"
    published = json.loads((tmp_path / "watchlist.json").read_text())

    class Broken(Fake):
        def gainers(self, *a, **k):
            raise BarDataError("fixture ranking unavailable")
    source = fake()
    if kind == "benchmark":
        source.frames.pop(("SPY", "D"))
    else:
        source = Broken(source.frames, lists=source.lists)
    rec = sc.leader_scan_job(source, log, at())
    status = rec.discovery["watchlist_build"]
    assert status["status"] == "FAILED" and status["retained"] and status["generation"] == 1
    assert status["published_at"] == at(date(2026, 9, 25)).isoformat()
    assert status["retained_age_hours"] == 168.0 and not status["stale"]
    assert json.loads((tmp_path / "watchlist.json").read_text()) == published
    assert log.watchlist_status(at(date(2026, 10, 3)))["stale"]


def test_failure_with_no_previous_list_says_none_exists(tmp_path):
    log = sc.ScanLog(tmp_path)
    source = fake()
    source.frames.pop(("SPY", "D"))
    rec = sc.leader_scan_job(source, log, at())
    status = rec.discovery["watchlist_build"]
    assert status["status"] == "FAILED" and status["exists"] is False and status["generation"] is None
    assert not (tmp_path / "watchlist.json").exists()


def test_crash_after_the_commit_point_is_repaired_on_read(tmp_path, monkeypatch):
    log = sc.ScanLog(tmp_path)
    sc.leader_scan_job(fake(day=date(2026, 9, 25)), log, at(date(2026, 9, 25)))
    old = (tmp_path / "watchlist.json").read_text()

    def crash(*a, **k):
        raise OSError("fixture crash after commit")
    monkeypatch.setattr(log, "write_watchlist", crash)
    with pytest.raises(OSError):
        sc.leader_scan_job(fake(leaders=("LEAD", "ALSO")), log, at())
    assert (tmp_path / "watchlist.json").read_text() == old            # derived file not yet rewritten
    restarted = sc.ScanLog(tmp_path)
    status = restarted.watchlist_status(at())
    assert status["generation"] == 2 and status["status"] == "READY" and status["published_at"] == at().isoformat()
    assert "ALSO" in restarted.published_watchlist()
    assert "ALSO" in json.loads((tmp_path / "watchlist.json").read_text())   # repaired


def test_crash_before_the_commit_point_leaves_the_previous_generation(tmp_path, monkeypatch):
    log = sc.ScanLog(tmp_path)
    sc.leader_scan_job(fake(day=date(2026, 9, 25)), log, at(date(2026, 9, 25)))
    real = log.write_json

    def crash(name, value):
        if name == sc.ScanLog.BUILD:
            raise OSError("fixture crash before commit")
        return real(name, value)
    monkeypatch.setattr(log, "write_json", crash)
    with pytest.raises(OSError):
        sc.leader_scan_job(fake(leaders=("LEAD", "ALSO")), log, at())
    status = sc.ScanLog(tmp_path).watchlist_status(at())
    assert status["generation"] == 1 and "ALSO" not in sc.ScanLog(tmp_path).published_watchlist()


def test_tampered_bundle_is_invalid_not_trusted(tmp_path):
    log = sc.ScanLog(tmp_path)
    sc.leader_scan_job(fake(), log, at())
    bundle = json.loads((tmp_path / "watchlist-build.json").read_text())
    bundle["list"]["EVIL"] = ["leader scan"]
    (tmp_path / "watchlist-build.json").write_text(json.dumps(bundle))
    assert log.watchlist_status(at())["status"] == "INVALID"
    with pytest.raises(ValueError):
        log.published_watchlist()


def test_restart_and_recovery_after_failure(tmp_path):
    log = sc.ScanLog(tmp_path)
    source = fake()
    source.frames.pop(("SPY", "D"))
    sc.leader_scan_job(source, log, at(date(2026, 9, 25)))
    log = sc.ScanLog(tmp_path)                                           # restart
    assert log.watchlist_status(at())["status"] == "FAILED"
    rec = sc.leader_scan_job(fake(), log, at())
    status = rec.discovery["watchlist_build"]
    assert status["status"] == "READY" and status["generation"] == 1 and status["last_complete_at"] == at().isoformat()


def test_legacy_list_is_generation_zero_until_the_first_bundle(tmp_path):
    log = sc.ScanLog(tmp_path)
    log.write_watchlist({"OLD": ["leader scan"]})
    log.build_status(at(date(2026, 9, 25)), "READY", {}, success=True)
    status = log.watchlist_status(at())
    assert status["generation"] == 0 and status["published_at"] == at(date(2026, 9, 25)).isoformat()
    assert log.published_watchlist() == {"OLD": ["leader scan"]}


# ---------------------------------------------------------------- 10.9 ----

def test_user_picks_and_core_exemptions_across_partial_and_failed_builds(tmp_path):
    log = sc.ScanLog(tmp_path)
    picks(log, add=["MINE"], remove=["ALSO", "SPY"])
    rec = sc.leader_scan_job(fake(leaders=("LEAD", "ALSO"), missing=("MISSING",)), log, at())
    wl = json.loads((tmp_path / "watchlist.json").read_text())
    assert rec.discovery["watchlist_build"]["status"] == "PARTIAL"
    assert wl["MINE"] == ["Taz"] and "ALSO" not in wl and wl["SPY"] == ["always"] and wl["LEAD"] == ["leader scan"]
    leaders = [s for s, tags in wl.items() if "leader scan" in tags]
    assert leaders == ["LEAD"]                                   # no padding to a target count
    # A failed build keeps the published list; removals still apply at the next scan, core stays.
    source = fake()
    source.frames.pop(("SPY", "D"))
    sc.leader_scan_job(source, log, at(minute=50))
    picks(log, remove=["LEAD", "QQQ"])
    log = sc.ScanLog(tmp_path)                                   # restart
    from desk.watchlist import overlay_picks
    current = overlay_picks(log.published_watchlist(), log.picks())
    assert "LEAD" not in current and "QQQ" in current and current["MINE"] == ["Taz"]


def test_publication_records_attempt_and_stage_counts(tmp_path):
    log = sc.ScanLog(tmp_path)
    rec = sc.leader_scan_job(fake(leaders=("LEAD",), missing=("MISSING",)), log, at())
    attempt = json.loads((tmp_path / "watchlist-status.json").read_text())
    assert attempt["attempted_at"] == at().isoformat() and attempt["published_generation"] == 1
    assert attempt["stage_counts"] == {"selected:leader": 1, "source_failure:bars": 1}
    assert counts_reconcile(rec)


def test_spy_must_reach_the_latest_completed_session_at_the_real_clock(tmp_path):
    log = sc.ScanLog(tmp_path)
    rec = sc.leader_scan_job(fake(day=date(2026, 10, 1)), log, at())          # Thursday's bars on Friday
    assert rec.error == "no valid completed SPY bars"
    weekend = sc.leader_scan_job(fake(), sc.ScanLog(tmp_path / "w"), at(date(2026, 10, 4), 9, 0))
    assert weekend.discovery["build"]["latest_completed_session"] == "2026-10-02"
    assert weekend.discovery["watchlist_build"]["status"] == "READY"


# ------------------------------------------------- scanner timing paths ----

@pytest.fixture
def template(monkeypatch):
    monkeypatch.setattr(sc, "trend_template", lambda f, spy: GateResult({"fixture template": True}))


def test_close_scan_volume_setups_wait_for_the_next_session_and_prepare_once(tmp_path, template):
    day0, entry = date(2026, 9, 29), date(2026, 9, 30)
    fr = frames(day0)
    fr[("LEAD", "D")] = daily(CUP, end=day0)
    sim = AlpacaSim(symbols=["LEAD", "SPY", "QQQ", "IWM"], daily=dry_daily(day0, days=12))
    clock = Clock(at(day0, 16, 12))
    source = with_volume(Fake(fr), tmp_path / "volume.sqlite", sim, clock)
    log = sc.ScanLog(tmp_path / "scan")
    close = sc.run(source, {"LEAD": ["leader scan"]}, log, clock.at)
    assert close.discovery["volume_pending"] == ["LEAD"] and sim.calls() == 0
    assert "DAILY_NOT_COMPLETED_AT_RECEIPT" in close.skipped["LEAD/2_minervini_vcp"]
    assert not {a["setup_id"] for a in close.armed} & {"2_minervini_vcp", "3_oneil_cup_with_handle"}
    clock.at = at(entry, 9, 47)
    morning = sc.run(with_volume(Fake(fr), tmp_path / "volume.sqlite", sim, clock), {"LEAD": ["leader scan"]},
                     log, clock.at)
    prep = morning.discovery["volume_preparation"]
    assert {a["setup_id"] for a in prep["armed"]} == {"2_minervini_vcp", "3_oneil_cup_with_handle"}
    assert all(a["volume_evidence"] for a in prep["armed"]) and sim.calls() == 2
    clock.at = at(entry, 10, 2)
    sc.run(with_volume(Fake(fr), tmp_path / "volume.sqlite", sim, clock), {"LEAD": ["leader scan"]}, log, clock.at)
    assert sim.calls() == 2                                        # prepared once; observation reads the cache
    assert {s.setup_id for s in log.load_armed(entry)[1]} >= {"2_minervini_vcp", "3_oneil_cup_with_handle"}


def test_mover_ep_unavailable_at_1000_retries_at_1015(tmp_path):
    day = date(2026, 9, 29)
    d = daily(np.r_[EP, EP[-1]], spread=0.02)
    m = m15([(57.5, 58.5, 57, 58.2), (58.2, 58.4, 57.8, 58.1), (58.1, 58.3, 57.9, 58.0)])
    fr = {("GAP", "D"): d, ("GAP", "M15"): m}
    lists = {"PRE_MARKET": [{"symbol": "GAP", "price": "57.5", "change_ratio": "0.13"}]}
    sim = AlpacaSim(symbols=["GAP"], daily=lambda s, x: 2_000_000,
                    rth=lambda s, t: 600_000 if t.minute == 30 else 400_000)
    log = sc.ScanLog(tmp_path / "scan")
    log.save_armed(day, sc.ScanRecord("close", "", None, market="full"))
    clock = Clock(at(day, 10, 2))
    first = sc.run(with_volume(Fake(fr, lists=lists), tmp_path / "v.sqlite", sim, clock), [], log, clock.at)
    assert first.discovery["ep_candidates"]["errors"]["GAP"] == "volume unavailable: END_NOT_15_MINUTES_OLD"
    assert log.pending(day, "ep") == {"GAP"} and not first.armed
    clock.at = at(day, 10, 17)
    second = sc.run(with_volume(Fake(fr, lists=lists), tmp_path / "v.sqlite", sim, clock), [], log, clock.at)
    ep = [a for a in second.armed if a["setup_id"] == "5_qullamaggie_episodic_pivot"]
    assert ep and ep[0]["volume_evidence"]["result"]["met"] is True
    assert "GAP" in log.prepared_users(day, "ep")
