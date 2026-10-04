"""G5a checkpoint 2, sections 10.2-10.5 and 10.10: every volume consumer on Alpaca SIP.

Webull charts and Alpaca replies are synthetic fixtures (labelled). The consumer
matrix in docs/checkpoints/G5a-cp2-volume-consumers.md maps each row to a test here.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
import json

import numpy as np
import pandas as pd
import pytest

from desk import scanner as sc
from desk.alpaca_source import (CONSUMERS, CONFIG_UNAVAILABLE, NOT_IMPLEMENTED, VOLUME_SETUPS, AlpacaVolumeProvider,
                                DecisionVolumeSource, configured_volume, complete_through)
from desk.alpaca_volume import BarRequest
from desk.bars import BarDataError
from desk.calendar import ET, sessions
from desk.data_basis import (AlpacaDecisionVolume, VolumeUnavailable, attached_decision, decision_window,
                             dependency_terms)
from desk.indicators import daily_features
from desk.playbook.cards import CARDS
from desk.playbook.filters import GateResult, MarketSize
from desk.signal_state import candidate_id
from tests.alpaca_support import AlpacaSim, Clock, asset_row, dry_daily, provider, with_volume
from tests.test_scanner import Fake, daily, frames, m15, UP
from tests.test_triggers import CUP, DARVAS, LUK, QULL, ctx, run
from tests.charts import features

DAY0 = date(2026, 9, 29)


@pytest.fixture
def template(monkeypatch):
    monkeypatch.setattr(sc, "trend_template", lambda f, spy: GateResult({"fixture template": True}))


def at(day=date(2026, 9, 30), hour=9, minute=45):
    return datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(hour=hour, minute=minute)


def prepared(tmp_path, *, chart=CUP, end=DAY0, now=None, **sim):
    """Webull fixture frames through ``end`` plus one Alpaca provider; close-scan preparation."""
    fr = frames(end)
    fr[("LEAD", "D")] = daily(chart, end=end)
    fr[("BOX", "D")] = daily(DARVAS, end=end)                 # price-only setups (Darvas, Holy Grail)
    sim.setdefault("daily", dry_daily(end, days=12))
    s = AlpacaSim(symbols=["LEAD", "BOX", "SPY", "QQQ", "IWM"], **sim)
    clock = Clock(now or at())
    src = with_volume(Fake(fr), tmp_path / "volume.sqlite", s, clock)
    rec, armed = sc.close_scan(src, ["LEAD", "BOX"], clock.at, preparing=True)
    return fr, s, rec, {(x.symbol, x.setup_id): x for x in armed}


def attached(tmp_path, *, n=60, end=DAY0, now=None, through=None, **sim):
    """One Webull daily frame (session index, metadata) with Alpaca decision volume attached."""
    fr = {("LEAD", "D"): daily(np.full(n, 100.0), end=end)}
    s = AlpacaSim(symbols=["LEAD"], **sim)
    clock = Clock(now or at(date(2026, 9, 30), 9, 45))
    src = with_volume(Fake(fr), tmp_path / "volume.sqlite", s, clock)
    bars = sc.fetch(src, ["LEAD"], "D", 1000, {})
    src.decision_volume.attach_daily(bars, clock.at, through=through or end)
    return bars["LEAD"], s


# ---------------------------------------------------------------- 10.2 ----

def test_alpaca_volume_drives_vcp_and_cup_while_webull_frames_stay_unchanged(tmp_path, template):
    fr, sim, rec, armed = prepared(tmp_path)
    assert set(armed) >= {("LEAD", "2_minervini_vcp"), ("LEAD", "3_oneil_cup_with_handle")}
    vcp, cup = armed["LEAD", "2_minervini_vcp"], armed["LEAD", "3_oneil_cup_with_handle"]
    assert vcp.volume_evidence["result"] == {"sum10": "10000000", "sum50": "88000000", "met": True}
    assert cup.volume_evidence["result"]["handle_bars"] == 12
    # The same Webull charts with flat Alpaca volume: neither volume setup qualifies.
    _, _, _, flat = prepared(tmp_path / "flat", daily=lambda s, d: 2_000_000)
    assert not {k[1] for k in flat} & VOLUME_SETUPS
    # Price-only setups arm identically either way, with no volume dependency.
    price_only = {k for k in armed if k[1] not in VOLUME_SETUPS}
    assert price_only == {k for k in flat if k[1] not in VOLUME_SETUPS} == {
        ("BOX", "4_darvas_box"), ("BOX", "8_raschke_holy_grail")}
    assert all(armed[k].volume_evidence is None and armed[k] == flat[k] for k in price_only)
    # Webull: the volume column is the fixture's own (no dry-up), untouched by Alpaca.
    assert (fr[("LEAD", "D")]["volume"] == 2e6).all()


def test_attached_volume_changes_only_rel_volume_never_webull_columns(tmp_path):
    frame, _ = attached(tmp_path, daily=lambda s, d: 3_000_000 if d == DAY0 else 1_000_000)
    plain = frame.copy()
    plain.attrs = {k: v for k, v in frame.attrs.items() if k != "decision_volume"}
    with_alpaca, webull = daily_features(frame), daily_features(plain)
    assert with_alpaca.drop(columns="rel_volume").equals(webull.drop(columns="rel_volume"))
    assert with_alpaca["volume"].equals(frame["volume"]) and frame.attrs["volume_basis"] == plain.attrs["volume_basis"]
    # Same denominator semantics: the bar over the 50-session mean that includes it.
    assert with_alpaca["rel_volume"].iloc[-1] == pytest.approx(3e6 / ((49 * 1e6 + 3e6) / 50))
    assert webull["rel_volume"].iloc[-1] == pytest.approx(1.0)
    assert with_alpaca["rel_volume"].iloc[:-11].isna().all()          # no full, proven window there


def test_unchanged_webull_path_without_configuration(tmp_path, template):
    fr = frames(DAY0)
    fr[("LEAD", "D")] = daily(CUP, end=DAY0)
    fr[("LEAD", "D")]["volume"] = np.r_[np.full(len(CUP) - 12, 2e6), np.full(12, 1e6)]
    source = Fake(fr)
    assert configured_volume(source, {}) is source
    rec, armed = sc.close_scan(source, ["LEAD"], at(), preparing=True)
    vcp = next(s for s in armed if s.setup_id == "2_minervini_vcp")
    assert vcp.volume_evidence is None and "alpaca_volume" not in rec.discovery
    payload = sc._sig(vcp)
    assert "volume_evidence" in payload and payload["volume_evidence"] is None
    # Earlier signal identities do not move: a None field is not part of the hash.
    from dataclasses import asdict
    legacy = {k: v for k, v in asdict(vcp).items() if k != "volume_evidence"}
    from desk.signal_state import _hash, signal_payload
    terms = {**legacy, "as_of": vcp.as_of.isoformat()}
    terms.pop("saw")
    assert candidate_id(vcp, date(2026, 9, 30)) == _hash(["2026-09-30", vcp.setup_version, terms])


# ---------------------------------------------------------------- 10.3 ----

@pytest.mark.parametrize("env", [{"DESK_ALPACA_VOLUME_CACHE": "{tmp}/v.sqlite", "DESK_ALPACA_VOLUME_BUDGET": "6"},
                                 {"DESK_ALPACA_VOLUME_CACHE": "{tmp}/v.sqlite", "DESK_ALPACA_VOLUME_BUDGET": "0",
                                  "APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s"},
                                 {"DESK_ALPACA_VOLUME_CACHE": "{tmp}/v.sqlite", "DESK_ALPACA_VOLUME_BUDGET": "six",
                                  "APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s"}])
def test_missing_keys_or_bad_config_isolate_volume_and_keep_price_checks(tmp_path, template, env):
    env = {k: v.replace("{tmp}", str(tmp_path)) for k, v in env.items()}
    fr = frames(DAY0)
    fr[("LEAD", "D")] = daily(CUP, end=DAY0)
    fr[("LEAD", "D")]["volume"] = np.r_[np.full(len(CUP) - 12, 2e6), np.full(12, 1e6)]  # Webull dry-up
    fr[("BOX", "D")] = daily(DARVAS, end=DAY0)
    source = configured_volume(Fake(fr), env, transport=lambda *a: pytest.fail("no request expected"))
    assert source.decision_volume.issue == CONFIG_UNAVAILABLE
    rec, armed = sc.close_scan(source, ["LEAD", "BOX"], at(), preparing=True)
    # No Webull, IEX or native fallback: the volume setups report unavailable ...
    assert not {s.setup_id for s in armed} & VOLUME_SETUPS
    assert rec.skipped["LEAD/2_minervini_vcp"] == "volume unavailable: " + CONFIG_UNAVAILABLE
    # ... while price-only setups and the market filter still run.
    assert rec.market and {s.setup_id for s in armed} == {"4_darvas_box", "8_raschke_holy_grail"}


def test_decision_volume_refuses_iex_raw_and_mixed_sources(tmp_path):
    frame, _ = attached(tmp_path)
    decision = attached_decision(frame)
    obs = decision.observation
    for bad in (dict(feed="iex"), dict(adjustment="raw"), dict(channel="rth-m15")):
        with pytest.raises(ValueError):
            AlpacaDecisionVolume.model_validate({**decision.model_dump(), "observation": {**obs.model_dump(), **bad}})
    with pytest.raises(ValueError):
        BarRequest(symbols=("LEAD",), timeframe="1Day", adjustment="split", feed="iex",
                   start=datetime(2026, 9, 1, tzinfo=ET), end=datetime(2026, 9, 2, tzinfo=ET))
    # A decision attached to another Webull instrument is unusable, never re-pointed.
    frame.attrs["security_metadata"] = {**frame.attrs["security_metadata"], "instrument_id": "fixture:other"}
    with pytest.raises(VolumeUnavailable, match="WEBULL_IDENTITY_MISMATCH"):
        decision_window(frame, 50)
    frame.attrs["decision_volume"] = {"not": "a decision"}
    with pytest.raises(VolumeUnavailable, match="MALFORMED_DECISION_VOLUME"):
        decision_window(frame, 50)


# ---------------------------------------------------------------- 10.4 ----

def test_full_short_missing_and_unfinished_daily_windows(tmp_path):
    frame, _ = attached(tmp_path)
    window = decision_window(frame, 50)
    assert window.source == "alpaca" and len(window.values) == 50 and window.sessions[-1] == DAY0
    short, _ = attached(tmp_path / "short", n=40)
    with pytest.raises(VolumeUnavailable, match="SHORT_WINDOW"):
        decision_window(short, 50)
    gap = sessions(date(2026, 9, 1), DAY0)[-20]
    missing, _ = attached(tmp_path / "missing", daily=lambda s, d: None if d == gap else 1_000_000)
    with pytest.raises(VolumeUnavailable, match="MISSING_DAILY_SESSIONS"):
        decision_window(missing, 50)
    # At 16:10 on the session itself the native daily is not final: no request at all.
    late, sim = attached(tmp_path / "late", now=at(DAY0, 16, 10))
    assert sim.calls() == 0
    with pytest.raises(VolumeUnavailable, match="DAILY_NOT_COMPLETED_AT_RECEIPT"):
        decision_window(late, 50)
    # A Webull frame with a hole is misaligned against the exchange calendar.
    holed = frame.drop(index=frame.index[-30])
    with pytest.raises(VolumeUnavailable, match="MISALIGNED_SESSIONS"):
        decision_window(holed, 50)


def test_holiday_and_early_close_follow_the_exchange_calendar(tmp_path):
    end = date(2026, 12, 1)                     # window spans Thanksgiving (closed) and 27 Nov (early close)
    frame, _ = attached(tmp_path, end=end, now=at(date(2026, 12, 2), 9, 45))
    window = decision_window(frame, 50)
    assert date(2026, 11, 26) not in window.sessions and date(2026, 11, 27) in window.sessions


def ep_source(tmp_path, *, entry=date(2026, 9, 29), rth=None, daily=None, now=None, budget=6, **sim):
    prior = sessions(entry - timedelta(days=120), entry)[-61:-1]
    fr = {("NEW", "D"): daily_frame(prior[-1]),
          ("NEW", "M15"): m15([(57.5, 58.5, 57, 58), (58, 59, 57.5, 58.8), (58.8, 59.5, 58.5, 59.2)], day=entry)}
    s = AlpacaSim(symbols=["NEW"], daily=daily or (lambda sym, d: 2_000_000),
                  rth=rth or (lambda sym, t: 600_000 if t.minute == 30 else 400_000), **sim)
    clock = Clock(now or at(entry, 10, 15))
    return with_volume(Fake(fr), tmp_path / "volume.sqlite", s, clock, budget=budget), s, clock


def daily_frame(end):
    return daily(np.full(60, 50.0), spread=.03, end=end)


def test_ep_uses_the_approved_card_component_with_an_exact_boundary(tmp_path):
    src, sim, clock = ep_source(tmp_path)
    skipped = {}
    out = sc.episodic_pivots(src, MarketSize.FULL, clock.at, skipped, candidates=["NEW"])
    assert len(out) == 1, skipped
    evidence = out[0].volume_evidence
    assert evidence["consumer"] == "5_qullamaggie_episodic_pivot:early_volume"
    assert evidence["result"]["ratio"] == "0.5" and evidence["result"]["met"] is True   # exactly at 0.5
    assert evidence["rule_version"] == CARDS["5_qullamaggie_episodic_pivot"].fingerprint()
    assert [i["channel"] for i in evidence["inputs"]] == ["native-daily", "rth-m15"]
    assert out[0].saw["early volume"] == "50% of a normal day"
    # One share fewer in the first 30 minutes misses the threshold; nothing else decides it.
    below, _, clock2 = ep_source(tmp_path / "below", rth=lambda s, t: 600_000 if t.minute == 30 else 399_999)
    assert sc.episodic_pivots(below, MarketSize.FULL, clock2.at, {}, candidates=["NEW"]) == []


def test_ep_excludes_the_entry_day_and_rejects_missing_intervals(tmp_path):
    entry = date(2026, 9, 29)
    src, sim, clock = ep_source(tmp_path, daily=lambda s, d: 10**12 if d == entry else 2_000_000)
    out = sc.episodic_pivots(src, MarketSize.FULL, clock.at, {}, candidates=["NEW"])
    assert out and out[0].volume_evidence["result"]["prior50_total"] == "100000000"
    daily_url = next(u for u in sim.sent if "timeframe=1Day" in u)
    assert "end=2026-09-29T03%3A59%3A59Z" in daily_url
    gap, _, clock = ep_source(tmp_path / "gap", rth=lambda s, t: None if t.minute == 45 else 600_000)
    skipped = {}
    assert sc.episodic_pivots(gap, MarketSize.FULL, clock.at, skipped, candidates=["NEW"]) == []
    assert skipped["NEW"] == "volume unavailable: MISSING_RTH_INTERVALS"


def test_ep_before_1015_is_unavailable_without_a_request_and_retries(tmp_path):
    src, sim, clock = ep_source(tmp_path, now=at(date(2026, 9, 29), 10, 0))
    skipped = {}
    assert sc.episodic_pivots(src, MarketSize.FULL, clock.at, skipped, candidates=["NEW"]) == []
    assert skipped["NEW"] == "volume unavailable: END_NOT_15_MINUTES_OLD"
    assert sim.calls("bars") == 1                 # the finished daily window only; no RTH request


@pytest.mark.parametrize("entry,utc", [(date(2026, 10, 30), "13:30"), (date(2026, 11, 2), "14:30")])
def test_ep_rth_intervals_follow_daylight_saving(tmp_path, entry, utc):
    src, sim, clock = ep_source(tmp_path, entry=entry)
    out = sc.episodic_pivots(src, MarketSize.FULL, clock.at, {}, candidates=["NEW"])
    assert out, "EP should qualify"
    rth = out[0].volume_evidence["inputs"][1]["values"]
    assert [k for k, _ in rth] == [f"{entry}T{utc}:00Z",
                                   f"{entry}T{utc[:2]}:45:00Z"]


def test_holiday_shortened_prior_window_for_ep(tmp_path):
    entry = date(2026, 11, 30)
    src, sim, clock = ep_source(tmp_path, entry=entry)
    out = sc.episodic_pivots(src, MarketSize.FULL, clock.at, {}, candidates=["NEW"])
    days = [k for k, _ in out[0].volume_evidence["inputs"][0]["values"]]
    assert "2026-11-26" not in days and "2026-11-27" in days and len(days) == 50


# ---------------------------------------------------------------- 10.5 ----

def test_every_volume_consumer_and_unimplemented_card_condition_is_registered():
    params = {f"{k}:{p}" for k, c in CARDS.items() for p in c.params if "volume" in p}
    registered = {key for key in CONSUMERS} | set(NOT_IMPLEMENTED)
    implemented = {"2_minervini_vcp:dry_volume", "5_qullamaggie_episodic_pivot:early_volume"}
    assert params - implemented <= set(NOT_IMPLEMENTED)
    assert implemented <= registered and "3_oneil_cup_with_handle:handle_volume" in CONSUMERS
    assert VOLUME_SETUPS == {"2_minervini_vcp", "3_oneil_cup_with_handle", "5_qullamaggie_episodic_pivot"}
    assert all(c["sessions"] == 50 for c in CONSUMERS.values())


def test_luk_anchored_vwap_never_reads_alpaca_weights():
    plain = features(LUK)
    tagged = plain.copy()
    tagged.attrs = {**plain.attrs, "decision_volume": {"not": "read by Luk"}}   # would raise if consulted
    a, b = run("7_luk_pullback_reclaim", plain, ctx()), run("7_luk_pullback_reclaim", tagged, ctx())
    assert a == b and a is not None and a.volume_evidence is None


def test_discovery_liquidity_uses_final_alpaca_sessions_with_an_exact_boundary(tmp_path):
    day = date(2026, 10, 2)
    lists = {"MONTH_3": [{"symbol": "LEAD", "price": 150}]}

    def build(path, volume):
        fr = {**frames(day), ("LEAD", "D"): daily(QULL, spread=0.02, end=day)}
        s = AlpacaSim(symbols=["LEAD", "SPY"], daily=lambda sym, d: volume)
        src = with_volume(Fake(fr, lists=lists), path / "volume.sqlite", s, Clock(at(day, 16, 40)))
        log = sc.ScanLog(path / "scan")
        return sc.leader_scan_job(src, log, at(day, 16, 40)), log, s

    rec, log, sim = build(tmp_path / "ok", 1_000_000)          # mean exactly 1,000,000: liquid
    build_report = json.loads((tmp_path / "ok" / "scan" / "watchlist-build.json").read_text())["build"]
    assert [l["symbol"] for l in rec.leaders] == ["LEAD"] and rec.leaders[0]["liquidity_digest"]
    assert build_report["volume_through"] == "2026-10-01"          # Friday's native daily not final at 16:40
    assert build_report["liquidity_evidence"]["LEAD"]["inputs"][0]["values"][-1][0] == "2026-10-01"
    assert sim.calls() == 2
    thin, _, _ = build(tmp_path / "thin", 999_999)
    assert thin.discovery["build"]["status"] == "EMPTY" and thin.skipped["LEAD"] == "under 1M shares a day"


def test_discovery_liquidity_unavailable_is_a_source_failure_not_a_fallback(tmp_path):
    day = date(2026, 10, 2)
    fr = {**frames(day), ("LEAD", "D"): daily(QULL, spread=0.02, end=day)}
    src = DecisionVolumeSource(Fake(fr, lists={"MONTH_3": [{"symbol": "LEAD", "price": 150}]}),
                               AlpacaVolumeProvider.unavailable(CONFIG_UNAVAILABLE))
    log = sc.ScanLog(tmp_path)
    log.write_watchlist({"OLD": ["leader scan"]})
    rec = sc.leader_scan_job(src, log, at(day, 16, 40))
    build = json.loads((tmp_path / "watchlist-status.json").read_text())
    assert rec.discovery["watchlist_build"]["status"] == "INCOMPLETE" and build["stage_counts"] == {
        "source_failure:liquidity": 1}
    assert json.loads((tmp_path / "watchlist.json").read_text()) == {"OLD": ["leader scan"]}


# --------------------------------------------------------------- 10.10 ----

def test_pagination_completes_and_exhausted_budget_stays_incomplete(tmp_path, template):
    _, sim, rec, armed = prepared(tmp_path, page_rows=20)
    assert sim.calls("bars") == 5 and ("LEAD", "2_minervini_vcp") in armed   # 2 x 50 rows, 20 per page
    # A two-request budget cannot finish the pages: incomplete, never a truncated success.
    fr = frames(DAY0)
    fr[("LEAD", "D")] = daily(CUP, end=DAY0)
    s = AlpacaSim(symbols=["LEAD"], daily=dry_daily(DAY0, days=12), page_rows=20)
    src = with_volume(Fake(fr), tmp_path / "tight.sqlite", s, Clock(at()), budget=2)
    rec3, armed3 = sc.close_scan(src, ["LEAD"], at(), preparing=True)
    assert not {x.setup_id for x in armed3} & VOLUME_SETUPS
    assert rec3.skipped["LEAD/2_minervini_vcp"] == "volume unavailable: INCOMPLETE_PAGINATION"
    assert s.calls() == 2


def test_cache_reuse_makes_zero_calls_and_a_stop_blocks_later_requests(tmp_path, template):
    fr, sim, rec, armed = prepared(tmp_path)
    run = provider(tmp_path / "volume.sqlite", sim, Clock(at(minute=50)))
    sent = sim.calls()
    assert run.gate(armed["LEAD", "2_minervini_vcp"].volume_evidence, at(minute=50), refresh=False) == ("OK", None)
    assert sim.calls() == sent
    stopped = provider(tmp_path / "volume.sqlite", AlpacaSim(symbols=["LEAD"], status=[429]), Clock(at(minute=55)))
    meta = {"LEAD": Fake({}).security_metadata(["LEAD"])[0]}
    assert stopped.gate(armed["LEAD", "2_minervini_vcp"].volume_evidence, at(minute=55), refresh=True, metadata=meta)[0] \
        == "UNAVAILABLE"
    assert stopped.client.stopped == "RATE_LIMITED"
    assert stopped.client.fetch(BarRequest(symbols=("LEAD",), timeframe="1Day", adjustment="split",
                                           start=datetime(2026, 9, 1, tzinfo=ET),
                                           end=datetime(2026, 9, 2, tzinfo=ET))).error == "RUN_STOPPED_AFTER_RATE_LIMITED"


def test_receipts_after_the_decision_clock_cannot_qualify_it(tmp_path, template):
    fr = frames(DAY0)
    fr[("LEAD", "D")] = daily(CUP, end=DAY0)
    s = AlpacaSim(symbols=["LEAD"], daily=dry_daily(DAY0, days=12))
    receipt = Clock(at(minute=50))                                   # provider receipt later than the decision
    src = with_volume(Fake(fr), tmp_path / "volume.sqlite", s, receipt)
    rec, armed = sc.close_scan(src, ["LEAD"], at(), decision_clock=lambda: at(minute=46), preparing=True)
    assert not {x.setup_id for x in armed} & VOLUME_SETUPS
    assert rec.skipped["LEAD/2_minervini_vcp"] == "volume unavailable: RECEIPT_AFTER_DECISION_CLOCK"


def test_complete_through_is_the_latest_final_native_daily_session():
    assert complete_through(at(date(2026, 10, 2), 16, 40)) == date(2026, 10, 1)
    assert complete_through(at(date(2026, 10, 4), 9, 0)) == date(2026, 10, 2)    # Sunday preview
    assert complete_through(at(date(2026, 10, 5), 0, 0)) == date(2026, 10, 2)
    assert complete_through(at(date(2026, 10, 6), 0, 0)) == date(2026, 10, 5)


def test_dependency_terms_exclude_receipts(tmp_path, template):
    _, _, _, armed = prepared(tmp_path)
    evidence = armed["LEAD", "2_minervini_vcp"].volume_evidence
    terms = dependency_terms(evidence)
    assert "audit" not in terms and terms["dependency_digest"] == evidence["dependency_digest"]
    text = json.dumps(terms)
    assert "received_at" not in text and "snapshot_digest" not in text and "sha256" not in text


@pytest.mark.parametrize("recent,met", [(2_800_000, False), (2_799_999, True)])
def test_vcp_dry_volume_boundary_is_exact_and_strict(tmp_path, template, recent, met):
    # 10-day mean < 0.7 x 50-day mean  <=>  s10*50 < 0.7*s50*10. With 40 sessions at 4.3M
    # and 10 at 2.8M the two sides are equal: not dry. One share less in each: dry.
    last10 = set(sessions(date(2026, 8, 1), DAY0)[-10:])
    _, _, _, armed = prepared(tmp_path, daily=lambda s, d: recent if d in last10 else 4_300_000)
    vcp = armed.get(("LEAD", "2_minervini_vcp"))
    assert (vcp is not None) is met
    if met:
        assert vcp.volume_evidence["result"] == {"sum10": "27999990", "sum50": "199999990", "met": True}
