import numpy as np
import pytest

from desk.bars import BarDataError
from desk.playbook import triggers as t
from desk.playbook.cards import CARDS
from desk.playbook.filters import GateResult, MarketSize, trend_template
from tests.charts import chart, features, line
from tests.basis_support import volume_evidence

PASS, FAIL = GateResult({"ok": True}), GateResult({"ok": False})


def ctx(symbol="XYZ", market=MarketSize.FULL, template=PASS, **kw):
    if "early_volume" in kw:
        kw.setdefault("early_volume_basis", volume_evidence("synthetic first 30m"))
    return t.Context(symbol, market, template, **kw)


def run(setup_id, f, c):
    return t.CHECKS[setup_id](f, CARDS[setup_id], c)


def dry(cl, days=10):
    vol = np.full(len(cl), 2e6)
    vol[-days:] = 1e6
    return vol


# Made-up charts, one per setup, each drawn to show the setup on its last bar.
QULL = np.r_[line((0, 60), (370, 105)), line((0, 105), (30, 150))[1:],
             [150, 142, 149, 143, 148, 144, 149, 145, 148, 146, 148, 146.5, 148, 147, 148.5, 147, 148.5, 147.5, 148.5, 148]]
VCP = line((0, 60), (400, 150), (430, 120), (450, 145), (460, 133), (470, 142), (475, 137), (478, 140))
CUP = line((0, 80), (200, 100), (350, 150), (410, 115), (470, 148), (480, 140), (482, 141))
DARVAS = line((0, 80), (400, 150), (403, 148.5), (407, 149.3))
EP = line((0, 50), (300, 52), (330, 48), (360, 51))
KELL = line((0, 130), (300, 100), (325, 125), (328, 119))
LUK = line((0, 80), (300, 100), (360, 140), (366, 134.5))
GRAIL_UP = line((0, 100), (300, 110), (340, 160), (344, 150))
GRAIL_DOWN = line((0, 160), (300, 150), (340, 100), (344, 110))
STAGE4 = np.r_[line((0, 80), (300, 150)), 150 + 2 * np.sin(np.arange(140) / 3), line((0, 149), (6, 140))[1:]]
SPY_UP = chart(line((0, 300), (600, 360)))["close"]
RSI2_DIP = line((0, 300), (400, 400), (403, 390))
RSI2_POP = line((0, 400), (400, 300), (403, 310))


def test_qullamaggie_breakout():
    s = run("1_qullamaggie_breakout", features(QULL, spread=0.02), ctx())
    assert s and s.direction == "long" and s.stop is None and s.stop_basis == "session_low"
    assert "up 54" in s.saw["prior move"]
    # A calm stock (ADR% under 3.5) doesn't qualify.
    assert run("1_qullamaggie_breakout", features(QULL, spread=0.002), ctx()) is None


def test_minervini_vcp():
    s = run("2_minervini_vcp", features(VCP, volume=dry(VCP)), ctx())
    assert s and s.saw["contractions"].count("→") == 2
    assert (s.trigger - s.stop) / s.trigger <= 0.08
    # Without the volume dry-up, no signal.
    assert run("2_minervini_vcp", features(VCP), ctx()) is None
    # Pullbacks that get deeper instead of shallower are not a VCP.
    wide = line((0, 60), (400, 150), (430, 140), (450, 148), (460, 130), (470, 142), (475, 137), (478, 140))
    assert run("2_minervini_vcp", features(wide, volume=dry(wide)), ctx()) is None


def test_oneil_cup_with_handle():
    s = run("3_oneil_cup_with_handle", features(CUP, volume=dry(CUP, 12)), ctx())
    assert s and s.target == pytest.approx(s.trigger * 1.2, abs=0.02)
    assert s.stop >= s.trigger * 0.92 - 0.01
    # A V-shaped cup (the low right after the left high) is not a cup.
    v = line((0, 80), (200, 100), (350, 150), (360, 115), (470, 148), (480, 140), (482, 141))
    assert run("3_oneil_cup_with_handle", features(v, volume=dry(v, 12)), ctx()) is None


def test_darvas_box():
    s = run("4_darvas_box", features(DARVAS), ctx())
    assert s and s.trigger == pytest.approx(150.75) and s.stop < 148.5
    # Still making new highs: no box yet.
    assert run("4_darvas_box", features(line((0, 80), (400, 150))), ctx()) is None


def test_episodic_pivot_needs_a_big_gap_and_heavy_volume():
    f = features(EP, spread=0.02)
    s = run("5_qullamaggie_episodic_pivot", f, ctx(template=None, today_open=57.5, early_volume=1.5e6))
    assert s and s.trigger == 57.5
    assert run("5_qullamaggie_episodic_pivot", f, ctx(today_open=54.0, early_volume=1.5e6)) is None
    assert run("5_qullamaggie_episodic_pivot", f, ctx(today_open=57.5, early_volume=2e5)) is None
    assert run("5_qullamaggie_episodic_pivot", f, ctx()) is None


