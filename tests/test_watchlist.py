import numpy as np

from desk.watchlist import ALWAYS, build_watchlist, leader_scan
from tests.test_filters import N, SPY, bars_from_path, path
from tests.basis_support import volume_evidence


def stock(pct, start=100.0, volume=2e6):
    df = bars_from_path(path((N, pct), start=start))
    df["volume"] = volume
    df.attrs["volume_basis"] = volume_evidence()
    return df


def test_leader_scan_ranks_strength_and_keeps_only_trend_template_names():
    universe = {"FAST": stock(300), "GOOD": stock(150), "LAG": stock(5),
                "CHEAP": stock(300, start=2.0), "THIN": stock(300, volume=1e5),
                "SHORT": stock(300).iloc[-100:]}
    scan = leader_scan(universe, SPY["close"])
    assert [l.symbol for l in scan.leaders] == ["FAST", "GOOD"]
    assert scan.leaders[0].score > scan.leaders[1].score
    assert scan.failed_template == 1 and scan.ranked == 3
    assert set(scan.skipped) == {"CHEAP", "THIN", "SHORT"}


def test_leader_scan_respects_the_limit():
    universe = {f"S{i}": stock(100 + 20 * i) for i in range(5)}
    assert [l.symbol for l in leader_scan(universe, SPY["close"], limit=2).leaders] == ["S4", "S3"]


def test_watchlist_sources_and_removals():
    wl = build_watchlist(["nvda", "AMD"], movers=["NKE", "AMD"], added=["TSLA"], removed=["AMD", "SPY"])
    assert set(ALWAYS) <= set(wl) and wl["SPY"] == ["always"]
    assert "AMD" not in wl and wl["NVDA"] == ["leader scan"] and wl["TSLA"] == ["Taz"]
