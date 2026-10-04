"""G5a checkpoint 2, section 10.1 and the asset route: identity joins and isolation.

All asset rows are synthetic fixtures (labelled), not Alpaca data. No network.
"""
from datetime import date, datetime, timezone
from pathlib import Path
import json

import pytest

from desk import alpaca_assets as aa
from desk.alpaca_assets import AssetError, IdentityStore, alpaca_symbol, parse_assets
from desk.alpaca_volume import RequestBudget
from desk.security import SecurityMetadata
from tests.alpaca_support import KEY, SECRET, AlpacaSim, Clock, asset_id, asset_row, provider

NOW = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)
SESSION = date(2026, 10, 2)


def meta(symbol, instrument=None, provider_symbol=None):
    return SecurityMetadata(symbol=symbol, provider_symbol=provider_symbol, instrument_id=instrument or f"fixture:{symbol}",
                            name=symbol + " Inc", category="US_STOCK", sub_category="COMMON_STOCK",
                            exchange_code="NAS", currency="USD", observed_at=NOW)


def assets(*rows, at=NOW):
    return parse_assets(json.dumps(list(rows)).encode(), at)


def test_exact_join_keeps_webull_and_alpaca_ids_apart_and_reuses_the_pin(tmp_path):
    store = IdentityStore(tmp_path / "v.sqlite")
    out = store.resolve({"NVDA": meta("NVDA")}, assets(asset_row("NVDA")), SESSION)
    record = out["NVDA"]
    assert (record.webull_instrument_id, record.alpaca_asset_id) == ("fixture:NVDA", asset_id("NVDA"))
    assert record.method == "exact-symbol" and record.version == 1 and record.valid_after_session is None
    assert "asof omitted" in record.asof_policy and "not independently proven" in record.historical_mapping
    # A later list with the same identity (new receipt, renamed company) is the same pin.
    again = store.resolve({"NVDA": meta("NVDA")}, assets(asset_row("NVDA", name="Renamed"), at=NOW.replace(hour=3)),
                          SESSION)["NVDA"]
    assert again == record and len(store.history("NVDA")) == 1


def test_class_share_alias_is_explicit_and_punctuation_is_never_stripped(tmp_path):
    store = IdentityStore(tmp_path / "v.sqlite")
    out = store.resolve({"BRK.B": meta("BRK.B", "916040668", "BRK B")},
                        assets(asset_row("BRK.B"), asset_row("BRKB")), SESSION)
    assert out["BRK.B"].alpaca_symbol == "BRK.B" and out["BRK.B"].method == "explicit-class-share-alias"
    assert out["BRK.B"].webull_symbol == "BRK B"
    with pytest.raises(AssetError, match="CLASS_SHARE_ALIAS_REQUIRED"):
        alpaca_symbol("BF.B")                     # no alias written: never guessed as BFB or BF-B
    wrong = store.resolve({"BRK.B": meta("BRK.B", "not-the-webull-id")}, assets(asset_row("BRK.B")), SESSION)
    assert wrong["BRK.B"] == "WEBULL_ALIAS_ID_MISMATCH"


def test_ambiguous_missing_inactive_and_conflicting_identities_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setitem(aa.CLASS_SHARE_ALIASES, "BRK.X", "BRK.B")
    store = IdentityStore(tmp_path / "v.sqlite")
    rows = [asset_row("AAPL"), asset_row("DUP"), asset_row("DUP", id=asset_id("DUP", "2")),
            asset_row("OLD", status="inactive"), asset_row("COIN", cls="crypto"), asset_row("BRK.B")]
    out = store.resolve({s: meta(s) for s in ("AAPL", "DUP", "OLD", "COIN", "GONE", "BRK.X")} |
                        {"BRK.B": meta("BRK.B", "916040668")}, assets(*rows), SESSION)
    assert out["AAPL"].alpaca_asset_id == asset_id("AAPL")
    assert out["DUP"] == "ASSET_AMBIGUOUS" and out["GONE"] == "ASSET_NOT_FOUND"
    assert out["OLD"] == out["COIN"] == "ASSET_UNSUPPORTED"
    assert out["BRK.X"] == out["BRK.B"] == "ASSET_CONFLICT"
    assert store.resolve({"NOMETA": None}, assets(asset_row("NOMETA")), SESSION)["NOMETA"] == "WEBULL_IDENTITY_MISSING"


