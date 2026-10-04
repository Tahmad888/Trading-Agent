"""Regressions for Astra's G5a audit of 2d97ea6 (F1 run stop, F2 cache failure state,
F3 approved threshold). Synthetic fixtures only; no network.

Every test here fails on 2d97ea6 and passes on the repair.
"""
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
import json
import sqlite3

import pytest

from desk import alpaca_volume
from desk.alpaca_volume import (AlpacaVolumeError, HttpReply, VolumeCache, ep_volume_component, evaluate,
                                revalidate)
from desk.playbook.cards import CARDS
from tests.test_alpaca_volume import (ENTRY, NOW, SYMBOLS, Transport, _daily, _rth, client, fetch_both,
                                      healthy_pages, requests_for)

STOPS = [(401, "AUTH_OR_ENTITLEMENT_FAILURE"), (403, "AUTH_OR_ENTITLEMENT_FAILURE"), (429, "RATE_LIMITED")]


def available(result, rth):
    return {s for s, v in evaluate(ENTRY, result, rth).items() if not isinstance(v, str)}


def bad_spy_page():
    body = json.loads(healthy_pages()[0].body)
    body["bars"]["SPY"][-1]["v"] = -1
    return HttpReply(200, {}, json.dumps(body).encode())


# ---------------------------------------------------- F1: active run stop ----

@pytest.mark.parametrize("status, code", STOPS)
def test_f1_active_stop_refuses_cached_daily_and_rth(status, code, tmp_path):
    cache = VolumeCache(tmp_path / "v.db")
    transport = Transport(*healthy_pages(), HttpReply(status, {}, b""))
    c, daily, rth = fetch_both(transport, cache=cache)  # both caches populated
    stopped = c.fetch(requests_for()[0])
    assert stopped.status == "UNAVAILABLE" and c.stopped == code
    for req in requests_for():
        reused = c.fetch(req, reuse=True)
        assert reused.status == "UNAVAILABLE" and reused.observations == {}
        assert reused.error == "RUN_STOPPED_AFTER_" + code
    assert available(c.fetch(requests_for()[0], reuse=True), rth) == set()
    assert len(transport.sent) == 3  # no request after the stop


@pytest.mark.parametrize("status, code", STOPS)
def test_f1_recorded_stop_survives_restart_until_a_successful_refresh(status, code, tmp_path):
    path = tmp_path / "v.db"
    c, _, _ = fetch_both(Transport(*healthy_pages(), HttpReply(status, {}, b"")), cache=VolumeCache(path))
    c.fetch(requests_for()[0])
    fresh = client(Transport(), cache=VolumeCache(path))
    daily_reuse, rth_reuse = (fresh.fetch(r, reuse=True) for r in requests_for())
    assert daily_reuse.failures == {s: code for s in SYMBOLS}
    assert rth_reuse.failures == {s: "PROVIDER_STOP_AFTER_LAST_SUCCESS:" + code for s in SYMBOLS}
    # A later successful refresh of the RTH key restores only that key.
    recovered = client(Transport(healthy_pages()[1]), cache=VolumeCache(path))
    assert recovered.fetch(requests_for()[1]).status == "COMPLETE"
    assert recovered.fetch(requests_for()[1], reuse=True).status == "COMPLETE"
    assert recovered.fetch(requests_for()[0], reuse=True).status == "UNAVAILABLE"


# ------------------------------------------- F2: failure state persists ----

def test_f2_failed_refresh_is_not_forgotten_across_restart_until_recovery(tmp_path):
    path = tmp_path / "v.db"
    transport = Transport(*healthy_pages(), bad_spy_page())
    c, daily, rth = fetch_both(transport, cache=VolumeCache(path))
    bad = c.fetch(requests_for()[0])
    assert bad.status == "PARTIAL" and bad.failures == {"SPY": "NONFINITE_OR_NEGATIVE_VOLUME"}

    reused = c.fetch(requests_for()[0], reuse=True)  # same run
    assert reused.status == "PARTIAL" and reused.failures == {"SPY": "NONFINITE_OR_NEGATIVE_VOLUME"}
    assert available(reused, rth) == {"NVDA", "QQQ", "AAPL"}

    restarted = client(Transport(), cache=VolumeCache(path))  # restart, no requests possible
    after_restart = restarted.fetch(requests_for()[0], reuse=True)
    assert after_restart.failures == {"SPY": "NONFINITE_OR_NEGATIVE_VOLUME"}
    assert available(after_restart, rth) == {"NVDA", "QQQ", "AAPL"}

    # Same receipt clock as the failure: sequence, not time, decides the order.
    recovery = client(Transport(healthy_pages()[0]), cache=VolumeCache(path))
    healed = recovery.fetch(requests_for()[0])
    assert healed.status == "COMPLETE" and healed.cache_status["SPY"]["status"] == "UNCHANGED"
    final = client(Transport(), cache=VolumeCache(path)).fetch(requests_for()[0], reuse=True)
    assert final.status == "COMPLETE" and available(final, rth) == set(SYMBOLS)
    # The earlier snapshot is still retained as audit history.
    with closing(sqlite3.connect(path)) as db:
        states = [r[0] for r in db.execute(
            "SELECT state FROM events WHERE symbol='SPY' AND key LIKE '%1Day%' ORDER BY sequence")]
    assert states == ["OK", "FAILED", "OK"]


