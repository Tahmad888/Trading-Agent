"""G2 acceptance: independent observations, deliberate tampering, no orders."""
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from fractions import Fraction
import sqlite3

import pytest

from desk import scanner as sc
from desk.playbook.cards import CARDS
from desk.risk import evaluate
from desk.risk_terms import EventRiskSource, stop_distance
from desk.signal_state import SignalStore
from tests.conftest import NOW
from tests.risk_support import BOOK, MARKET, REGISTRY, FixtureTerms
from tests.test_risk import failed
from tests.test_scanner import Fake, m15, sig
from tests.test_signal_lifecycle import BASE, now

BREAKOUT = "1_qullamaggie_breakout"
EP = "5_qullamaggie_episodic_pivot"


class Source(FixtureTerms):
    def __init__(self, **changes):
        self.changes = changes

    def resolve(self, event_id, at):
        return super().resolve(event_id, at).model_copy(update=self.changes)


def risk(p, a, **kw):
    return evaluate(p, a, now=NOW, contract_book=BOOK, registry=REGISTRY, market=MARKET,
                    terms_source=kw.pop("terms_source", Source()), **kw)


@pytest.mark.parametrize("setup", [BREAKOUT, EP])
def test_session_low_bound_at_observation_then_frozen_and_invalidated(tmp_path, setup):
    candidate = replace(sig(setup), stop=None, stop_basis="session_low", adr_pct=4)
    path = tmp_path / "events.db"
    store = SignalStore(path)
    event = sc.observe_signal(store, candidate, m15(BASE), now())[0]
    assert event["signal"]["stop"] == 98
    assert event["signal"]["trigger"] == event["entry_level"] == 101
    assert event["candidate_signal"]["stop"] is None
    assert sc._event_signal(candidate, event)["stop"] == 98
    assert event["terms_digest"]
    later = BASE + [(101.5, 103, 100, 102)]
    store = SignalStore(path)
    assert not sc.observe_signal(store, candidate, m15(later), now(minute=15))
    assert store.get(event["id"], now(minute=15))["signal"]["stop"] == 98
    # The low falls below the frozen stop; never replace it with 97.
    assert not sc.observe_signal(store, candidate, m15(later + [(102, 103, 97, 102)]), now(minute=30))
    end = store.get(event["id"], now(minute=30))
    assert end["state"] == "invalidated" and end["signal"]["stop"] == 98


@pytest.mark.parametrize("setup,adr,eligible", [(BREAKOUT, 3, True), (BREAKOUT, 2.99, False),
                                                (EP, 2, True), (EP, 1.99, False)])
def test_adr_is_width_cap_not_stop_placement(tmp_path, setup, adr, eligible):
    # Actual entry=100, actual low=97. No invented ADR-distance stop.
    rows = [(99, 100, 97, 99.5), (99.5, 101, 98, 100.5)]
    candidate = replace(sig(setup), trigger=99, stop=None, stop_basis="session_low", adr_pct=adr)
    store = SignalStore(tmp_path / "s.db")
    results = sc.observe_signal(store, candidate, m15(rows), now())
    assert bool(results) is eligible
    event = store.events(now())[0]
    assert event["signal"]["stop"] == 97
    assert event["stop_width_valid"] is eligible


def test_trigger_bar_low_is_allowed_because_decision_is_after_completion(tmp_path):
    candidate = replace(sig(), stop=None, stop_basis="session_low", adr_pct=4)
    rows = [(99, 101, 99, 100), (100, 102, 98, 101.5)]
    store = SignalStore(tmp_path / "s.db")
    assert sc.observe_signal(store, candidate, m15(rows), now())[0]["signal"]["stop"] == 98


def test_future_low_cannot_enter_decision_and_session_low_never_rises(tmp_path):
    candidate = replace(sig(), stop=None, stop_basis="session_low", adr_pct=4)
    rows = BASE + [(101.5, 102, 90, 101.5)]
    store = SignalStore(tmp_path / "s.db")
    event = sc.observe_signal(store, candidate, m15(rows), now())[0]
    assert event["signal"]["stop"] == 98  # the 10:00 bar has not completed
    observations = sc.entry_observations(candidate, m15(rows))
    assert [o["session_low"] for o in observations] == [98, 98, 90]


