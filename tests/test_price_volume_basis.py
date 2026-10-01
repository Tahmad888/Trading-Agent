"""Decision-path tests; all action/volume attestations here are fictional fixtures."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from desk import scanner as sc
from desk.bar_contract import (BarProvenance, completed_daily, completed_intraday,
                               developing_daily_from_m15, hourly_from_m15)
from desk.bars import BarDataError
from desk.data_basis import VolumeBasis, price_basis, compatible_prices, compatible_volume
from desk.indicators import daily_features
from desk.playbook.cards import CARDS
from desk.playbook.filters import MarketSize
from desk.playbook.triggers import episodic_pivot, connors_rsi2
from desk.webull import WebullData, WebullError
from tests.basis_support import price_evidence, volume_evidence
from tests.charts import features
from tests.test_data_contracts import intraday, now
from tests.test_scanner import daily, sig, stamp, Fake, frames, DAY
from tests.test_triggers import EP, VCP, RSI2_DIP, ctx, dry
from tests.test_triggers import PASS
from tests.test_webull import FakeTransport, bar


def action(kind="split", day="2026-09-29", revision="1"):
    return dict(source="fixture", evidence_ref="fictional action", event_id="action-1",
                kind=kind, effective_session=day, revision=revision)


def set_basis(frame, *, day="2026-09-29", symbol="LEAD", normalization="split_adjusted", actions=()):
    frame.attrs["bar_provenance"].update(adjustment=normalization,
        price_basis=price_evidence(day, symbol, normalization, actions))
    return frame


def test_legacy_labels_cannot_qualify_and_different_labels_do_not_override_evidence():
    bars = intraday(2)
    with pytest.raises(BarDataError, match="corporate-action"):
        sc.entry_hit(replace(sig(), price_basis=None), bars, now())
    before = sc.entry_hit(sig(), bars, now())
    bars.attrs["bar_provenance"]["price_scale_id"] = "different-but-irrelevant-label"
    assert sc.entry_hit(sig(), bars, now()) == before


@pytest.mark.parametrize("kind", ["split", "cash_dividend"])
def test_new_action_blocks_old_signal_then_fresh_adjusted_history_can_compare(kind):
    events = (action(kind),)
    bars = set_basis(intraday(2), normalization="unadjusted", actions=events)
    with pytest.raises(BarDataError, match="Changed corporate-action"):
        sc.entry_hit(sig(), bars, now())
    history = set_basis(daily([100] * 60), normalization="split_dividend_adjusted", actions=events)
    completed = completed_daily(history, now())
    old_prices = completed.copy()
    proof = price_basis(completed, now()).model_dump(mode="json")
    compatible_prices(proof, bars, now(), symbol="LEAD")
    pd.testing.assert_frame_equal(completed, old_prices)  # never adjust already adjusted prices twice


@pytest.mark.parametrize("change", ["revision", "deletion", "effective_date", "security", "symbol", "currency"])
def test_revised_action_or_identity_invalidates_persisted_levels(change):
    old = price_evidence("2026-09-28", actions=(action(day="2026-09-25"),))
    bars = set_basis(intraday(2), actions=(action(day="2026-09-25"),))
    basis = bars.attrs["bar_provenance"]["price_basis"]
    if change == "revision":
        basis["actions"][0]["revision"] = "2"
    elif change == "deletion":
        basis["actions"] = []
    elif change == "effective_date":
        basis["actions"][0]["effective_session"] = "2026-09-24"
    else:
        basis[{"security": "security_id", "symbol": "symbol", "currency": "currency"}[change]] = "DIFFERENT"
    with pytest.raises(BarDataError):
        compatible_prices(old, bars, now(), symbol="LEAD")


def test_same_ledger_new_fetch_time_preserves_no_action_continuity():
    bars = intraday(2)
    bars.attrs["bar_provenance"]["price_basis"]["verified_at"] = now(time="09:59").isoformat()
    compatible_prices(sig().price_basis, bars, now())


@pytest.mark.parametrize("field,value", [
    ("coverage_complete", False), ("coverage_complete", 1), ("actions", None),
    ("verified_at", "2026-09-29T10:01:00-04:00"), ("verified_at", "2026-09-29T09:00:00"),
    ("basis_session", "2026-09-30"), ("coverage_start", "2026-09-30"),
])
def test_missing_incomplete_or_future_price_evidence_cannot_pass(field, value):
    bars = intraday(2)
    bars.attrs["bar_provenance"]["price_basis"][field] = value
    with pytest.raises(BarDataError):
        completed_intraday(bars, now())


def test_shortened_coverage_cannot_hide_a_historical_action_revision():
    bars = intraday(2)
    bars.attrs["bar_provenance"]["price_basis"]["coverage_start"] = "2026-09-01"
    with pytest.raises(BarDataError, match="coverage"):
        compatible_prices(sig().price_basis, bars, now())


def test_unknown_and_duplicate_actions_are_not_silently_ignored():
    for events in [(action("unsupported"),), (action(), action())]:
        bars = intraday(2)
        bars.attrs["bar_provenance"]["price_basis"]["actions"] = events
        with pytest.raises(BarDataError):
            completed_intraday(bars, now())


@pytest.mark.parametrize("normalization,kind", [("unadjusted", "split"), ("unadjusted", "cash_dividend"),
                                               ("split_adjusted", "cash_dividend")])
def test_history_cannot_cross_an_unapplied_action(normalization, kind):
    history = set_basis(daily([100] * 60), normalization=normalization, actions=(action(kind),))
    with pytest.raises(BarDataError, match="unapplied"):
        completed_daily(history, now(time="16:10"))


def test_hourly_history_detects_action_between_individually_valid_sessions():
    first = intraday(26, pd.Timestamp("2026-09-28").date())
    second = intraday(2)
    bars = set_basis(stamp(pd.concat([first, second]), "M15"), normalization="unadjusted", actions=(action(),))
    with pytest.raises(BarDataError, match="unapplied"):
        hourly_from_m15(bars, now(time="10:30"))


def test_signal_price_evidence_survives_restart_and_legacy_record_is_readable(tmp_path):
    log = sc.ScanLog(tmp_path)
    log.add_armed(DAY, MarketSize.FULL, [sig(), replace(sig(), price_basis=None)])
    market, restored = sc.ScanLog(tmp_path).load_armed(DAY)
    assert market == MarketSize.FULL and restored[0].price_basis == sig().price_basis
    assert sc.entry_hit(restored[0], intraday(2), now()) == sc.entry_hit(sig(), intraday(2), now())
    with pytest.raises(BarDataError, match="corporate-action"):
        sc.entry_hit(restored[1], intraday(2), now())


def test_wrong_security_never_remains_in_leader_scan_after_validation_failure(tmp_path):
    class WrongSecurity(Fake):
        def bars(self, symbols, **kw):
            out = super().bars(symbols, **kw)
            if "SPY" in out:
                out["SPY"].attrs["bar_provenance"]["price_basis"]["symbol"] = "WRONG"
            return out

    record = sc.leader_scan_job(WrongSecurity(frames()), sc.ScanLog(tmp_path), now(time="16:40"))
    assert record.error == "no valid completed SPY bars"
    assert "different symbol" in record.skipped["SPY"]
    assert not (tmp_path / "watchlist.json").exists()


@pytest.mark.parametrize("field,value", [("definition_id", None), ("definition_id", "different-sale-conditions"),
                                       ("share_basis_id", None), ("share_basis_id", "pre-split-shares")])
def test_ep_cannot_compare_unknown_or_different_volume_definitions(field, value):
    f = features(EP)
    early = volume_evidence("first 30m")
    early[field] = value
    with pytest.raises(BarDataError, match="volume"):
        episodic_pivot(f, CARDS["5_qullamaggie_episodic_pivot"],
                      ctx(today_open=57.5, early_volume=1.5e6, early_volume_basis=early))


def test_ep_valid_comparable_volume_retains_existing_threshold_and_metadata():
    f = features(EP)
    assert f.attrs["volume_basis"] == volume_evidence()
    card = CARDS["5_qullamaggie_episodic_pivot"]
    threshold = card.p("early_volume") * f.volume.iloc[-50:].mean()
    assert episodic_pivot(f, card, ctx(today_open=57.5, early_volume=threshold)) is not None
    assert episodic_pivot(f, card, ctx(today_open=57.5, early_volume=threshold - 1)) is None


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -1, None])
def test_ep_invalid_volume_is_not_a_qualifying_spike(bad):
    with pytest.raises(BarDataError, match="volume"):
        episodic_pivot(features(EP), CARDS["5_qullamaggie_episodic_pivot"],
                      ctx(today_open=57.5, early_volume=bad))


def test_composed_daily_volume_is_unavailable_without_breaking_price_only_rsi2():
    history = stamp(daily(RSI2_DIP), "D", "IWM")
    bars = stamp(intraday(), "M15", "IWM")
    bars.loc[:, "open"] = 390.0
    bars.loc[:, "high"] = 391.0
    bars.loc[:, "low"] = 380.0
    bars.loc[:, "close"] = 381.0
    bars.attrs["volume_basis"]["definition_id"] = "different-from-native-daily"
    snapshot = developing_daily_from_m15(history, bars, now(time="15:45"))
    f = daily_features(snapshot)
    assert pd.isna(f.rel_volume.iloc[-1]) and np.isfinite(f.rel_volume.iloc[-2])
    assert "not provider daily volume" in snapshot.attrs["developing_components"]["volume"]
    assert connors_rsi2(f, CARDS["10_connors_rsi2"], ctx(symbol="IWM")) is not None
    skipped = {}
    result = sc.rsi2_estimate(Fake({("IWM", "D"): history, ("IWM", "M15"): bars}),
                              ["IWM"], MarketSize.FULL, now(time="15:45"), skipped)
    assert len(result) == 1 and not skipped
    assert "developing_components" in result[0].saw
    with pytest.raises(BarDataError, match="Developing"):
        compatible_volume(f, volume_evidence())


def test_missing_volume_evidence_is_logged_per_setup(monkeypatch):
    monkeypatch.setattr(sc, "trend_template", lambda *args: PASS)
    data = frames()
    data[("LEAD", "D")] = daily(VCP, spread=.005)
    data[("LEAD", "D")]["volume"] = dry(VCP)
    data[("LEAD", "D")].attrs.pop("volume_basis")
    record, signals = sc.close_scan(Fake(data), ["LEAD"], now(time="16:10"))
    assert "LEAD/2_minervini_vcp" in record.skipped
    assert all(s.setup_id != "2_minervini_vcp" for s in signals)
    assert record.error is None


def test_failed_volume_check_does_not_discard_price_only_signal(monkeypatch):
    from desk.playbook import triggers
    from desk.data_basis import volume_basis
    from tests.test_triggers import QULL
    monkeypatch.setitem(triggers.CHECKS, "2_minervini_vcp", lambda f, card, context: volume_basis(f))
    data = features(QULL, spread=.02)
    data.attrs.pop("volume_basis")
    skipped = {}
    signals = triggers.scan(data, ctx(), skipped=skipped)
    assert "XYZ/2_minervini_vcp" in skipped
    assert "1_qullamaggie_breakout" in {s.setup_id for s in signals}


def test_luk_does_not_use_unverified_volume_for_anchored_vwap():
    from desk.playbook.triggers import luk_reclaim
    from tests.test_triggers import LUK
    frame = features(LUK)
    frame.attrs.pop("volume_basis")
    with pytest.raises(BarDataError, match="volume"):
        luk_reclaim(frame, CARDS["7_luk_pullback_reclaim"], ctx())


def test_price_screen_diagnostic_still_works_without_volume_provenance():
    from desk.screen_check import table
    from tests.test_indicators import random_bars
    frame = random_bars()
    rows = [{"time": index.isoformat(), **row.to_dict()} for index, row in frame.iterrows()]
    result = dict(table(rows))
    assert float(result["Close"]) == round(frame.close.iloc[-1], 2)
    assert "SMA 200" in result


@pytest.mark.parametrize("instrument", [None, "wrong-instrument"])
def test_adapter_cannot_attach_price_proof_for_wrong_or_unknown_security(instrument):
    profile = BarProvenance.model_validate(stamp(daily([100]), "D", "SPY").attrs["bar_provenance"])
    row = {"symbol": "SPY", "instrument_id": instrument, "delay_minutes": 0,
           "result": [bar("2026-09-28T04:00:00Z", 100)]}
    source = WebullData("fixture", "fixture", transport=FakeTransport([row]), min_interval=0,
                        clock=lambda: now(), bar_profile=lambda symbol, tf: profile)
    with pytest.raises(WebullError, match="instrument identity"):
        source.bars(["SPY"], category="US_ETF", timespan="D")


def test_adapter_default_volume_is_explicitly_unknown_and_verified_hook_is_separate():
    row = {"symbol": "SPY", "delay_minutes": 0, "result": [bar("2026-09-28T04:00:00Z", 100)]}
    source = WebullData("fixture", "fixture", transport=FakeTransport([row]), min_interval=0)
    frame = source.bars(["SPY"], category="US_ETF", timespan="D")["SPY"]
    assert frame.attrs["volume_basis"]["channel"] == "native:D"
    assert frame.attrs["volume_basis"]["definition_id"] is None
    with pytest.raises(BarDataError):
        compatible_volume(frame, volume_evidence())
    verified = WebullData("fixture", "fixture", transport=FakeTransport([row]), min_interval=0,
                         volume_profile=lambda s, tf: VolumeBasis.model_validate(volume_evidence()))
    frame = verified.bars(["SPY"], category="US_ETF", timespan="D")["SPY"]
    compatible_volume(frame, volume_evidence("synthetic first 30m"))