def test_f2_missing_ticker_on_refresh_stays_unavailable(tmp_path):
    path = tmp_path / "v.db"
    body = json.loads(healthy_pages()[0].body)
    del body["bars"]["QQQ"]
    c, _, rth = fetch_both(Transport(*healthy_pages(), HttpReply(200, {}, json.dumps(body).encode())),
                           cache=VolumeCache(path))
    c.fetch(requests_for()[0])
    reused = client(Transport(), cache=VolumeCache(path)).fetch(requests_for()[0], reuse=True)
    assert reused.failures == {"QQQ": "SYMBOL_MISSING"} and available(reused, rth) == {"NVDA", "SPY", "AAPL"}


@pytest.mark.parametrize("reply, code", [(OSError("fixture"), "TRANSPORT_FAILURE"),
                                         (HttpReply(500, {}, b""), "HTTP_FAILURE")])
def test_f2_request_level_failure_is_persisted(reply, code, tmp_path):
    path = tmp_path / "v.db"
    c, _, _ = fetch_both(Transport(*healthy_pages(), reply), cache=VolumeCache(path))
    c.fetch(requests_for()[0])
    reused = client(Transport(), cache=VolumeCache(path)).fetch(requests_for()[0], reuse=True)
    assert reused.status == "UNAVAILABLE" and reused.failures == {s: code for s in SYMBOLS}


def test_f2_identity_change_is_persisted(tmp_path, monkeypatch):
    path = tmp_path / "v.db"
    fetch_both(Transport(*healthy_pages()), cache=VolumeCache(path))
    monkeypatch.setattr(alpaca_volume, "MAPPING", "fixture: a different identity mapping")
    later = NOW + timedelta(hours=1)
    changed = client(Transport(healthy_pages()[0]), cache=VolumeCache(path), now=later).fetch(requests_for()[0])
    assert changed.failures == {s: "IDENTITY_CHANGED" for s in SYMBOLS}
    monkeypatch.undo()
    reused = client(Transport(), cache=VolumeCache(path), now=later).fetch(requests_for()[0], reuse=True)
    assert reused.status == "UNAVAILABLE" and reused.failures == {s: "IDENTITY_CHANGED" for s in SYMBOLS}


def test_f2_reuse_never_sends_a_request(tmp_path):
    transport = Transport()
    result = client(transport, cache=VolumeCache(tmp_path / "v.db")).fetch(requests_for()[0], reuse=True)
    assert transport.sent == [] and result.failures == {s: "NOT_CACHED" for s in SYMBOLS}


# --------------------------------------------- F3: approved threshold ----

def test_f3_caller_cannot_supply_a_threshold():
    with pytest.raises(TypeError):
        ep_volume_component(ENTRY, _daily(), _rth(volumes=(1, 1)), threshold=Decimal("0.001"))


@pytest.mark.parametrize("threshold", [Decimal("0.001"), Decimal("0.9")])
def test_f3_unsupported_threshold_cannot_claim_the_policy(threshold):
    good = ep_volume_component(ENTRY, _daily(), _rth(volumes=(1, 1)))
    data = good.model_dump()
    data.update(threshold=threshold, threshold_met=data["first30_volume"] * 50 >= threshold * data["prior50_total"])
    with pytest.raises(ValueError):
        type(good).model_validate(data)


def test_f3_stored_altered_result_is_rejected_on_revalidation():
    daily, rth = _daily(), _rth(volumes=(1, 1))
    good = ep_volume_component(ENTRY, daily, rth)
    assert good.ratio == Decimal("0.002") and good.threshold == Decimal("0.5") and not good.threshold_met
    forged = good.model_copy(update={"threshold": Decimal("0.001"), "threshold_met": True})
    with pytest.raises(AlpacaVolumeError, match="VOLUME_RULE_CHANGED_REQUALIFY"):
        revalidate(forged, daily, rth)
    flipped = good.model_copy(update={"threshold_met": True})
    with pytest.raises(AlpacaVolumeError, match="VOLUME_RESULT_MISMATCH"):
        revalidate(flipped, daily, rth)
    revalidate(good, daily, rth)


def test_f3_approved_rule_change_requires_fresh_qualification(monkeypatch):
    daily, rth = _daily(), _rth(volumes=(250, 250))
    old = ep_volume_component(ENTRY, daily, rth)
    card = CARDS["5_qullamaggie_episodic_pivot"]
    params = dict(card.params)
    params["min_gap"] = params["min_gap"].model_copy(update={"value": params["min_gap"].value + 0.01})
    monkeypatch.setitem(CARDS, card.id, card.model_copy(update={"params": params}))
    with pytest.raises(AlpacaVolumeError, match="VOLUME_RULE_CHANGED_REQUALIFY"):
        revalidate(old, daily, rth)
    assert ep_volume_component(ENTRY, daily, rth).rule_version != old.rule_version


def test_f3_legitimate_boundary_unchanged():
    at = ep_volume_component(ENTRY, _daily(), _rth(volumes=(250, 250)))
    below = ep_volume_component(ENTRY, _daily(), _rth(volumes=(250, 249)))
    assert at.threshold_met and not below.threshold_met and at.threshold == Decimal("0.5")
    assert at.rule_version == CARDS["5_qullamaggie_episodic_pivot"].fingerprint()
