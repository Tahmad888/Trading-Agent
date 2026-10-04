"""G5a checkpoint 2, Astra's re-audit of 0d8838c: R1 and R2.

R1: an identity outcome that cannot be persisted must not leave an earlier pin eligible
(refresh attempts are committed before any request; an open attempt withholds
eligibility in every process). R2: the final volume/identity reservation follows the
stored event's actual Alpaca dependency. Synthetic Webull charts and a synthetic Alpaca
provider (labelled fixtures); real SQLite write locks from independent connections.
"""
from contextlib import closing, contextmanager
from dataclasses import replace
import json
import sqlite3

import pandas as pd
import pytest

from desk import alpaca_assets
from desk import scanner as sc
from desk.alpaca_assets import REFRESH_UNRESOLVED, SOURCE_SCOPE, IdentityStore
from desk.alpaca_source import AlpacaVolumeProvider, needs_volume_guard
from desk.alpaca_volume import AlpacaVolumeClient
from desk.calendar import latest_closed_session
from desk.playbook.filters import GateResult, MarketSize
from desk.risk_terms import EventRiskSource
from desk.signal_state import candidate_id
from desk.tickets import TicketError
from tests.alpaca_support import asset_id, asset_row
from tests.test_scanner import ET, sig
from tests.test_volume_lifecycle import DAY0, ENTRY, Desk, at
from tests.ticket_support import approve

HOLY_GRAIL = "8_raschke_holy_grail"
SHORT = 0.05      # shortened busy waits; the locks themselves are real


@pytest.fixture(autouse=True)
def template_passes(monkeypatch):
    monkeypatch.setattr(sc, "trend_template", lambda f, spy: GateResult({"fixture template": True}))


@pytest.fixture
def short_waits(monkeypatch):
    monkeypatch.setattr(alpaca_assets, "STORE_TIMEOUT_SECONDS", SHORT)
    monkeypatch.setattr("desk.alpaca_source.GUARD_TIMEOUT_SECONDS", SHORT)


class Crash(BaseException):
    """A process dying mid-step: nothing after it runs, no handler records anything."""


def healthy_assets():
    return [asset_row(s) for s in ("LEAD", "SPY", "QQQ", "IWM")]


def frame(desk, symbol="LEAD"):
    f = desk.frames[symbol, "D"].copy()
    f.attrs["security_metadata"] = desk.metadata[symbol]
    return f


def identities(desk, src, symbols=("LEAD",)):
    return src.decision_volume.identities({s: frame(desk, s) for s in symbols},
                                          latest_closed_session(desk.clock.at))


def cached(src, signal, desk):
    return sc.volume_status(src, signal, desk.clock.at, refresh=False)


def fence(desk, event_id, source=None):
    with EventRiskSource(source or desk.source(), desk.log, lambda s: (s, desk.price, desk.clock.at)) \
            .held_event(event_id) as status:
        return status(desk.clock.at)


def attempts(desk):
    with closing(sqlite3.connect(desk.tmp / "volume.sqlite")) as db:
        return db.execute("SELECT attempt, kind, completed_at IS NOT NULL, outcome FROM identity_attempts "
                          "ORDER BY attempt").fetchall()


class Lock:
    """BEGIN IMMEDIATE on the volume/identity file from an independent connection."""

    def __init__(self, desk):
        self.db = sqlite3.connect(desk.tmp / "volume.sqlite", isolation_level=None)

    def take(self):
        self.db.execute("BEGIN IMMEDIATE")

    def release(self):
        if self.db.in_transaction:
            self.db.execute("ROLLBACK")

    def close(self):
        self.release()
        self.db.close()


@contextmanager
def lock_after_registration(monkeypatch, lock):
    """The attempt commits; then another writer holds the file until the block ends."""
    real = IdentityStore.register_attempt

    def wrapped(self, *a, **k):
        out = real(self, *a, **k)
        lock.take()
        return out
    with monkeypatch.context() as m:
        m.setattr(IdentityStore, "register_attempt", wrapped)
        try:
            yield
        finally:
            lock.close()


# ------------------------------------------------------------------- R1 ----