def test_kell_crossback_first_pullback_only():
    s = run("6_kell_ema_crossback", features(KELL), ctx())
    assert s and s.saw["pullback"].startswith("first dip")
    second = line((0, 130), (300, 100), (315, 120), (318, 116), (330, 130), (333, 124))
    assert run("6_kell_ema_crossback", features(second), ctx()) is None


def test_luk_pullback_to_the_21_ema():
    s = run("7_luk_pullback_reclaim", features(LUK), ctx())
    assert s and "21 EMA" in s.saw["pullback"]
    # A slow grinder (up under 30% in 6 months) doesn't qualify.
    slow = line((0, 80), (300, 100), (360, 115), (366, 112))
    assert run("7_luk_pullback_reclaim", features(slow), ctx()) is None


def test_holy_grail_both_ways():
    up = run("8_raschke_holy_grail", features(GRAIL_UP), ctx())
    assert up and up.direction == "long" and up.target > up.trigger
    down = run("8_raschke_holy_grail", features(GRAIL_DOWN), ctx(template=None))
    assert down and down.direction == "short" and down.stop > down.trigger > down.target


def test_weinstein_stage4_only_below_full_size():
    f = features(STAGE4)
    spy = SPY_UP.iloc[:len(STAGE4)]
    s = run("9_weinstein_stage4_breakdown", f, ctx(market=MarketSize.HALF, template=None, spy_close=spy))
    assert s and s.direction == "short" and s.stop > s.trigger
    assert run("9_weinstein_stage4_breakdown", f, ctx(market=MarketSize.FULL, spy_close=spy)) is None


def test_connors_rsi2_both_ways_on_index_etfs_only():
    long_ = run("10_connors_rsi2", features(RSI2_DIP), ctx("SPY", template=None))
    assert long_ and long_.direction == "long"
    short = run("10_connors_rsi2", features(RSI2_POP), ctx("QQQ", template=None))
    assert short and short.direction == "short"
    assert run("10_connors_rsi2", features(RSI2_DIP), ctx("AAPL")) is None
    atr10 = features(RSI2_DIP)["atr_10"].iloc[-1]
    assert long_.trigger - long_.stop == pytest.approx(2.5 * atr10, abs=0.02)


def test_ep_and_rsi_longs_reach_review_in_bearish_markets():
    market = MarketSize.NO_NEW_LONGS
    assert run("5_qullamaggie_episodic_pivot", features(EP, spread=0.02),
               ctx(market=market, template=None, today_open=57.5, early_volume=1.5e6)) is not None
    assert run("10_connors_rsi2", features(RSI2_DIP), ctx("SPY", market=market, template=None)) is not None


@pytest.mark.parametrize("setup_id,cl,kw", [
    ("1_qullamaggie_breakout", QULL, {"spread": 0.02}),
    ("2_minervini_vcp", VCP, {"volume": dry(VCP)}),
    ("6_kell_ema_crossback", KELL, {}),
    ("7_luk_pullback_reclaim", LUK, {}),
    ("8_raschke_holy_grail", GRAIL_UP, {}),
])
def test_bearish_market_does_not_suppress_longs_but_setup_filters_remain(setup_id, cl, kw):
    f = features(cl, **kw)
    assert run(setup_id, f, ctx(market=MarketSize.NO_NEW_LONGS)) is not None
    if CARDS[setup_id].trend_template:
        assert run(setup_id, f, ctx(template=FAIL)) is None


def test_short_bars_fail_closed():
    f = features(line((0, 100), (30, 110)))
    with pytest.raises(BarDataError):
        run("10_connors_rsi2", f, ctx("SPY"))
    with pytest.raises(BarDataError):
        run("8_raschke_holy_grail", f, ctx())


def test_stops_on_the_wrong_side_are_refused():
    with pytest.raises(ValueError):
        t.Signal("x", "XYZ", "long", SPY_UP.index[-1], trigger=100, stop=101)


def test_scan_uses_the_real_trend_template():
    f = features(QULL, spread=0.02)
    spy = SPY_UP.iloc[:len(QULL)]
    gate = trend_template(f, spy)
    found = t.scan(f, t.Context("XYZ", MarketSize.FULL, gate, spy))
    assert gate.passed and "1_qullamaggie_breakout" in {s.setup_id for s in found}


def test_every_card_has_a_check():
    assert set(t.CHECKS) == set(CARDS)


def test_episodic_pivot_growth_gate_and_tag():
    card = CARDS["5_qullamaggie_episodic_pivot"]
    assert t.growth_group(card, 0.70, 0.30) == "50%+"          # EPS or sales counts
    assert t.growth_group(card, 0.40, 0.35) == "25-50%"        # qualifies, tagged as the smaller group
    assert t.growth_group(card, 0.20, None) is None
    assert t.growth_group(card, None, None) is None            # no numbers, no trade