def test_changed_identity_gets_a_new_version_and_old_history_is_kept(tmp_path):
    store = IdentityStore(tmp_path / "v.sqlite")
    first = store.resolve({"NVDA": meta("NVDA")}, assets(asset_row("NVDA")), SESSION)["NVDA"]
    changed = store.resolve({"NVDA": meta("NVDA")}, assets(asset_row("NVDA", id=asset_id("NVDA", "new"))),
                            date(2026, 10, 5))["NVDA"]
    assert changed.version == 2 and changed.valid_after_session == date(2026, 10, 5)
    assert changed.namespace != first.namespace and store.history("NVDA") == [first, changed]
    # The old asset ID now listed under another symbol is reuse, not a fresh identity.
    reused = store.resolve({"NVDX": meta("NVDX")}, assets(asset_row("NVDX", id=asset_id("NVDA", "new"))), SESSION)
    assert reused["NVDX"] == "ASSET_REUSED_BY_ANOTHER_SYMBOL"
    # A changed Webull instrument behind the same desk symbol is also a new version.
    webull = store.resolve({"NVDA": meta("NVDA", "fixture:other")}, assets(asset_row("NVDA", id=asset_id("NVDA", "new"))),
                           date(2026, 10, 6))["NVDA"]
    assert webull.version == 3 and webull.valid_after_session == date(2026, 10, 6)


def test_whole_list_is_rejected_when_one_row_is_unattributable():
    with pytest.raises(AssetError, match="INVALID_ASSET_LIST"):
        assets(asset_row("AAPL"), {"symbol": "X", "status": "active"})
    with pytest.raises(AssetError, match="INVALID_JSON"):
        parse_assets(b"<html>", NOW)


def test_asset_route_is_one_read_only_get_on_the_allowlisted_paper_host(tmp_path):
    sim = AlpacaSim(symbols=["AAPL"])
    run = provider(tmp_path / "v.sqlite", sim, Clock(NOW))
    listing = run.client.fetch_assets()
    assert sim.sent == ["https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity"]
    assert sim.methods == ["GET"] and listing.assets[0].symbol == "AAPL"
    text = json.dumps(run.client.asset_receipt)
    assert KEY not in text and SECRET not in text
    assert run.requests_used == 1                 # the asset list spends the run's bar budget


def test_no_account_order_or_position_route_exists_in_the_volume_path():
    root = Path(__file__).resolve().parents[1] / "src" / "desk"
    for name in ("alpaca_assets.py", "alpaca_source.py", "alpaca_volume.py", "alpaca_probe.py"):
        text = (root / name).read_text()
        for route in ("/v2/account", "/v2/orders", "/v2/positions", "POST", "DELETE"):
            assert route not in text, (name, route)


@pytest.mark.parametrize("status,code", [(401, "AUTH_OR_ENTITLEMENT_FAILURE"), (403, "AUTH_OR_ENTITLEMENT_FAILURE"),
                                         (429, "RATE_LIMITED")])
def test_asset_stop_blocks_every_later_volume_request_in_the_run(tmp_path, status, code):
    sim = AlpacaSim(symbols=["AAPL"], status=[status])
    run = provider(tmp_path / "v.sqlite", sim, Clock(NOW))
    with pytest.raises(AssetError, match=code):
        run.client.fetch_assets()
    with pytest.raises(AssetError, match="RUN_STOPPED_AFTER_" + code):
        run.client.fetch_assets()
    assert sim.calls() == 1


def test_echoed_credentials_in_the_asset_reply_are_refused_and_not_hashed(tmp_path):
    sim = AlpacaSim(symbols=["AAPL"], echo_key=True)
    run = provider(tmp_path / "v.sqlite", sim, Clock(NOW))
    with pytest.raises(AssetError, match="UNSAFE_RESPONSE"):
        run.client.fetch_assets()
    assert run.client.asset_receipt["sha256"] is None


def test_asset_fetch_respects_the_shared_budget(tmp_path):
    from desk.alpaca_volume import AlpacaVolumeClient, VolumeCache
    sim = AlpacaSim(symbols=["AAPL"])
    client = AlpacaVolumeClient(KEY, SECRET, budget=RequestBudget(1), transport=sim, clock_fn=Clock(NOW),
                                cache=VolumeCache(tmp_path / "v.sqlite"))
    client.fetch_assets()
    with pytest.raises(AssetError, match="REQUEST_BUDGET_EXHAUSTED"):
        client.fetch_assets()
    assert sim.calls() == 1
