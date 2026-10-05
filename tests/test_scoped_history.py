"""G5a checkpoint 3: scoped discovery history (Taz's prompt, section 9, cases 1-11).

Every Webull and Alpaca reply is a labelled synthetic fixture (``tests/webull_sim.py``,
``tests/alpaca_support.py``) served through the real ``WebullData`` client and parser,
``VendorBasisSource``, ``VendorHistoryStore``, scanner and ``ScanLog``. No provider is
called. Nothing here is setup qualification or trade approval.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import io
import json
from urllib import error

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from desk import scanner as sc
from desk.action_source import configured_source
from desk.bar_contract import completed_daily
from desk.bars import BarDataError, BarRowError
from desk.calendar import ET, latest_closed_session, sessions
from desk.data_basis import PriceHistoryChanged, compatible_prices, price_basis
from desk.history_scope import (DISCOVERY_POLICY, FULL_HISTORY, HistoryScope, ScopeWindow, discovery_scope,
                                discovery_window)
from desk.indicators import daily_features, discovery_features
from desk.playbook.filters import trend_template
from desk.playbook.triggers import Context, Signal, scan
from desk.playbook.filters import MarketSize
from desk.signal_state import SignalStateError, candidate_id
from desk.vendor_basis import VendorHistoryStore, _daily_scoped
from desk.watchlist import _strength
from desk.webull import SANDBOX_HOST, WebullData
from tests.alpaca_support import AlpacaSim, Clock, with_volume
from tests.webull_sim import WebullSim, series

HOST = SANDBOX_HOST
FRIDAY = date(2026, 10, 2)
THURSDAY = date(2026, 10, 1)
BUILD = datetime(2026, 10, 2, 16, 40, tzinfo=ET)
WINDOW = sessions(date(2024, 9, 1), FRIDAY)[-260:]          # the 260 required sessions at BUILD
START = WINDOW[0]
MARGIN = sessions(date(2024, 9, 1), FRIDAY)[-265:-260]      # the 5 requested sessions before START
OLD = date(2023, 6, 5)                                      # inside 1000 rows, outside discovery
GROWTH = {"AAA": 0.0015, "BBB": 0.0020, "CCC": 0.0011, "DDD": 0.0006, "DOWN": -0.0010}


class NoScope:
    """The same Webull client with its scoped method hidden: an adapter without scope support."""

    def __init__(self, raw):
        self.raw = raw

    def __getattr__(self, name):
        if name == "bars_scoped":
            raise AttributeError(name)
        return getattr(self.raw, name)


def world(*names, end=FRIDAY, n=1100, mode="bounded", flat=False):
    sim = WebullSim(mode=mode)
    sim.add("SPY", series(end, n, start=300, growth=0.0004, wave=0.01), sub="ETF")
    for i, name in enumerate(names):
        sim.add(name, series(end, n, start=40 + 5 * i, growth=GROWTH.get(name, 0.0015), period=7 + i))
    if flat:
        sim.add("FLAT", series(end, n, start=100, growth=0, wave=0))
    sim.universe(*names, *(["FLAT"] if flat else []))
    return sim


class Desk:
    def __init__(self, tmp_path, sim, at=BUILD, *, scoped=True, volume=True, vendor="vendor.sqlite"):
        self.sim, self.clock = sim, Clock(at)
        self.raw = WebullData("fixture-key", "fixture-secret", host=HOST, transport=sim, min_interval=0,
                              clock=self.clock)
        env = {"DESK_VENDOR_BASIS_DB": str(tmp_path / vendor), "WEBULL_HOST": HOST}
        self.vendor = configured_source(self.raw if scoped else NoScope(self.raw), env, clock=self.clock)
        self.alpaca = AlpacaSim(symbols=sorted(sim.world))
        self.source = (with_volume(self.vendor, tmp_path / "alpaca.sqlite", self.alpaca, self.clock)
                       if volume else self.vendor)
        self.log = sc.ScanLog(tmp_path / "scan")

    @property
    def store(self):
        return self.vendor.store

    def build(self):
        """The persisted build: the published bundle, or the retained-list attempt record."""
        rec = sc.leader_scan_job(self.source, self.log, self.clock(), decision_clock=self.clock)
        attempt = json.loads((self.log.root / "watchlist-status.json").read_text())
        assert attempt["history_scope"] == rec.discovery["build"]["history_scope"]
        if attempt["status"] in sc.ScanLog.PUBLISHED:
            return rec, json.loads((self.log.root / "watchlist-build.json").read_text())["build"]
        return rec, {**rec.discovery["build"], "status": attempt["status"], "reasons": attempt["reasons"]}

    def scoped(self, *names, category="US_STOCK", through=None):
        return self.vendor.discovery_bars(list(names), category=category, liquidity_through=through)

    def full(self, *names, category="US_STOCK"):
        return self.vendor.bars(list(names), timespan="D", category=category)


def break_high(sim, symbol, day):
    """Open above the high on one provider row (the shape of Webull's 2023-06-05 rows)."""
    row = sim.row(symbol, day)
    row["open"] = str(round(float(row["high"]) + 3, 4))


# ------------------------------------------------------------------ 1 ----

def test_full_and_scoped_discovery_agree_on_features_scores_checks_and_leaders(tmp_path):
    names = ("AAA", "BBB", "CCC", "DDD", "DOWN")
    full_desk = Desk(tmp_path / "full", world(*names, flat=True), scoped=False)
    scoped_desk = Desk(tmp_path / "scoped", world(*names, flat=True))
    full_rec, full = full_desk.build()
    scoped_rec, scoped = scoped_desk.build()
    assert full["history_scope"]["path"] == "full-history" and "DISCOVERY_SCOPE_UNSUPPORTED" in full["history_scope"]["reason"]
    assert scoped["history_scope"]["path"] == "scoped" and scoped["history_scope"]["sessions"] == 260
    for key in ("status", "leaders", "population", "stage_counts", "outcomes"):
        assert full[key] == scoped[key], key
    assert {o["outcome"] for o in scoped["outcomes"].values()} == {"selected", "criterion"}
    assert 0 < len(scoped["leaders"]) < len(names) + 1          # some pass, some fail the template
    # The consumed numbers, name by name (ordinary float tolerance only here, never in a decision).
    spy_full = completed_daily(full_desk.full("SPY", category="US_ETF")["SPY"], BUILD)["close"]
    spy_scoped = scoped_desk.scoped("SPY", category="US_ETF")["SPY"]["close"]
    for name in (*names, "FLAT"):
        a = completed_daily(full_desk.full(name)[name], BUILD)
        b = completed_daily(scoped_desk.scoped(name)[name], BUILD)
        assert len(a) == 1000 and len(b) == 260 and a.index[-260:].equals(b.index)
        fa, fb = discovery_features(a), discovery_features(b)
        for column in ("close", "sma_50", "sma_150", "sma_200", "high_52w", "low_52w"):
            assert np.isclose(fa[column].iloc[-1], fb[column].iloc[-1], rtol=1e-12, atol=0), (name, column)
        assert np.isclose(fa["sma_200"].iloc[-22], fb["sma_200"].iloc[-22], rtol=1e-12, atol=0)
        assert _strength(a["close"]) == _strength(b["close"])                    # endpoint ratios: exact
        assert trend_template(fa, spy_full).checks == trend_template(fb, spy_scoped).checks
    # Tie control: a flat series has SMA150 == SMA200 exactly on both paths.
    flat = discovery_features(scoped_desk.scoped("FLAT")["FLAT"])
    assert flat["sma_150"].iloc[-1] == flat["sma_200"].iloc[-1]
    assert not trend_template(flat, spy_scoped).checks["150-day above the 200-day"]
    assert scoped["outcomes"]["FLAT"] == {"outcome": "criterion", "stage": "trend_template",
                                          "reason": "failed the Trend Template"}


# ------------------------------------------------------------------ 2 ----

def test_old_defect_before_the_required_start_is_kept_but_does_not_block_discovery(tmp_path):
    sim = world("AAA", "BBB")
    break_high(sim, "AAA", MARGIN[1])                     # returned by the bounded request, outside scope
    desk = Desk(tmp_path, sim)
    rec, build = desk.build()
    assert build["status"] == "READY" and {l["symbol"] for l in build["leaders"]} == {"AAA", "BBB"}
    assert build["history_scope"]["excluded_outside_scope"]["AAA"]["excluded_defect_count"] == 1
    kept = desk.store.defects(HOST, "AAA", DISCOVERY_POLICY)
    assert len(kept) == 1 and kept[0]["status"] == "EXCLUDED_OUTSIDE_SCOPE"
    assert kept[0]["field"] == "high" and pd.Timestamp(kept[0]["row_time"]).tz_convert(ET).date() == MARGIN[1]
    assert json.loads(kept[0]["row_json"])["open"] == sim.row("AAA", MARGIN[1])["open"]
    assert json.loads(kept[0]["required"]) == {"start": START.isoformat(), "end": FRIDAY.isoformat(), "sessions": 260}
    assert kept[0]["evidence_digest"] and kept[0]["instrument_id"] == "wb:AAA"
    # The same row still refuses every full-history consumer, naming the row.
    assert "AAA" not in desk.full("AAA")
    why = desk.vendor.last_errors["AAA"]
    assert why.startswith("DAILY_ROW_INVALID: high/low don't contain open and close at session " + MARGIN[1].isoformat())
    assert "field high" in why and "of 1000" in why
    full = desk.store.defects(HOST, "AAA", FULL_HISTORY)
    assert [d["status"] for d in full] == ["REJECTED_REQUIRED_DATA"]
    rec_close, _ = sc.close_scan(desk.source, ["AAA"], BUILD.replace(hour=16, minute=10))
    assert "DAILY_ROW_INVALID" in rec_close.skipped["AAA"] and MARGIN[1].isoformat() in rec_close.skipped["AAA"]


# ------------------------------------------------------------------ 3 ----

@pytest.mark.parametrize("where", ["first", "interior", "last"])
def test_required_session_defect_rejects_that_ticker_and_peers_publish_partial(tmp_path, where):
    sim = world("AAA", "BAD")
    day = {"first": START, "interior": WINDOW[130], "last": FRIDAY}[where]
    break_high(sim, "BAD", day)
    desk = Desk(tmp_path, sim)
    rec, build = desk.build()
    assert build["status"] == "PARTIAL" and [l["symbol"] for l in build["leaders"]] == ["AAA"]
    outcome = build["outcomes"]["BAD"]
    assert outcome["outcome"] == "source_failure"
    assert outcome["reason"].startswith(f"DAILY_ROW_INVALID: high/low don't contain open and close at session {day}")
    rejected = desk.store.defects(HOST, "BAD", DISCOVERY_POLICY)
    assert [d["status"] for d in rejected] == ["REJECTED_REQUIRED_DATA"]
    assert desk.store.latest(HOST, "BAD", DISCOVERY_POLICY)["status"] == "UNAVAILABLE"
    assert desk.store.latest(HOST, "BAD") is None                 # the full-history record is untouched


# ------------------------------------------------------------------ 4 ----

def _row(day, **values):
    base = {"time": pd.Timestamp(day).tz_localize(ET).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "open": "50", "high": "51", "low": "49", "close": "50", "volume": "1000"}
    return {**base, **values}


FAULTS = {
    "unparsable time": (lambda sim: sim.extra.setdefault("BAD", []).append(_row(WINDOW[5], time="not-a-time")),
                        "SCOPED_TIMESTAMP_INVALID"),
    "numeric time without unit": (lambda sim: sim.extra.setdefault("BAD", []).append(_row(WINDOW[5], time="1696000000000")),
                                  "SCOPED_TIMESTAMP_INVALID"),
    "intraday daily label": (lambda sim: sim.extra.setdefault("BAD", []).append(
        _row(WINDOW[5], time="2026-09-30T15:00:00.000+0000")), "SCOPED_TIMESTAMP_AMBIGUOUS"),
    "required duplicate": (lambda sim: sim.extra.setdefault("BAD", []).append(dict(sim.row("BAD", WINDOW[7]))),
                           "duplicate bar times"),
    "NaN": (lambda sim: sim.row("BAD", WINDOW[9]).update(close="NaN"), "missing or non-numeric"),
    "inf": (lambda sim: sim.row("BAD", WINDOW[9]).update(volume="inf"), "missing or non-numeric"),
    "negative volume": (lambda sim: sim.row("BAD", WINDOW[9]).update(volume="-1"), "negative volume"),
    "impossible high/low": (lambda sim: sim.row("BAD", WINDOW[9]).update(low="9999"), "high/low don't contain"),
    "wrong identity": (lambda sim: sim.wrong_id.update(BAD="wb:SOMEONE-ELSE"), "BAR_IDENTITY_MISMATCH"),
    "delayed": (lambda sim: sim.delay.update(BAD=15), "minutes delayed"),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_unwaivable_defects_refuse_the_ticker_and_keep_the_peer(tmp_path, fault):
    sim = world("AAA", "BAD")
    apply, expected = FAULTS[fault]
    apply(sim)
    desk = Desk(tmp_path, sim)
    out = desk.scoped("AAA", "BAD")
    assert "AAA" in out and "BAD" not in out
    assert expected in desk.vendor.last_errors["BAD"]
    if fault != "wrong identity":
        assert desk.store.latest(HOST, "BAD", DISCOVERY_POLICY)["status"] == "UNAVAILABLE"


def test_duplicate_outside_the_scope_is_a_diagnostic_not_a_rejection(tmp_path):
    sim = world("AAA")
    sim.extra["AAA"] = [dict(sim.row("AAA", MARGIN[0]))]
    desk = Desk(tmp_path, sim)
    out = desk.scoped("AAA")
    assert len(out["AAA"]) == 260 and desk.vendor.last_scope_report["AAA"]["before"] == 6
    # CP3 audit D1: the duplicate is detected and persisted, not only counted.
    assert desk.vendor.last_scope_report["AAA"]["excluded_defect_count"] == 1
    (kept,) = desk.store.defects(HOST, "AAA", DISCOVERY_POLICY)
    assert kept["status"] == "EXCLUDED_OUTSIDE_SCOPE" and kept["field"] == "time" and kept["row_index"] == 265
    assert kept["reason"] == "duplicate bar times (also row 0)"     # the appended copy comes first (newest-first reply)
    assert json.loads(kept["row_json"])["time"] == sim.row("AAA", MARGIN[0])["time"]


def test_stale_receipt_refuses_even_a_scoped_request(tmp_path):
    desk = Desk(tmp_path, world("AAA"))
    desk.raw._clock = Clock(BUILD - timedelta(seconds=61))
    assert desk.scoped("AAA") == {} and desk.vendor.last_errors["AAA"] == "BAR_RECEIPT_STALE"


# ------------------------------------------------------------------ 5 ----

def test_missing_session_or_suspension_is_refused_never_filled(tmp_path):
    sim = world("ONE", "GAP", "HEAD", "ZERO")
    sim.drop("ONE", WINDOW[100])
    sim.drop("GAP", *WINDOW[40:55])                       # a suspension inside the window
    sim.drop("HEAD", START)                               # older rows exist: a gap, not a young listing
    sim.row("ZERO", WINDOW[50]).update(volume="0")
    desk = Desk(tmp_path, sim)
    out = desk.scoped("ONE", "GAP", "HEAD", "ZERO")
    errors = desk.vendor.last_errors
    assert errors["ONE"] == f"MISSING_REQUIRED_SESSIONS: {WINDOW[100]}"
    assert errors["GAP"].startswith(f"MISSING_REQUIRED_SESSIONS: {WINDOW[40]},{WINDOW[41]}") and "(+5 more)" in errors["GAP"]
    assert errors["HEAD"] == f"MISSING_REQUIRED_SESSIONS: {START}..{START} (1 sessions) while older rows exist"
    assert set(out) == {"ZERO"} and out["ZERO"]["volume"].min() == 0 and len(out["ZERO"]) == 260
    # The full path refuses the same gaps and now names them (diagnostic only).
    assert desk.full("ONE") == {}
    assert desk.vendor.last_errors["ONE"].startswith(f"MISSING_OR_STALE_DAILY_HISTORY: missing {WINDOW[100]};")


def test_short_history_is_unverified_on_the_scoped_path_and_unchanged_on_the_full_fallback(tmp_path):
    # Coverage-origin repair (2026-10-05): no supported origin evidence exists, so a short
    # scoped reply is a source failure even after a broader full request returned the
    # same 259 rows. The under-260 criterion stays on the unchanged full-history path.
    sim = world("AAA")
    sim.add("YOUNG", series(FRIDAY, 259, start=60))
    sim.universe("AAA", "YOUNG")
    desk = Desk(tmp_path / "scoped", sim)
    assert len(desk.full("YOUNG")["YOUNG"]) == 259
    _, build = desk.build()
    assert build["status"] == "PARTIAL" and [l["symbol"] for l in build["leaders"]] == ["AAA"]
    assert build["outcomes"]["YOUNG"]["reason"].startswith("SCOPED_COVERAGE_UNVERIFIED: the reply starts")
    _, fallback = Desk(tmp_path / "full", sim, scoped=False).build()
    assert fallback["history_scope"]["path"] == "full-history"
    assert fallback["outcomes"]["YOUNG"] == {"outcome": "criterion", "stage": "history", "reason": "under a year of bars"}
    # A reply that is short only because the provider returned less: accepted evidence proves older rows.
    desk.full("AAA")
    sim.drop("AAA", *[d for d in list(sim.world["AAA"]["rows"]) if d < WINDOW[60]])
    assert desk.scoped("AAA") == {}
    assert desk.vendor.last_errors["AAA"].startswith("SCOPED_HISTORY_INCOMPLETE: accepted evidence for this "
                                                     "instrument starts")


def test_a_capped_reply_that_misses_the_start_is_truncated_not_short(tmp_path):
    desk = Desk(tmp_path, world("AAA"))
    frame = desk.scoped("AAA")["AAA"]
    scope = discovery_scope("AAA", "wb:AAA", BUILD)
    clipped = frame.iloc[10:].copy()
    clipped.attrs = {**frame.attrs, "scope_report": {**frame.attrs["scope_report"],
                     "window": ScopeWindow.for_scope(scope).model_dump(mode="json"), "rows_returned": 265, "before": 0}}
    meta = type("M", (), {"symbol": "AAA", "instrument_id": "wb:AAA", "bar_category": "US_STOCK"})()
    with pytest.raises(BarDataError, match="SCOPED_HISTORY_TRUNCATED"):
        _daily_scoped(clipped, meta, scope, BUILD, HOST)
    clipped.attrs["scope_report"]["rows_returned"] = 250
    # Not truncated; whether it is short history is decided from accepted evidence (audit F1).
    assert len(_daily_scoped(clipped, meta, scope, BUILD, HOST)) == 250


# ------------------------------------------------------------------ 6 ----

def test_exact_260_window_holidays_half_day_and_lag_252(tmp_path):
    scope = discovery_scope("AAA", "wb:AAA", BUILD)
    days = scope.required_sessions()
    assert len(days) == 260 and days[0] == START and days[-1] == FRIDAY
    assert date(2025, 11, 27) not in days and date(2025, 12, 25) not in days and date(2026, 7, 3) not in days
    assert date(2025, 11, 28) in days and date(2025, 12, 24) in days               # half-days are sessions
    sim = world("AAA")
    sim.add("EXACT", series(FRIDAY, 260, start=60))
    sim.universe("AAA", "EXACT")
    desk = Desk(tmp_path, sim)
    frame = desk.scoped("EXACT")["EXACT"]
    assert len(frame) == 260 and list(frame.index.tz_convert(ET).date) == days
    closes = {d: float(r["close"]) for d, r in sim.world["EXACT"]["rows"].items()}
    assert _strength(frame["close"])["rs12m"] == pytest.approx(
        sum(w * (closes[FRIDAY] / closes[days[-1 - n]] - 1) for w, n in ((0.4, 63), (0.2, 126), (0.2, 189), (0.2, 252))),
        rel=0, abs=0)
    assert days[-1 - 252] == days[7]                                                # lag 252 is inside the window
    _, build = desk.build()
    assert build["outcomes"]["EXACT"]["stage"] != "history"                         # exactly 260 is eligible


def test_half_day_build_and_weekend_cutoff(tmp_path):
    half = date(2026, 11, 27)                                                       # closes 13:00 ET
    desk = Desk(tmp_path / "half", world("AAA", end=half), datetime(2026, 11, 27, 13, 40, tzinfo=ET))
    _, build = desk.build()
    assert build["status"] == "READY" and build["history_scope"]["cutoff_session"] == half.isoformat()
    saturday = Desk(tmp_path / "sat", world("AAA"), datetime(2026, 10, 3, 11, 0, tzinfo=ET))
    _, build = saturday.build()
    assert build["history_scope"]["cutoff_session"] == FRIDAY.isoformat() and build["status"] == "READY"
    assert build["history_scope"]["liquidity_through"] == FRIDAY.isoformat()    # Friday's SIP is final by Saturday


def test_friday_prices_end_friday_while_final_liquidity_ends_thursday(tmp_path):
    desk = Desk(tmp_path, world("AAA"))
    _, build = desk.build()
    assert build["history_scope"]["cutoff_session"] == FRIDAY.isoformat()
    assert build["history_scope"]["liquidity_through"] == THURSDAY.isoformat()
    used = [d for d, _ in build["liquidity_evidence"]["AAA"]["inputs"][0]["values"]]
    assert used[-1] == THURSDAY.isoformat() and len(used) == 50
    assert all(START.isoformat() <= d <= FRIDAY.isoformat() for d in used)       # inside the price window
    # A liquidity end further back extends the required start; a shortened scope is refused.
    early = sessions(date(2025, 1, 1), FRIDAY)[-300]
    start, _ = discovery_window(FRIDAY, early)
    assert start == sessions(date(2024, 1, 1), early)[-50] and start < START
    with pytest.raises(ValidationError, match="cannot be shortened"):
        HistoryScope(consumer="weekly-leader-discovery", policy=DISCOVERY_POLICY, timeframe="D", symbol="AAA",
                     instrument_id="wb:AAA", cutoff_session=FRIDAY, required_start=WINDOW[1], required_end=FRIDAY)


# ------------------------------------------------------------------ 7 ----

def test_required_spy_defect_fails_the_shared_build_but_an_old_one_does_not(tmp_path):
    bad = world("AAA")
    break_high(bad, "SPY", WINDOW[200])
    _, build = Desk(tmp_path / "bad", bad).build()
    assert build["status"] == "FAILED" and "DAILY_ROW_INVALID" in build["reasons"]["SPY"]
    old = world("AAA")
    break_high(old, "SPY", MARGIN[2])
    _, build = Desk(tmp_path / "old", old).build()
    assert build["status"] == "READY" and [l["symbol"] for l in build["leaders"]] == ["AAA"]


# ------------------------------------------------------------------ 8 ----

def test_excess_old_rows_meet_the_pre_parse_boundary_and_strict_parsing_still_rejects(tmp_path):
    sim = world("AAA", "BBB", mode="excess")
    break_high(sim, "AAA", OLD)
    desk = Desk(tmp_path, sim)
    _, build = desk.build()
    assert build["status"] == "READY" and {l["symbol"] for l in build["leaders"]} == {"AAA", "BBB"}
    report = build["history_scope"]["excluded_outside_scope"]["AAA"]
    assert report["rows_returned"] == 1000 and report["before"] == 740 and report["excluded_defect_count"] == 1
    kept = desk.store.defects(HOST, "AAA", DISCOVERY_POLICY)
    assert pd.Timestamp(kept[0]["row_time"]).tz_convert(ET).date() == OLD
    asked = [b for b in sim.daily_requests() if b["count"] != 1000]
    assert asked and all(b["count"] == 265 and "start_time" in b and "end_time" in b for b in asked)
    with pytest.raises(BarRowError) as caught:
        desk.raw.bars(["AAA"], category="US_STOCK", timespan="D", count=1000)
    assert caught.value.defect["field"] == "high" and caught.value.defect["time"].startswith("2023-06-05")
    assert "AAA" not in desk.raw.bars_partial(["AAA", "BBB"], category="US_STOCK", timespan="D", count=1000)
    assert desk.raw.last_partial_errors["AAA"]["defect"]["rows"] == 1000


# ------------------------------------------------------------------ 9 ----

def test_discovery_and_full_history_evidence_never_substitute_across_restarts(tmp_path):
    sim = world("AAA")
    break_high(sim, "AAA", MARGIN[0])
    desk = Desk(tmp_path, sim)
    assert desk.full("AAA") == {}
    assert desk.store.latest(HOST, "AAA")["status"] == "UNAVAILABLE"
    assert "AAA" in desk.scoped("AAA")
    for store in (desk.store, VendorHistoryStore(desk.store.path)):              # and after a restart
        assert store.latest(HOST, "AAA")["status"] == "UNAVAILABLE"
        assert store.latest(HOST, "AAA")["detail"].startswith("DAILY_ROW_INVALID")
        assert store.latest(HOST, "AAA", DISCOVERY_POLICY)["status"] == "CONSISTENT"
        assert store.current_snapshot(HOST, "AAA") is None
        assert store.current_snapshot(HOST, "AAA", DISCOVERY_POLICY) is not None
    scoped_digest = desk.store.current_snapshot(HOST, "AAA", DISCOVERY_POLICY)["snapshot"]
    # A discovery failure stays in its own scope and keeps its last good pointer.
    break_high(sim, "AAA", WINDOW[3])
    assert desk.scoped("AAA") == {}
    assert desk.store.latest(HOST, "AAA", DISCOVERY_POLICY)["status"] == "UNAVAILABLE"
    assert desk.store.current_snapshot(HOST, "AAA", DISCOVERY_POLICY)["snapshot"] == scoped_digest
    assert desk.store.latest(HOST, "AAA")["detail"].startswith("DAILY_ROW_INVALID")
    # Heal both; full succeeds; a later discovery success never touches the full pointer.
    sim.add("AAA", series(FRIDAY, 1100, start=40, growth=GROWTH["AAA"], period=7))
    assert "AAA" in desk.full("AAA")
    full = desk.store.current_snapshot(HOST, "AAA")
    restarted = Desk(tmp_path, sim)
    assert "AAA" in restarted.scoped("AAA")
    assert restarted.store.current_snapshot(HOST, "AAA") == full
    assert restarted.store.latest(HOST, "AAA")["status"] == "CONSISTENT"
    # A new full failure is not hidden by the healthy discovery record.
    break_high(sim, "AAA", OLD)
    assert restarted.full("AAA") == {}
    assert restarted.store.latest(HOST, "AAA")["status"] == "UNAVAILABLE"
    assert restarted.store.latest(HOST, "AAA", DISCOVERY_POLICY)["status"] == "CONSISTENT"


# ----------------------------------------------------------------- 10 ----

def test_revised_overlap_stays_visible_and_old_signals_cannot_use_it(tmp_path):
    sim = world("AAA")
    desk = Desk(tmp_path, sim)
    armed = price_basis(completed_daily(desk.full("AAA")["AAA"], BUILD), BUILD).model_dump(mode="json")
    assert desk.store.latest_revision(HOST, "AAA") is None
    row = sim.row("AAA", WINDOW[200])
    row["close"] = str(round((float(row["high"]) + float(row["low"])) / 2, 4))        # a valid revised value
    frame = desk.scoped("AAA")["AAA"]
    seen = desk.store.latest(HOST, "AAA", DISCOVERY_POLICY)
    assert seen["status"] == "REVISED" and WINDOW[200].isoformat() in seen["detail"]
    assert desk.store.latest_revision(HOST, "AAA") is not None                    # visible to full consumers
    with pytest.raises(PriceHistoryChanged, match="revised"):
        compatible_prices(armed, completed_daily(desk.full("AAA")["AAA"], BUILD), BUILD, symbol="AAA")
    # Shortened discovery evidence withholds (never invalidates) a full-history signal.
    with pytest.raises(BarDataError, match="cannot revalidate") as caught:
        compatible_prices(armed, frame, BUILD, symbol="AAA")
    assert not isinstance(caught.value, PriceHistoryChanged)


def test_discovery_evidence_cannot_arm_feed_setups_or_revalidate_even_with_full_overlap(tmp_path):
    sim = world("AAA")
    sim.add("NEW", series(FRIDAY, 260, start=60))                            # exactly the complete window
    desk = Desk(tmp_path, sim)
    armed = price_basis(completed_daily(desk.full("NEW")["NEW"], BUILD), BUILD).model_dump(mode="json")
    frame = desk.scoped("NEW")["NEW"]
    assert len(frame) == 260 and price_basis(frame, BUILD).method == "webull-discovery-v1"
    with pytest.raises(BarDataError, match="cannot arm or revalidate"):
        compatible_prices(armed, frame, BUILD, symbol="NEW")                    # every original row present
    with pytest.raises(BarDataError, match="cannot feed setup evaluation"):
        daily_features(frame)
    with pytest.raises(BarDataError, match="cannot feed setup evaluation"):
        scan(discovery_features(frame), Context("NEW", MarketSize.FULL))
    signal = Signal("1_qullamaggie_breakout", "NEW", "long", frame.index[-1], 100.0, 95.0,
                    price_basis=price_basis(frame, BUILD).model_dump(mode="json"))
    with pytest.raises(SignalStateError, match="cannot arm"):
        candidate_id(signal, FRIDAY)


def test_missing_raw_anchor_minutes_still_refuse_discovery(tmp_path):
    sim = world("AAA", "BBB")
    sim.short_anchor.add("AAA")
    desk = Desk(tmp_path, sim)
    _, build = desk.build()
    assert build["status"] == "PARTIAL" and build["outcomes"]["AAA"]["reason"] == "INCOMPLETE_RAW_ANCHOR_SESSION"


# ----------------------------------------------------------------- 11 ----

def test_bounded_request_count_and_range_without_fan_out(tmp_path):
    names = [f"S{i:02d}" for i in range(25)]
    sim = world(*names)
    desk = Desk(tmp_path, sim, volume=False)
    window = ScopeWindow.discovery(BUILD)
    before = len(sim.requests)
    out, _ = sc.fetch_scoped(desk.source, ["SPY", *names], {})
    assert len(out) == 26
    bars = [b for p, b in sim.requests[before:] if p.endswith("/bars/list")]
    daily = [b for b in bars if b["timespan"] == "D"]
    assert len(daily) == 3 and len(bars) == 6                     # ETF + 20 + 5 stock batches, one anchor each
    assert {(b["count"], b["start_time"], b["end_time"]) for b in daily} == {
        (265, window.start_ms(), window.end_ms())}
    assert window.count == 265 and pd.Timestamp(window.end_ms(), unit="ms", tz="UTC") < pd.Timestamp(
        "2026-10-02 16:00", tz=ET)
    assert sorted(len(b["symbols"]) for b in daily) == [1, 5, 20]


def test_shared_batch_failure_is_not_retried_ticker_by_ticker(tmp_path):
    sim = world("AAA", "BBB")
    calls = []

    def failing(req, timeout):
        body = json.loads(req.data) if req.data else {}
        if body.get("timespan") == "D":
            calls.append(body)
            raise error.HTTPError(req.full_url, 429, "fixture rate limit", {}, io.BytesIO(b""))
        return sim(req, timeout)
    desk = Desk(tmp_path, sim)
    desk.raw._transport = failing
    out = desk.scoped("AAA", "BBB")
    assert out == {} and len(calls) == 1
    assert desk.vendor.last_errors["AAA"] == "DISCOVERY_BATCH_UNAVAILABLE (WebullHTTPError, shared by 2 tickers)"


def test_unsupported_adapter_uses_the_strict_full_path_and_says_so(tmp_path):
    sim = world("AAA")
    desk = Desk(tmp_path, sim, scoped=False)
    _, build = desk.build()
    assert build["history_scope"]["path"] == "full-history" and build["history_scope"]["requested"] == {"count": 1000}
    assert {b["count"] for b in sim.daily_requests()} == {1000}
    assert all(b["count"] == 1000 for b in sim.daily_requests())