def test_r1_a_lock_preventing_registration_sends_nothing_and_withholds_this_run(tmp_path, short_waits):
    desk = Desk(tmp_path)
    _, armed = desk.arm()
    desk.clock.at = at(10)
    src = desk.source()
    desk.sim.status = [500]                     # would be the reply, if anything were sent
    before, rows = desk.sim.calls(), attempts(desk)
    lock = Lock(desk)
    lock.take()
    try:
        seen = identities(desk, src)
    finally:
        lock.close()
    assert seen == {"LEAD": "IDENTITY_UNAVAILABLE:IDENTITY_STORE_UNAVAILABLE"}
    assert desk.sim.calls() == before and desk.sim.status == [500]    # no request at all
    assert attempts(desk) == rows                                       # nothing was registered
    assert cached(src, armed[0], desk) == ("UNAVAILABLE", "IDENTITY_STORE_UNAVAILABLE")
    rec = sc.intraday_scan(src, armed, desk.clock.at, store=desk.log.signals)
    assert not rec.triggered and "IDENTITY_STORE_UNAVAILABLE" in rec.skipped["LEAD"]
    # Nothing was sent, so nothing new was observed: a new process reads the earlier confirmation.
    assert cached(desk.source(), armed[0], desk) == ("OK", None)


@pytest.mark.parametrize("reply", ["asset_500", "not_found", "healthy_list"])
def test_r1_an_outcome_that_cannot_be_committed_stays_unavailable_after_restart(tmp_path, monkeypatch,
                                                                                  short_waits, reply):
    desk = Desk(tmp_path)
    _, armed = desk.arm()
    desk.clock.at = at(10)
    if reply == "asset_500":
        desk.sim.status = [500]
    elif reply == "not_found":
        desk.sim.assets = [asset_row(s) for s in ("SPY", "QQQ", "IWM")]
    src = desk.source()
    lock = Lock(desk)
    before = desk.sim.calls("assets")
    with lock_after_registration(monkeypatch, lock):
        seen = identities(desk, src)
    assert desk.sim.calls("assets") == before + 1                       # the request was sent
    assert "IDENTITY_STORE_UNAVAILABLE" in seen["LEAD"]
    assert attempts(desk)[-1][1:3] == ("FETCH", False)                  # the attempt stays open
    sent = desk.sim.calls()
    assert cached(src, armed[0], desk) == ("UNAVAILABLE", "IDENTITY_STORE_UNAVAILABLE")
    desk.restart()
    assert cached(desk.source(), armed[0], desk) == ("UNAVAILABLE", REFRESH_UNRESOLVED)
    rec = sc.intraday_scan(src, armed, desk.clock.at, store=desk.log.signals)
    assert not rec.triggered
    assert desk.sim.calls() == sent                                     # cache-only checks: zero requests


@pytest.mark.parametrize("point", ["after_registration", "after_response"])
def test_r1_a_crash_between_registration_response_and_completion_leaves_it_unresolved(tmp_path, monkeypatch,
                                                                                         point):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    desk.advance()
    def crash(self, *a, **k):
        raise Crash()
    with monkeypatch.context() as m:
        if point == "after_registration":
            m.setattr(AlpacaVolumeClient, "fetch_assets", crash)
        else:
            m.setattr(IdentityStore, "resolve", crash)          # the list arrived; nothing recorded
        with pytest.raises(Crash):
            identities(desk, desk.source())
    assert attempts(desk)[-1][1:3] == ("FETCH", False)
    desk.restart()
    sent = desk.sim.calls()
    assert cached(desk.source(), signal, desk) == ("UNAVAILABLE", REFRESH_UNRESOLVED)
    assert fence(desk, event["id"]).eligible is False
    assert desk.sim.calls() == sent
    assert desk.tickets.get(tid, v, now=desk.clock.at)["state"] == "approved"


def test_r1_a_stale_list_cannot_clear_a_newer_open_or_failed_attempt(tmp_path):
    desk = Desk(tmp_path)
    _, armed = desk.arm()
    desk.advance()
    stale = desk.source()
    assert isinstance(identities(desk, stale)["LEAD"], alpaca_assets.IdentityRecord)   # list held in memory
    store = IdentityStore(desk.tmp / "volume.sqlite")
    # Another process registers a newer fetch and has not recorded its outcome yet.
    newer = store.register_attempt(["LEAD"], desk.clock.at)
    assert cached(desk.source(), armed[0], desk) == ("UNAVAILABLE", REFRESH_UNRESOLVED)
    desk.advance()
    assert identities(desk, stale) == {"LEAD": "IDENTITY_STALE_ASSET_LIST"}
    assert cached(desk.source(), armed[0], desk) == ("UNAVAILABLE", REFRESH_UNRESOLVED)
    # That attempt then fails; the old list still cannot record success over it.
    store.record_source_failure("HTTP_FAILURE", desk.clock.at, attempt=newer)
    assert identities(desk, stale) == {"LEAD": "IDENTITY_STALE_ASSET_LIST"}
    assert cached(desk.source(), armed[0], desk) == ("UNAVAILABLE", "IDENTITY_SOURCE_FAILED:HTTP_FAILURE")
    # Only a newly fetched list recovers.
    desk.advance()
    assert isinstance(identities(desk, desk.source())["LEAD"], alpaca_assets.IdentityRecord)
    assert cached(desk.source(), armed[0], desk) == ("OK", None)