def test_legacy_event_migration_retains_history_but_denies_eligibility(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('''CREATE TABLE events (id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
            state TEXT NOT NULL, trigger_at TEXT NOT NULL, observed_at TEXT NOT NULL,
            entry_level REAL NOT NULL, expires_at TEXT NOT NULL, valid_until TEXT NOT NULL, reason TEXT NOT NULL)''')
    store = SignalStore(path)
    event = sc.observe_signal(store, sig("8_raschke_holy_grail"), m15(BASE), now())[0]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE events SET signal_terms=NULL")
    assert not SignalStore(path).get(event["id"], now())["eligible"]
    assert store.history(event["id"])


def test_changed_completed_low_invalidates_event(tmp_path):
    candidate = replace(sig(), stop=None, stop_basis="session_low", adr_pct=4)
    store = SignalStore(tmp_path / "s.db")
    event = sc.observe_signal(store, candidate, m15(BASE), now())[0]
    revised = [(99, 101, 97.9, 100), BASE[1]]
    assert sc.observe_signal(store, candidate, m15(revised), now()) == []
    assert store.get(event["id"], now())["reason"] == "consumed bars revised"


@pytest.mark.parametrize("update,rule", [({"max_loss_usd": 4}, "declared_stop_loss_matches"),
    ({"stop_price": 249.8, "max_loss_usd": 4}, "stop_matches_event"),
    ({"event_id": "made_up"}, "signal_terms_valid"), ({"target_price": 270}, "target_matches_event")])
def test_fabricated_loss_stop_event_and_target_rejected(shares, account, update, rule):
    result = risk(shares.model_copy(update=update), account)
    assert not result.approved and rule in failed(result)
    assert result.final_leg_quantities == [0]
    assert result.requested_leg_quantities == [20]  # retained for review, not hidden


def test_missing_independent_source_cannot_use_caller_loss(shares, account):
    assert not risk(shares, account, terms_source=None).approved


@pytest.mark.parametrize("change", [{"stop": 251}, {"stop": float("nan")}, {"symbol": "OTHER"},
    {"setup_version": "old"}, {"event_digest": ""}, {"event_id": "wrong"},
    {"checked_at": NOW-timedelta(seconds=61)}, {"checked_at": NOW+timedelta(seconds=1)},
    {"valid_until": NOW}, {"quote_at": NOW-timedelta(seconds=61)},
    {"underlying_price": 249}, {"underlying_price": 260}])
def test_invalid_resolved_evidence_denies_eligibility(shares, account, change):
    assert not risk(shares, account, terms_source=Source(**change)).approved


@pytest.mark.parametrize("entry", [248, 249, 258])
def test_wrong_side_or_chased_limit_cannot_pass_with_false_zero_chase(shares, account, entry):
    p = shares.model_copy(update={"legs": [shares.legs[0].model_copy(update={"limit_price": entry})],
        "worst_case_loss_usd": entry * 20, "max_loss_usd": None, "already_moved_pct": 0})
    assert not risk(p, account).approved


def test_actual_limit_drives_share_size_not_old_loss_claim(shares, account):
    p = shares.model_copy(update={"legs": [shares.legs[0].model_copy(update={"limit_price": 251})],
                                 "worst_case_loss_usd": 5020, "max_loss_usd": None, "risk_usd": 26})
    result = risk(p, account)
    assert result.approved
    assert result.final_leg_quantities == [11]  # floor((26 - 1) / (251 - 248.75))
    assert result.estimated_stop_loss_usd == 24.75
    assert result.estimated_total_risk_usd == 25.75
    assert result.requested_stop_loss_usd == 45


def test_target_must_remain_beyond_executable_limit(shares, account):
    p = shares.model_copy(update={"target_price": 249})
    assert "target_beyond_entry" in failed(risk(p, account, terms_source=Source(target=249)))


@pytest.mark.parametrize("direction,entry,stop", [("long", 101.25, 100), ("short", 100, 101.25)])
def test_exact_directional_stop_math(direction, entry, stop):
    assert stop_distance(entry, stop, direction) == Fraction(5, 4)
    with pytest.raises(ValueError):
        stop_distance(stop, entry, direction)


def test_unavailable_option_stop_estimate_does_not_hide_selected_exposure(long_call, account):
    source = Source(option_exit_prices={}, option_exit_source=None)
    p = long_call.model_copy(update={"max_loss_usd": None, "sizing_mode": "selected_quantity", "risk_usd": 10})
    result = risk(p, account, terms_source=source)
    assert result.approved and result.final_leg_quantities == [3]
    assert result.computed_max_loss_usd == 450 and result.requested_max_loss_usd == 450
    assert result.estimated_stop_loss_usd is result.estimated_total_risk_usd is None
    assert result.resolved_stop_price == 225
    assert "unavailable" in result.stop_loss_basis
    assert "exposure_above_budget" in {w.code for w in result.warnings}
    assert result.warning_acknowledgement_required and not result.order_authorized
    assert not risk(p.model_copy(update={"sizing_mode": "stop_budget"}), account, terms_source=source).approved


def test_independent_conditional_option_exit_not_the_same_as_stock_stop(long_call, account):
    result = risk(long_call, account)
    assert result.estimated_stop_loss_usd == 60  # (1.50-1.30) * 100 * 3
    assert result.computed_max_loss_usd == 450
    assert "not a prediction" in result.stop_loss_basis
    assert not risk(long_call.model_copy(update={"max_loss_usd": 4}), account).approved


def test_selected_quantity_over_stop_budget_is_warning_not_cap(long_call, account):
    result = risk(long_call.model_copy(update={"sizing_mode": "selected_quantity", "risk_usd": 10}), account)
    assert result.approved and result.final_leg_quantities == [3]
    assert "stop_estimate_above_budget" in {w.code for w in result.warnings}


def test_maximum_loss_budget_is_an_explicit_optional_choice(long_call, account):
    p = long_call.model_copy(update={"sizing_mode": "maximum_loss_budget", "risk_usd": 303})
    result = risk(p, account)
    assert result.approved and result.final_leg_quantities == [2]
    assert result.computed_max_loss_usd + result.cost_reserve_usd == 303
    assert result.estimated_stop_loss_usd == 40


@pytest.mark.parametrize("update", [{"risk_usd": 100}, {"est_costs_usd": 2},
    {"sizing_mode": "selected_quantity"}, {"exit_rules": ["different management"]}])
def test_changed_proposal_terms_change_review_fingerprint(shares, account, update):
    first = risk(shares, account)
    second = risk(shares.model_copy(update=update), account)
    assert first.approved and second.approved
    assert first.terms_sha256 != second.terms_sha256
    assert not first.order_authorized and not second.order_authorized


def test_event_revision_stop_and_target_bind_review_fingerprint(shares, account):
    first = risk(shares, account)
    for source, p in [
        (Source(event_digest="revision2"), shares),
        (Source(stop=248.5), shares.model_copy(update={"stop_price": 248.5, "max_loss_usd": None})),
        (Source(target=270), shares.model_copy(update={"target_price": 270})),
    ]:
        second = risk(p, account, terms_source=source)
        assert second.approved and second.terms_sha256 != first.terms_sha256


def test_real_scanner_event_to_risk_and_fresh_stop_invalidation(tmp_path, shares, account):
    at = now()
    candidate = replace(sig(), stop=None, stop_basis="session_low", adr_pct=4)
    log = sc.ScanLog(tmp_path)
    frame = m15(BASE)
    event = sc.observe_signal(log.signals, candidate, frame, at)[0]
    source = Fake({("LEAD", "M15"): frame})
    adapter = EventRiskSource(source, log, lambda symbol: (symbol, 101.5, at))
    terms = adapter.resolve(event["id"], at)
    assert terms.stop == 98 and terms.entry_level == 101
    p = shares.model_copy(update={"event_id": event["id"], "instrument": "LEAD", "stop_price": 98,
        "max_loss_usd": None, "worst_case_loss_usd": 2030, "risk_usd": 36, "quote_as_of": at,
        "legs": [shares.legs[0].model_copy(update={"symbol": "LEAD", "limit_price": 101.5})]})
    a = replace(account, as_of=at, pnl_day=at.date(), pnl_week_start=at.date()-timedelta(days=at.weekday()))
    result = evaluate(p, a, now=at, live=False, market=MARKET.model_copy(update={"as_of": at}), terms_source=adapter)
    assert result.approved, result
    assert result.final_leg_quantities == [10] and result.estimated_stop_loss_usd == 35
    assert result.resolved_stop_price == 98 and not result.order_authorized
    assert len(log.signals.events(at)) == 1  # bound stop did not create a new candidate
    adapter.quote_source = lambda symbol: (symbol, 97, at)
    assert not evaluate(p, a, now=at, live=False, market=MARKET.model_copy(update={"as_of": at}), terms_source=adapter).approved
    assert log.signals.get(event["id"], at)["state"] == "invalidated"


def test_gap_day_low_above_old_base_is_valid_against_actual_entry(tmp_path):
    candidate = replace(sig(), stop=None, stop_basis="session_low", adr_pct=4)
    rows = [(100.5, 102, 100.25, 101), (101, 103, 100.5, 102.5)]
    store = SignalStore(tmp_path / "s.db")
    event = sc.observe_signal(store, candidate, m15(rows), now())[0]
    assert event["candidate_signal"]["trigger"] == 100
    assert event["signal"]["stop"] == 100.25 and event["entry_level"] == 102


def test_day_low_is_not_rounded_into_a_different_stop(tmp_path):
    candidate = replace(sig(), stop=None, stop_basis="session_low", adr_pct=4)
    rows = [(99, 101, 98.006, 100), BASE[1]]
    event = sc.observe_signal(SignalStore(tmp_path / "s.db"), candidate, m15(rows), now())[0]
    assert event["signal"]["stop"] == 98.006


def test_actual_entry_must_still_fit_stop_width(shares, account):
    p = shares.model_copy(update={"max_loss_usd": None})
    assert risk(p, account, terms_source=Source(max_stop_fraction=0.005)).approved
    p = p.model_copy(update={"legs": [p.legs[0].model_copy(update={"limit_price": 251})],
                              "worst_case_loss_usd": 5020})
    assert "structural_stop_width" in failed(risk(p, account, terms_source=Source(max_stop_fraction=0.005)))


def test_option_full_loss_exit_can_be_zero_premium(long_call, account):
    source = Source(option_exit_prices={"AAPL261120C00230000": 0})
    p = long_call.model_copy(update={"max_loss_usd": None, "sizing_mode": "selected_quantity"})
    result = risk(p, account, terms_source=source)
    assert result.approved and result.estimated_stop_loss_usd == result.computed_max_loss_usd == 450


def test_changed_quantity_or_limit_changes_terms_digest(shares, account):
    first = risk(shares, account)
    for qty, entry in [(19, 250), (20, 251)]:
        p = shares.model_copy(update={"legs": [shares.legs[0].model_copy(update={"qty": qty, "limit_price": entry})],
            "worst_case_loss_usd": qty * entry, "max_loss_usd": None})
        result = risk(p, account)
        assert result.approved and result.terms_sha256 != first.terms_sha256


def test_legacy_schema_cannot_skip_event_requirement(shares, account):
    assert "proposal_valid" in failed(risk(shares.model_copy(update={"schema_version": 2}), account))


def test_ep_source_still_requires_fundamentals(tmp_path):
    log = sc.ScanLog(tmp_path)
    candidate = replace(sig(EP), stop=None, stop_basis="session_low", adr_pct=4)
    frame = m15(BASE)
    event = sc.observe_signal(log.signals, candidate, frame, now())[0]
    adapter = EventRiskSource(Fake({("LEAD", "M15"): frame}), log, lambda s: (s, 101.5, now()))
    with pytest.raises(ValueError, match="revalidation"):
        adapter.resolve(event["id"], now())


def test_price_unavailability_stops_risk_adapter(tmp_path):
    log = sc.ScanLog(tmp_path)
    event = sc.observe_signal(log.signals, sig(), m15(BASE), now())[0]
    adapter = EventRiskSource(Fake({}), log, lambda s: (s, 101.5, now()))
    with pytest.raises(ValueError, match="revalidation"):
        adapter.resolve(event["id"], now())


def test_short_share_execution_remains_unsupported(shares, account):
    p = shares.model_copy(update={"event_id": "spy_short", "setup_id": "8_raschke_holy_grail",
        "setup_version": CARDS["8_raschke_holy_grail"].fingerprint(), "stop_price": 505,
        "instrument": "SPY", "max_loss_usd": None, "worst_case_loss_usd": 10000,
        "legs": [shares.legs[0].model_copy(update={"symbol": "SPY", "side": "sell", "limit_price": 500})]})
    assert "instrument_valid" in failed(risk(p, account))