def test_r1_a_stale_list_cannot_pin_an_older_identity_over_a_newer_one(tmp_path):
    desk = Desk(tmp_path)
    _, armed = desk.arm()
    desk.advance()
    stale = desk.source()
    identities(desk, stale)
    desk.advance()
    desk.sim.assets = [asset_row("LEAD", id=asset_id("LEAD", "relisted")), *healthy_assets()[1:]]
    assert identities(desk, desk.source())["LEAD"].version == 2
    desk.advance()
    assert identities(desk, stale) == {"LEAD": "IDENTITY_STALE_ASSET_LIST"}
    store = IdentityStore(desk.tmp / "volume.sqlite")
    assert store.latest("LEAD").alpaca_asset_id == asset_id("LEAD", "relisted")
    assert len(store.history("LEAD")) == 2
    assert cached(desk.source(), armed[0], desk) == ("CHANGED", "IDENTITY_CHANGED")


def test_r1_later_unchanged_recovery_keeps_terms_and_never_revives_a_spent_approval(tmp_path, monkeypatch,
                                                                                     short_waits):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    terms = desk.log.signals.get(event["id"], desk.clock.at)["terms_digest"]
    desk.advance()
    lock = Lock(desk)
    with lock_after_registration(monkeypatch, lock):
        identities(desk, desk.source())
    assert fence(desk, event["id"]).eligible is False
    with pytest.raises(TicketError):
        desk.tickets.consume(tid, v, request_id="r1-blocked", inputs=desk.adapters(desk.source(budget=0)),
                             now=desk.clock.at)
    assert desk.tickets.get(tid, v, now=desk.clock.at)["state"] == "approved"
    # A later, independently successful fetch confirms the same pin.
    desk.advance()
    record = identities(desk, desk.source())["LEAD"]
    assert record.version == 1 and len(IdentityStore(desk.tmp / "volume.sqlite").history("LEAD")) == 1
    assert cached(desk.source(), signal, desk) == ("OK", None)
    assert desk.log.signals.get(event["id"], desk.clock.at)["terms_digest"] == terms
    _, again = desk.arm()
    assert candidate_id(again[0], ENTRY) == candidate_id(signal, ENTRY)
    assert desk.tickets.consume(tid, v, request_id="r1-ok", inputs=desk.adapters(),
                                now=desk.clock.at)["replay"] is False
    with pytest.raises(TicketError, match="already consumed"):
        desk.tickets.consume(tid, v, request_id="r1-again", inputs=desk.adapters(), now=desk.clock.at)


def test_r1_a_changed_identity_after_an_unresolved_attempt_still_requalifies(tmp_path, monkeypatch, short_waits):
    desk = Desk(tmp_path)
    _, armed = desk.arm()
    desk.advance()
    lock = Lock(desk)
    with lock_after_registration(monkeypatch, lock):
        identities(desk, desk.source())
    desk.advance()
    desk.sim.assets = [asset_row("LEAD", id=asset_id("LEAD", "relisted")), *healthy_assets()[1:]]
    assert identities(desk, desk.source())["LEAD"].version == 2
    assert cached(desk.source(), armed[0], desk) == ("CHANGED", "IDENTITY_CHANGED")


def price_only(desk):
    """A synthetic Holy Grail candidate with no Alpaca evidence (as in Astra's probe)."""
    signal = replace(sig(HOLY_GRAIL, trigger=148.74, stop=147), as_of=pd.Timestamp(DAY0, tz=ET))
    desk.log.add_armed(ENTRY, MarketSize.FULL, [signal])
    return signal


def test_r1_an_open_attempt_spares_recorded_healthy_tickers_scope_and_price_only_setups(tmp_path, monkeypatch,
                                                                                       short_waits):
    desk = Desk(tmp_path, symbols=("LEAD", "OTHER"))
    _, vcp = desk.arm(("LEAD", "OTHER"))
    by = {s.symbol: s for s in vcp}
    desk.advance()
    # A recorded per-ticker failure blocks only that ticker.
    desk.sim.assets = [asset_row(s) for s in ("OTHER", "SPY", "QQQ", "IWM")]
    identities(desk, desk.source(), ("LEAD", "OTHER"))
    assert cached(desk.source(), by["LEAD"], desk) == ("UNAVAILABLE", "ASSET_NOT_FOUND")
    assert cached(desk.source(), by["OTHER"], desk) == ("OK", None)
    # An unrecorded shared-source outcome withholds every identity-dependent input...
    desk.advance()
    lock = Lock(desk)
    with lock_after_registration(monkeypatch, lock):
        identities(desk, desk.source(), ("OTHER",))
    assert cached(desk.source(), by["OTHER"], desk) == ("UNAVAILABLE", REFRESH_UNRESOLVED)
    # ...but never a price-only setup: it triggers with the attempt still open.
    hg = price_only(desk)
    assert cached(desk.source(), hg, desk) == ("OK", None)
    rec = desk.trigger([hg])
    assert [t["symbol"] for t in rec.triggered] == ["LEAD"]


@pytest.mark.parametrize("status,code", [(401, "AUTH_OR_ENTITLEMENT_FAILURE"), (403, "AUTH_OR_ENTITLEMENT_FAILURE"),
                                         (429, "RATE_LIMITED")])
def test_r1_stop_replies_close_the_attempt_and_keep_the_stop(tmp_path, status, code):
    desk = Desk(tmp_path)
    _, armed = desk.arm()
    desk.advance()
    desk.sim.status = [status]
    src = desk.source()
    assert identities(desk, src) == {"LEAD": "IDENTITY_UNAVAILABLE:" + code}
    assert attempts(desk)[-1][1:] == ("FETCH", True, "SOURCE_FAILED:" + code)
    with closing(sqlite3.connect(desk.tmp / "volume.sqlite")) as db:
        assert db.execute("SELECT code FROM events WHERE state='STOP'").fetchall() == [(code,)]
    rows, sent = attempts(desk), desk.sim.calls()
    # The stopped run registers nothing more and sends nothing more.
    src.decision_volume._assets = src.decision_volume._asset_error = None
    assert identities(desk, src) == {"LEAD": "IDENTITY_UNAVAILABLE:RUN_STOPPED_AFTER_" + code}
    assert attempts(desk) == rows and desk.sim.calls() == sent
    assert cached(desk.source(), armed[0], desk) == ("UNAVAILABLE", "IDENTITY_SOURCE_FAILED:" + code)


def test_r1_the_final_guard_and_a_spent_budget_register_nothing_and_send_nothing(tmp_path):
    desk = Desk(tmp_path)
    signal, *_ = desk.approved()
    desk.advance()
    src = desk.source()
    rows, sent = attempts(desk), desk.sim.calls()
    with src.decision_volume.held():
        assert identities(desk, src) == {"LEAD": "IDENTITY_UNAVAILABLE:NETWORK_FORBIDDEN_IN_FINAL_GUARD"}
        assert cached(src, signal, desk) == ("OK", None)
    spent = desk.source(budget=0)
    assert identities(desk, spent) == {"LEAD": "IDENTITY_UNAVAILABLE:REQUEST_BUDGET_EXHAUSTED"}
    assert attempts(desk) == rows and desk.sim.calls() == sent


def test_r1_migration_adds_the_attempt_column_and_keeps_legacy_rows(tmp_path):
    desk = Desk(tmp_path)
    _, armed = desk.arm()
    path = desk.tmp / "volume.sqlite"
    with closing(sqlite3.connect(path)) as db, db:               # a 0d8838c file: no attempts, no column
        db.execute("DROP TABLE identity_attempts")
        db.execute("CREATE TABLE h AS SELECT sequence,scope,state,reason,version,mapping_digest,"
                   "asset_list_digest,received_at FROM identity_health")
        db.execute("DROP TABLE identity_health")
        db.execute("CREATE TABLE identity_health(sequence INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, "
                   "state TEXT NOT NULL CHECK(state IN ('OK','FAILED')), reason TEXT, version INTEGER, "
                   "mapping_digest TEXT, asset_list_digest TEXT, received_at TEXT NOT NULL)")
        db.execute("INSERT INTO identity_health SELECT * FROM h")
        db.execute("DROP TABLE h")
        count = db.execute("SELECT COUNT(*) FROM identity_health").fetchone()[0]
    IdentityStore(path)
    with closing(sqlite3.connect(path)) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(identity_health)")}
        assert "attempt" in columns
        assert db.execute("SELECT COUNT(*), COUNT(attempt) FROM identity_health").fetchone() == (count, 0)
    assert cached(desk.source(), armed[0], desk) == ("OK", None)      # legacy OK rows keep their meaning
    desk.advance()
    identities(desk, desk.source())
    assert cached(desk.source(), armed[0], desk) == ("OK", None)


# ------------------------------------------------------------------- R2 ----

@pytest.fixture(params=["delete", "wal"])
def journal(request):
    return request.param


def set_journal(desk, mode):
    with closing(sqlite3.connect(desk.tmp / "volume.sqlite")) as db:
        assert db.execute(f"PRAGMA journal_mode={mode}").fetchone()[0] == mode


def test_r2_price_only_prepare_approve_and_consume_while_an_unrelated_writer_holds_the_file(
        tmp_path, short_waits, journal):
    desk = Desk(tmp_path)
    desk.arm()                                      # creates the volume/identity file through production
    set_journal(desk, journal)
    hg = price_only(desk)
    rec = desk.trigger([hg])
    assert len(rec.triggered) == 1
    event_id = rec.triggered[0]["event_id"]
    lock = Lock(desk)
    lock.take()
    try:
        tid, v = desk.ticket(event_id)
        assert desk.tickets.get(tid, v, now=desk.clock.at)["state"] == "pending"
        approve(desk.tickets, tid, v, desk.adapters(), now=desk.clock.at)
        desk.advance()
        desk.snapshot()
        # The manual stop still refuses a price-only ticket, unspent.
        revision = desk.state.set_manual_halt("fixture-account", True, actor="fixture:automated-test",
                                              reason="fixture", now=desk.clock.at)
        with pytest.raises(TicketError):
            desk.tickets.consume(tid, v, request_id="r2-halt", inputs=desk.adapters(), now=desk.clock.at)
        desk.state.set_manual_halt("fixture-account", False, expected_revision=revision,
                                   actor="fixture:automated-test", reason="fixture", now=desk.clock.at)
        assert desk.tickets.get(tid, v, now=desk.clock.at)["state"] == "approved"
        result = desk.tickets.consume(tid, v, request_id="r2-price-only", inputs=desk.adapters(),
                                      now=desk.clock.at)
        assert result["replay"] is False and result["broker_action"] == "none"
        with pytest.raises(TicketError, match="already consumed"):
            desk.tickets.consume(tid, v, request_id="r2-again", inputs=desk.adapters(), now=desk.clock.at)
        assert lock.db.in_transaction                 # the unrelated writer held the slot throughout
    finally:
        lock.close()


def before_guard(monkeypatch, action):
    real = EventRiskSource.held_event

    def wrapped(self, event_id):
        action()
        return real(self, event_id)
    monkeypatch.setattr(EventRiskSource, "held_event", wrapped)


def test_r2_a_volume_dependent_event_still_refuses_when_the_reservation_is_busy(tmp_path, monkeypatch,
                                                                                short_waits, journal):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    set_journal(desk, journal)
    desk.advance()
    adapters = desk.adapters()
    lock = Lock(desk)
    with monkeypatch.context() as m:
        before_guard(m, lock.take)                    # after the recheck, before the guard
        try:
            with pytest.raises(TicketError, match="Final check unavailable"):
                desk.tickets.consume(tid, v, request_id="r2-vcp", inputs=adapters, now=desk.clock.at)
        finally:
            lock.close()
    assert desk.tickets.get(tid, v, now=desk.clock.at)["state"] == "approved"
    assert desk.tickets.consume(tid, v, request_id="r2-vcp-ok", inputs=desk.adapters(),
                                now=desk.clock.at)["replay"] is False


def tamper(desk, event_id, change):
    """Edit the stored candidate payload directly (simulates a corrupted or legacy row)."""
    with closing(sqlite3.connect(desk.log.signals.path)) as db, db:
        cid, payload = db.execute("SELECT c.id, c.payload FROM events e JOIN candidates c ON c.id=e.candidate_id "
                                  "WHERE e.id=?", (event_id,)).fetchone()
        data = json.loads(payload)
        change(data)
        db.execute("UPDATE candidates SET payload=? WHERE id=?", (json.dumps(data), cid))


@pytest.mark.parametrize("case", ["evidence_added", "setup_mismatch"])
def test_r2_a_price_only_row_with_dependency_evidence_or_inconsistency_takes_the_guard(tmp_path, monkeypatch,
                                                                                         short_waits, case):
    desk = Desk(tmp_path)
    desk.arm()
    hg = price_only(desk)
    event_id = desk.trigger([hg]).triggered[0]["event_id"]
    if case == "evidence_added":
        tamper(desk, event_id, lambda d: d.update(volume_evidence={"consumer": "fixture"}))
    else:
        tamper(desk, event_id, lambda d: d.update(setup_id="2_minervini_vcp"))
    lock = Lock(desk)
    lock.take()
    try:
        with pytest.raises(sqlite3.OperationalError):     # the reservation was required and is busy
            fence(desk, event_id)
    finally:
        lock.close()
    status = fence(desk, event_id)                         # free: volume_status / the view decide
    assert status.eligible is False


def test_r2_source_changes_and_legacy_volume_rows_still_refuse(tmp_path):
    desk = Desk(tmp_path)
    signal, event, tid, v = desk.approved()
    desk.advance()
    # Alpaca evidence with the provider removed: CHANGED, refused (no reservation to take).
    plain = desk.source().source
    with EventRiskSource(plain, desk.log, lambda s: (s, desk.price, desk.clock.at)).held_event(event["id"]) as st:
        assert st(desk.clock.at).eligible is False
    # A volume setup saved without evidence while Alpaca is configured: CHANGED, refused.
    tamper(desk, event["id"], lambda d: d.update(volume_evidence=None))
    assert sc.volume_status(desk.source(), signal.__class__(**{**signal.__dict__, "volume_evidence": None}),
                            desk.clock.at, refresh=False) == ("CHANGED", "VOLUME_SOURCE_CHANGED")
    assert fence(desk, event["id"]).eligible is False
    # Malformed evidence: UNAVAILABLE, refused.
    tamper(desk, event["id"], lambda d: d.update(volume_evidence={"identity": "garbage"}))
    assert fence(desk, event["id"]).eligible is False
    assert desk.tickets.get(tid, v, now=desk.clock.at)["state"] == "approved"


@pytest.mark.parametrize("row,needed", [
    ({"setup_id": HOLY_GRAIL, "candidate_signal": json.dumps({"setup_id": HOLY_GRAIL}), "signal_terms": None}, False),
    ({"setup_id": HOLY_GRAIL, "candidate_signal": json.dumps({"setup_id": HOLY_GRAIL, "volume_evidence": None}),
      "signal_terms": json.dumps({"setup_id": HOLY_GRAIL, "volume_evidence": None})}, False),
    ({"setup_id": HOLY_GRAIL, "candidate_signal": json.dumps({"setup_id": HOLY_GRAIL}),
      "signal_terms": json.dumps({"setup_id": HOLY_GRAIL, "volume_evidence": {"x": 1}})}, True),
    ({"setup_id": HOLY_GRAIL, "candidate_signal": json.dumps({"setup_id": HOLY_GRAIL, "volume_evidence": "x"}),
      "signal_terms": None}, True),
    ({"setup_id": "2_minervini_vcp", "candidate_signal": json.dumps({"setup_id": "2_minervini_vcp"}),
      "signal_terms": None}, True),
    ({"setup_id": "5_qullamaggie_episodic_pivot",
      "candidate_signal": json.dumps({"setup_id": "5_qullamaggie_episodic_pivot"}), "signal_terms": None}, True),
    ({"setup_id": HOLY_GRAIL, "candidate_signal": json.dumps({"setup_id": "1_qullamaggie_breakout"}),
      "signal_terms": None}, True),
    ({"setup_id": "not-a-setup", "candidate_signal": json.dumps({"setup_id": "not-a-setup"}),
      "signal_terms": None}, True),
    ({"setup_id": HOLY_GRAIL, "candidate_signal": "{not json", "signal_terms": None}, True),
    ({"setup_id": HOLY_GRAIL, "candidate_signal": None, "signal_terms": None}, True),
    ({"candidate_signal": json.dumps({"setup_id": HOLY_GRAIL})}, True),
])
def test_r2_the_dependency_rule_reads_only_the_stored_row_and_fails_toward_the_guard(row, needed):
    assert needs_volume_guard(row) is needed
