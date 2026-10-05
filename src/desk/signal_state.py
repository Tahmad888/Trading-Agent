"""Durable signal observations, not approvals or orders. See checkpoint 07.

SQLite transactions own event identity and transitions. Reads always take a clock;
no persisted boolean authorizes a trade. Plan B: retain history, deny eligibility.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pandas as pd

from desk.calendar import clock, latest_closed_session, session
from desk.data_basis import dependency_terms
from desk.playbook.cards import CARDS
from desk.playbook.triggers import Signal


class SignalStateError(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def signal_payload(sig):
    return {**asdict(sig), "as_of": sig.as_of.isoformat()}


def restore_signal(payload):
    return Signal(**{**payload, "as_of": pd.Timestamp(payload["as_of"]),
                     "setup_version": payload.get("setup_version") or "legacy-unversioned"})


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(value):
    return clock(value).tz_convert("UTC").isoformat()


def candidate_id(sig, day):
    if sig.setup_id not in CARDS or sig.direction not in CARDS[sig.setup_id].directions:
        raise SignalStateError("Unknown setup or unsupported direction")
    if isinstance(sig.price_basis, dict) and sig.price_basis.get("method") == "webull-discovery-v1":
        # G5a checkpoint 3: discovery-scoped history is not setup evidence.
        raise SignalStateError("Discovery-scoped price evidence cannot arm a signal")
    clock(sig.as_of)
    # Rationale text may change without changing executable terms.
    terms = signal_payload(sig)
    terms.pop("saw")
    # Volume qualification (G5a): only its dependency (used values, definitions,
    # identity, rule, result) defines terms; receipts and snapshot digests do not.
    # Signals without it keep their earlier identities.
    volume = terms.pop("volume_evidence", None)
    if volume is not None:
        terms["volume_evidence"] = dependency_terms(volume)
    basis = terms.get("price_basis")
    if isinstance(basis,dict) and basis.get("method") == "webull-history-v1":
        # Re-fetching identical vendor history does not create new setup terms.
        # The full original receipt remains in payload; history/identity stay hashed.
        terms["price_basis"] = {k:v for k,v in basis.items() if k != "verified_at"}
    return _hash([day.isoformat(), sig.setup_version, terms])


class SignalStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS armed (
                    day TEXT PRIMARY KEY, market TEXT, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS armed_history (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
                    market TEXT, payload TEXT NOT NULL, observed_at TEXT);
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY, day TEXT NOT NULL, symbol TEXT NOT NULL,
                    setup_id TEXT NOT NULL, direction TEXT NOT NULL,
                    payload TEXT NOT NULL, card_version TEXT NOT NULL,
                    expires_at TEXT NOT NULL, blocked TEXT, suspended TEXT,
                    active_event TEXT, last_bar TEXT, reset_needed INTEGER NOT NULL DEFAULT 0,
                    start_after TEXT, reset_after TEXT, checked_at TEXT);
                CREATE TABLE IF NOT EXISTS observations (
                    candidate_id TEXT NOT NULL, bar_end TEXT NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(candidate_id, bar_end));
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    state TEXT NOT NULL, trigger_at TEXT NOT NULL, observed_at TEXT NOT NULL,
                    entry_level REAL NOT NULL, expires_at TEXT NOT NULL,
                    valid_until TEXT NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revision_rebuilds (
                    candidate_id TEXT PRIMARY KEY, day TEXT NOT NULL,
                    symbol TEXT NOT NULL, setup_id TEXT NOT NULL, direction TEXT NOT NULL,
                    requested_at TEXT NOT NULL, status TEXT NOT NULL,
                    attempted_at TEXT, reason TEXT NOT NULL, replacement_id TEXT);
                CREATE TABLE IF NOT EXISTS transitions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
                    state TEXT NOT NULL, at TEXT NOT NULL, reason TEXT NOT NULL);
                -- Live-run package 2: one unfinished-close-preparation job per completed
                -- source session; committed with the target's armed list in one transaction.
                CREATE TABLE IF NOT EXISTS close_jobs (
                    source_session TEXT PRIMARY KEY, target_session TEXT NOT NULL,
                    status TEXT NOT NULL, coverage TEXT, universe TEXT NOT NULL, discovery TEXT NOT NULL,
                    outcomes TEXT NOT NULL, market TEXT, published INTEGER NOT NULL DEFAULT 0,
                    volume_pending TEXT NOT NULL DEFAULT '[]', attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, last_attempt_at TEXT, next_attempt_at TEXT,
                    claim_token TEXT, claim_until TEXT, blocked_reason TEXT, finished_at TEXT,
                    history TEXT NOT NULL DEFAULT '[]');
                -- Bars ending at or before start_after never feed this candidate (no replay
                -- of crossings that happened before a mid-session publication).
                CREATE TABLE IF NOT EXISTS armed_floors (
                    day TEXT NOT NULL, candidate_id TEXT NOT NULL, start_after TEXT NOT NULL,
                    PRIMARY KEY(day, candidate_id));
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {r[1] for r in db.execute('PRAGMA table_info(events)')}
            if 'signal_terms' not in columns:
                db.execute('ALTER TABLE events ADD COLUMN signal_terms TEXT')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_armed(self, day, market, payload, *, append=False, observed_at=None):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT market,payload FROM armed WHERE day=?", (day.isoformat(),)).fetchone()
            if append and old:
                market = old["market"] or market
                payload = json.loads(old["payload"]) + payload
            # No event-state migration from old scan logs. Deduplicate candidate terms.
            unique = {candidate_id(restore_signal(p), day): p for p in payload}
            encoded = _json(list(unique.values()))
            if old is None or old["payload"] != encoded or old["market"] != market:
                db.execute("INSERT INTO armed_history(day,market,payload,observed_at) VALUES (?,?,?,?)",
                           (day.isoformat(), market, encoded, _time(observed_at) if observed_at else None))
            db.execute("INSERT OR REPLACE INTO armed VALUES (?,?,?)", (day.isoformat(), market, encoded))

    # ---- close-preparation jobs (live-run package 2) ---------------------------------
    @staticmethod
    def _job(row):
        if row is None:
            return None
        job = dict(row)
        for key in ("universe", "discovery", "outcomes", "volume_pending", "history"):
            job[key] = json.loads(job[key])
        return job

    def close_job(self, source_session):
        with self._db() as db:
            return self._job(db.execute("SELECT * FROM close_jobs WHERE source_session=?",
                                        (source_session.isoformat(),)).fetchone())

    def close_jobs(self):
        with self._db() as db:
            return [self._job(r) for r in db.execute("SELECT * FROM close_jobs ORDER BY source_session")]

    def create_close_job(self, source_session, target_session, universe, discovery, now):
        """Freeze the job's sessions and universe once; a later call returns the original."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO close_jobs(source_session,target_session,status,universe,discovery,"
                       "outcomes,created_at,next_attempt_at) VALUES (?,?,?,?,?,?,?,?)",
                       (source_session.isoformat(), target_session.isoformat(), "PENDING", _json(universe),
                        _json(discovery), _json({}), _time(now), _time(now)))
            return self._job(db.execute("SELECT * FROM close_jobs WHERE source_session=?",
                                        (source_session.isoformat(),)).fetchone())

    def claim_close_job(self, source_session, now, token, lease_until):
        """Atomically claim one attempt; returns (claimed, reason, job). No request is made here."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._job(db.execute("SELECT * FROM close_jobs WHERE source_session=?",
                                       (source_session.isoformat(),)).fetchone())
            if job is None:
                return False, "NO_JOB", None
            if job["status"] not in ("PENDING", "PARTIAL"):
                return False, job["status"], job
            stamp = clock(now)
            if job["claim_until"] and stamp < clock(job["claim_until"]):
                return False, "CLAIMED_BY_ANOTHER_ATTEMPT", job
            if job["next_attempt_at"] and stamp < clock(job["next_attempt_at"]):
                return False, "NOT_DUE", job
            db.execute("UPDATE close_jobs SET claim_token=?, claim_until=?, attempts=attempts+1, last_attempt_at=? "
                       "WHERE source_session=?", (token, _time(lease_until), _time(now), source_session.isoformat()))
            job.update(claim_token=token, attempts=job["attempts"] + 1)
            return True, "CLAIMED", job

    def commit_close_job(self, source_session, token, now, *, outcomes, market, payload, volume_pending,
                         status, coverage, next_attempt_at, blocked_reason, floor_after, record,
                         decision_clock=None):
        """Publish an attempt and its job progress together; False if the claim was lost.

        Candidates are appended to the target's armed list (deduplicated by candidate ID),
        never replacing committed candidates; the market is set only if none is
        committed. A lost or superseded claim commits nothing. Expiry commits only
        terminal job progress. The final clock is read after all SQLite lock waits;
        explicit clocks without a callback support deterministic offline replays.
        ``record`` receives this attempt's final time/status even if another attempt
        changes the job after this transaction returns.
        """
        with self._db() as db:
            db.execute("BEGIN EXCLUSIVE")  # rollback-journal readers cannot delay COMMIT after the clock
            job = self._job(db.execute("SELECT * FROM close_jobs WHERE source_session=?",
                                       (source_session.isoformat(),)).fetchone())
            if job is None or job["claim_token"] != token:
                return False
            day = date.fromisoformat(job["target_session"])
            final = clock(decision_clock() if decision_clock else now)
            if final < clock(now):
                raise SignalStateError("Close publication clock precedes the evaluation clock")
            record.update(at=final.isoformat(), status=status)
            expiry = ("TARGET_SESSION_ENDED" if final >= session(day)[1] else
                      "SOURCE_SESSION_NO_LONGER_LATEST" if latest_closed_session(final) != source_session else None)
            if expiry:
                record.update(status="EXPIRED", reason=expiry, prepared=[],
                              discarded_prepared=record.get("prepared", []))
                db.execute("UPDATE close_jobs SET status='EXPIRED', finished_at=?, claim_token=NULL, "
                           "claim_until=NULL, next_attempt_at=NULL, history=? WHERE source_session=?",
                           (_time(final), _json((job["history"] + [record])[-50:]), source_session.isoformat()))
                return True
            if next_attempt_at is not None:
                next_attempt_at = final + (clock(next_attempt_at) - clock(now))
            if final >= session(day)[0]:
                floor_after = max(final, clock(floor_after)) if floor_after is not None else final
            now = final
            published = job["published"]
            if market is not None:
                old = db.execute("SELECT market,payload FROM armed WHERE day=?", (day.isoformat(),)).fetchone()
                current = json.loads(old["payload"]) if old else []
                existing_ids = {candidate_id(restore_signal(p), day) for p in current}
                merged_market = (old["market"] if old and old["market"] else None) or market
                unique = {candidate_id(restore_signal(p), day): p for p in current + list(payload)}
                encoded = _json(list(unique.values()))
                if old is None or old["payload"] != encoded or old["market"] != merged_market:
                    db.execute("INSERT INTO armed_history(day,market,payload,observed_at) VALUES (?,?,?,?)",
                               (day.isoformat(), merged_market, encoded, _time(now)))
                db.execute("INSERT OR REPLACE INTO armed VALUES (?,?,?)", (day.isoformat(), merged_market, encoded))
                if floor_after is not None:
                    db.executemany("INSERT OR IGNORE INTO armed_floors VALUES (?,?,?)",
                                   [(day.isoformat(), candidate_id(restore_signal(p), day), _time(floor_after))
                                    for p in payload if candidate_id(restore_signal(p), day) not in existing_ids])
                published = 1
            merged = {**job["outcomes"], **outcomes}
            history = (job["history"] + [record])[-50:]  # bounded attempt history
            db.execute("UPDATE close_jobs SET status=?, coverage=?, outcomes=?, market=COALESCE(market,?), "
                       "published=?, volume_pending=?, next_attempt_at=?, blocked_reason=?, claim_token=NULL, "
                       "claim_until=NULL, finished_at=?, history=? WHERE source_session=?",
                       (status, coverage, _json(merged), market, published,
                        _json(sorted(set(job["volume_pending"]) | set(volume_pending))),
                        _time(next_attempt_at) if next_attempt_at else None, blocked_reason,
                        _time(now) if status in ("FINISHED", "FAILED", "EXPIRED") else None, _json(history),
                        source_session.isoformat()))
            return True

    def release_close_job(self, source_session, token, now, next_attempt_at, record):
        """An attempt that raised: free its claim, keep everything committed before."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._job(db.execute("SELECT * FROM close_jobs WHERE source_session=?",
                                       (source_session.isoformat(),)).fetchone())
            if job is None or job["claim_token"] != token:
                return False
            db.execute("UPDATE close_jobs SET claim_token=NULL, claim_until=NULL, next_attempt_at=?, history=? "
                       "WHERE source_session=?", (_time(next_attempt_at), _json((job["history"] + [record])[-50:]),
                                                  source_session.isoformat()))
            return True

    def end_close_job(self, source_session, now, status, reason):
        """Terminal state set without an attempt (EXPIRED); only an open job changes."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._job(db.execute("SELECT * FROM close_jobs WHERE source_session=?",
                                       (source_session.isoformat(),)).fetchone())
            if job is None or job["status"] not in ("PENDING", "PARTIAL", "BLOCKED"):
                return job
            record = {"at": _time(now), "status": status, "reason": reason}
            db.execute("UPDATE close_jobs SET status=?, finished_at=?, claim_token=NULL, claim_until=NULL, history=? "
                       "WHERE source_session=?", (status, _time(now), _json((job["history"] + [record])[-50:]),
                                                  source_session.isoformat()))
            return self._job(db.execute("SELECT * FROM close_jobs WHERE source_session=?",
                                        (source_session.isoformat(),)).fetchone())

    def resume_close_job(self, source_session, now, actor):
        """Operator resume of a BLOCKED job once the provider issue is resolved."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._job(db.execute("SELECT * FROM close_jobs WHERE source_session=?",
                                       (source_session.isoformat(),)).fetchone())
            if job is None or job["status"] != "BLOCKED":
                return job
            status = "PARTIAL" if job["published"] else "PENDING"
            stopped = {s: o for s, o in job["outcomes"].items() if o["status"] == "PROVIDER_STOP"}
            outcomes = {**job["outcomes"], **{s: {**o, "status": "RETRY_AFTER_OPERATOR_RESUME"}
                                             for s, o in stopped.items()}}
            record = {"at": _time(now), "status": status, "resumed_by": actor,
                      "previous_block": job["blocked_reason"], "previous_provider_stops": stopped}
            # Eligible at the next runner invocation, whatever its clock.
            db.execute("UPDATE close_jobs SET status=?, outcomes=?, blocked_reason=NULL, next_attempt_at=NULL, history=? "
                       "WHERE source_session=?", (status, _json(outcomes), _json((job["history"] + [record])[-50:]),
                                                  source_session.isoformat()))
            return self._job(db.execute("SELECT * FROM close_jobs WHERE source_session=?",
                                        (source_session.isoformat(),)).fetchone())

    def close_job_volume_pending(self, target_session):
        with self._db() as db:
            rows = db.execute("SELECT volume_pending FROM close_jobs WHERE target_session=?",
                              (target_session.isoformat(),)).fetchall()
        return set().union(*(json.loads(r["volume_pending"]) for r in rows)) if rows else set()

    def load_armed(self, day):
        with self._db() as db:
            row = db.execute("SELECT market,payload FROM armed WHERE day=?", (day.isoformat(),)).fetchone()
            return (row["market"], json.loads(row["payload"])) if row else None

    @staticmethod
    def _transition(db, event_id, state, now, reason):
        row = db.execute("SELECT state FROM events WHERE id=?", (event_id,)).fetchone()
        if row and row["state"] == "triggered" and row["state"] != state:
            db.execute("UPDATE events SET state=?,reason=? WHERE id=?", (state, reason, event_id))
            db.execute("INSERT INTO transitions(event_id,state,at,reason) VALUES (?,?,?,?)",
                       (event_id, state, _time(now), reason))

    def _expire(self, db, now):
        stamp = _time(now)
        for row in db.execute("SELECT id FROM events WHERE state='triggered' AND expires_at<=?", (stamp,)).fetchall():
            self._transition(db, row["id"], "expired", now, "entry session ended")
        db.execute("UPDATE candidates SET blocked=COALESCE(blocked,'entry session ended') WHERE expires_at<=?", (stamp,))

    def expire(self, now):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)

    def suspend(self, sig, day, reason):
        with self._db() as db:
            db.execute("UPDATE candidates SET suspended=? WHERE id=?", (reason, candidate_id(sig, day)))

    def suspend_symbols(self, symbols, reason):
        with self._db() as db:
            db.executemany("UPDATE candidates SET suspended=? WHERE symbol=? AND blocked IS NULL",
                           [(reason, symbol) for symbol in symbols])

    def suspend_all(self, reason):
        with self._db() as db:
            db.execute("UPDATE candidates SET suspended=? WHERE blocked IS NULL", (reason,))

    def observe(self, sig: Signal, day: date, observations: list[dict], now: datetime):
        """Consume validated completed bars atomically; return newly surviving events.

        Callers must run Step 06 completion/identity/action checks first. Observation
        data has bar_end, OHLC and candidate entry alternatives for each bar. Stored
        observation hashes detect revisions; retries never manufacture another event.
        """
        cid = candidate_id(sig, day)
        if sig.setup_version != CARDS[sig.setup_id].fingerprint():
            raise SignalStateError("Setup version changed or unknown; rebuild candidate")
        stamp = clock(now)
        opened, closed = session(day)
        if stamp.date() != day or not opened <= stamp < closed:
            self.expire(now)
            return []
        expiry = _time(closed)
        fresh_ids = []
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)
            existing = db.execute("SELECT blocked FROM candidates WHERE id=?", (cid,)).fetchone()
            if existing and existing["blocked"]:
                return []
            # New evaluated terms supersede prior candidates for this setup/direction.
            peers = db.execute('''SELECT id,active_event,last_bar,checked_at FROM candidates WHERE day=? AND symbol=?
                                  AND setup_id=? AND direction=? AND id<>?''',
                               (day.isoformat(), sig.symbol, sig.setup_id, sig.direction, cid)).fetchall()
            if any(p["checked_at"] and clock(p["checked_at"]) > stamp for p in peers):
                raise SignalStateError("Observation clock precedes existing setup evidence")
            for peer in peers:
                if peer["active_event"]:
                    self._transition(db, peer["active_event"], "invalidated", now, "setup evidence replaced")
                db.execute("UPDATE candidates SET blocked=COALESCE(blocked,'setup evidence replaced') WHERE id=?", (peer["id"],))
            floor = db.execute("SELECT start_after FROM armed_floors WHERE day=? AND candidate_id=?",
                               (day.isoformat(), cid)).fetchone()
            db.execute('''INSERT OR IGNORE INTO candidates
                       (id,day,symbol,setup_id,direction,payload,card_version,expires_at,start_after)
                       VALUES (?,?,?,?,?,?,?,?,?)''',
                       (cid, day.isoformat(), sig.symbol, sig.setup_id, sig.direction,
                        _json(signal_payload(sig)), sig.setup_version, expiry,
                        max([p["last_bar"] for p in peers if p["last_bar"]] + ([floor["start_after"]] if floor else []),
                            default=None)))
            c = dict(db.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone())
            if c["blocked"]:
                return []
            if c["checked_at"] and stamp < clock(c["checked_at"]):
                raise SignalStateError("Observation clock moved backwards")
            db.execute("UPDATE candidates SET suspended=NULL WHERE id=?", (cid,))
            for obs in observations:
                end = _time(obs["bar_end"])
                if clock(end) > stamp:
                    raise SignalStateError("Future observation")
                if c["start_after"] and end <= c["start_after"]:
                    continue  # changed setup terms cannot recycle earlier crossings
                digest = _hash(obs)
                seen = db.execute("SELECT digest FROM observations WHERE candidate_id=? AND bar_end=?", (cid, end)).fetchone()
                if seen:
                    if seen["digest"] != digest:
                        if c["active_event"]:
                            self._transition(db, c["active_event"], "invalidated", now, "consumed bars revised")
                        db.execute("UPDATE candidates SET blocked='consumed bars revised' WHERE id=?", (cid,))
                        return []
                    continue
                if c["last_bar"] and end <= c["last_bar"]:
                    raise SignalStateError("Out-of-order observation")
                db.execute("INSERT INTO observations VALUES (?,?,?)", (cid, end, digest))
                c["last_bar"] = end
                long = sig.direction == "long"
                active = db.execute("SELECT * FROM events WHERE id=?", (c["active_event"],)).fetchone() if c["active_event"] else None
                frozen_stop = (json.loads(active["signal_terms"])["stop"]
                               if active and active["state"] == "triggered" and active["signal_terms"] else sig.stop)
                stopped = frozen_stop is not None and (obs["low"] <= frozen_stop if long else obs["high"] >= frozen_stop)
                if stopped:
                    if c["active_event"]:
                        self._transition(db, c["active_event"], "invalidated", now, "setup stop touched")
                    c["blocked"] = "setup stop touched; setup must be evaluated again"
                    break
                if active and active["state"] == "triggered":
                    failed = obs["close"] <= active["entry_level"] if long else obs["close"] >= active["entry_level"]
                    if failed:
                        self._transition(db, active["id"], "invalidated", now, "closed back through entry")
                        c["active_event"] = None
                        # This closed bar supplies the reset; a subsequent bar must cross.
                    else:
                        until = min(clock(end) + pd.Timedelta(minutes=15), closed)
                        db.execute("UPDATE events SET valid_until=? WHERE id=?", (_time(until), active["id"]))
                    continue
                if c["reset_needed"]:
                    # A user/journal closure requires an observed neutral-side bar
                    # after closure before another crossing may become a new event.
                    if c["reset_after"] and end <= c["reset_after"]:
                        continue
                    level = active["entry_level"] if active else sig.trigger
                    if (obs["close"] <= level if long else obs["close"] >= level):
                        c["reset_needed"] = 0
                    continue
                entries = obs["entries"]
                if not entries:
                    continue
                level, why = entries[0]
                eid = _hash([cid, end, level])
                until = min(clock(end) + pd.Timedelta(minutes=15), closed)
                bound = sig
                if sig.stop_basis == "session_low":
                    stop = obs.get("session_low")
                    if stop is None or not 0 < stop < level:
                        raise SignalStateError("Session-low evidence missing or invalid")
                    # Keep a visible event when width fails; eligibility explains why.
                    bound = replace(sig, trigger=level, stop=stop)
                db.execute("INSERT INTO events (id,candidate_id,state,trigger_at,observed_at,entry_level,expires_at,valid_until,reason,signal_terms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (eid, cid, "triggered", end, _time(now), level, expiry, _time(until), why,
                            _json(signal_payload(bound))))
                db.execute("INSERT INTO transitions(event_id,state,at,reason) VALUES (?,?,?,?)", (eid, "triggered", _time(now), why))
                c["active_event"] = eid
                fresh_ids.append(eid)
                direct_entry = sig.setup_id == "10_connors_rsi2" or why == "first 15 minutes under support"
                if not direct_entry and (obs["close"] <= level if long else obs["close"] >= level):
                    self._transition(db, eid, "invalidated", now, "breakout failed in trigger bar")
                    c["active_event"] = None
            db.execute("UPDATE candidates SET last_bar=?,active_event=?,reset_needed=?,blocked=?,checked_at=? WHERE id=?",
                       (c["last_bar"], c["active_event"], c["reset_needed"], c["blocked"], _time(now), cid))
            return [self._view(db, eid, now) for eid in fresh_ids if self._view(db, eid, now)["eligible"]]

    def _view(self, db, event_id, now):
        row = db.execute('''SELECT e.*,c.payload,c.card_version,c.suspended,c.blocked,c.setup_id,c.checked_at
                            FROM events e JOIN candidates c ON c.id=e.candidate_id WHERE e.id=?''', (event_id,)).fetchone()
        if row is None:
            raise SignalStateError("Unknown signal event")
        out = dict(row)
        out["candidate_signal"] = json.loads(out.pop("payload"))
        terms = out.pop("signal_terms")
        out["signal"] = json.loads(terms) if terms else out["candidate_signal"]
        sig = restore_signal(out["signal"])
        out["stop_width_valid"] = (sig.stop is not None and (sig.stop_basis != "session_low" or
            (out["entry_level"] - sig.stop) / out["entry_level"] <=
            CARDS[sig.setup_id].p("max_stop_adr") * sig.adr_pct / 100 + 1e-12))
        out["terms_digest"] = _hash(out["signal"]) if terms else None
        card = CARDS.get(out["setup_id"])
        out["eligible"] = (out["state"] == "triggered" and not out["suspended"] and not out["blocked"]
                           and bool(terms) and out["stop_width_valid"]
                           and bool(card) and card.fingerprint() == out["card_version"]
                           and clock(out["observed_at"]) <= clock(now) < clock(out["valid_until"])
                           and (not out["checked_at"] or clock(out["checked_at"]) <= clock(now))
                           and clock(now) < clock(out["expires_at"]))
        return out

    def get(self, event_id, now):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)
            return self._view(db, event_id, now)

    @contextmanager
    def held_event(self, event_id):
        """Hold the signal store's write lock and yield ``view(at)`` for one event.

        A ticket's final approve/consume write runs inside this block, so an
        invalidation, suspension, closure or new terms revision either committed
        before (and shows in the view) or waits until that write has committed.
        """
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")

            def view(at):
                self._expire(db, at)
                return self._view(db, event_id, at)

            def persisted():
                """The stored candidate row and event terms, read under this lock with no
                clock (re-audit R2: which evidence the event depends on)."""
                row = db.execute("SELECT c.setup_id, c.payload, e.signal_terms FROM events e "
                                 "JOIN candidates c ON c.id=e.candidate_id WHERE e.id=?", (event_id,)).fetchone()
                if row is None:
                    raise SignalStateError("Unknown signal event")
                return {"setup_id": row["setup_id"], "candidate_signal": row["payload"],
                        "signal_terms": row["signal_terms"]}
            view.persisted = persisted
            yield view

    def events(self, now):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)
            return [self._view(db, r["id"], now) for r in db.execute("SELECT id FROM events ORDER BY trigger_at,id").fetchall()]

    def close(self, event_id, now, reason):
        if not reason.strip():
            raise SignalStateError("Closure requires a reason")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)
            event = self._view(db, event_id, now)
            if clock(now) < clock(event["observed_at"]):
                raise SignalStateError("Closure precedes observation")
            if event["state"] == "triggered":
                self._transition(db, event_id, "closed", now, reason)
                db.execute("UPDATE candidates SET reset_needed=1,reset_after=? WHERE id=?", (_time(now), event["candidate_id"]))

    def invalidate(self, event_id, now, reason):
        """Invalidate exact candidate after an authoritative live stop observation."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)
            event = self._view(db, event_id, now)
            if event["state"] == "triggered":
                self._transition(db, event_id, "invalidated", now, reason)
                db.execute("UPDATE candidates SET blocked=? WHERE id=?", (reason, event["candidate_id"]))

    def invalidate_candidate(self, sig, day, now, reason, *, rebuild=False):
        """An authoritative history revision retires this exact evaluated candidate."""
        cid = candidate_id(sig,day)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO candidates
                (id,day,symbol,setup_id,direction,payload,card_version,expires_at,last_bar)
                VALUES (?,?,?,?,?,?,?,?,?)""", (cid,day.isoformat(),sig.symbol,sig.setup_id,sig.direction,
                    _json(signal_payload(sig)),sig.setup_version,_time(session(day)[1]),
                    _time(clock(now).floor("15min"))))
            for row in db.execute("SELECT id FROM events WHERE candidate_id=?",(cid,)).fetchall():
                self._transition(db,row["id"],"invalidated",now,reason)
            db.execute("""UPDATE candidates SET blocked=?,
                last_bar=MAX(COALESCE(last_bar,''),?),checked_at=? WHERE id=?""",
                (reason,_time(clock(now).floor("15min")),_time(now),cid))
            if rebuild:
                db.execute("""INSERT OR IGNORE INTO revision_rebuilds
                    (candidate_id,day,symbol,setup_id,direction,requested_at,status,reason)
                    VALUES (?,?,?,?,?,?,'PENDING',?)""",
                    (cid,day.isoformat(),sig.symbol,sig.setup_id,sig.direction,_time(now),reason))

    def pending_rebuilds(self, day):
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM revision_rebuilds WHERE day=? AND status='PENDING' ORDER BY requested_at,candidate_id",
                (day.isoformat(),))]

    def rebuild_request(self, cid):
        with self._db() as db:
            row = db.execute("SELECT * FROM revision_rebuilds WHERE candidate_id=?", (cid,)).fetchone()
            return dict(row) if row else None

    def close_stale_rebuilds(self, day, now):
        """Give requests from an earlier entry session an explicit terminal outcome.

        Their candidates already expired with that session (no entry is possible);
        this only stops them sitting PENDING in storage forever.
        """
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM revision_rebuilds WHERE day<? AND status='PENDING' ORDER BY day,candidate_id",
                (day.isoformat(),))]
            db.execute("""UPDATE revision_rebuilds SET status='EXPIRED',attempted_at=?,
                          reason='entry session ended before the rebuild completed'
                          WHERE day<? AND status='PENDING'""", (_time(now), day.isoformat()))
            return rows

    def finish_rebuild(self, cid, now, status, reason, replacement=None):
        """Replace exactly the retired candidate and queue state in one transaction."""
        if status not in {"REBUILT", "NO_SETUP", "PENDING", "REMOVED"}:
            raise SignalStateError("Invalid rebuild outcome")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM revision_rebuilds WHERE candidate_id=?",(cid,)).fetchone()
            if row is None or row["status"] != "PENDING":
                return False
            day = date.fromisoformat(row["day"])
            if (clock(now).date() != day or not session(day)[0] <= clock(now) < session(day)[1]
                    or clock(now) < clock(row["requested_at"])
                    or row["attempted_at"] and clock(now) < clock(row["attempted_at"])):
                raise SignalStateError("Rebuild clock outside original entry session")
            rid = None
            if replacement is not None:
                if status != "REBUILT" or (replacement.symbol,replacement.setup_id,replacement.direction) != (
                        row["symbol"],row["setup_id"],row["direction"]):
                    raise SignalStateError("Rebuild changed ticker/setup/direction")
                rid = candidate_id(replacement,day)
                if rid == cid:
                    raise SignalStateError("Rebuild cannot restore retired evidence")
            elif status == "REBUILT":
                raise SignalStateError("Rebuilt signal missing")
            if status != "PENDING":
                old = db.execute("SELECT * FROM armed WHERE day=?",(day.isoformat(),)).fetchone()
                if old is None:
                    raise SignalStateError("Armed list missing for rebuild")
                original = json.loads(old["payload"])
                if cid not in {candidate_id(restore_signal(p),day) for p in original}:
                    db.execute("UPDATE revision_rebuilds SET status='REMOVED',attempted_at=?,reason=? WHERE candidate_id=?",
                               (_time(now),"retired candidate already withdrawn/replaced",cid))
                    return False
                payload = [p for p in original if candidate_id(restore_signal(p),day) != cid]
                if replacement is not None:
                    payload.append(signal_payload(replacement))
                unique = {candidate_id(restore_signal(p),day):p for p in payload}
                encoded = _json(list(unique.values()))
                db.execute("UPDATE armed SET payload=? WHERE day=?",(encoded,day.isoformat()))
                db.execute("INSERT INTO armed_history(day,market,payload,observed_at) VALUES (?,?,?,?)",
                           (day.isoformat(),old["market"],encoded,_time(now)))
                # Detector completion, not merely the earlier failed scan, is the
                # earliest boundary from which a new crossing may be considered.
                db.execute("UPDATE candidates SET last_bar=MAX(COALESCE(last_bar,''),?) WHERE id=?",
                           (_time(clock(now).floor("15min")),cid))
            db.execute("""UPDATE revision_rebuilds SET status=?,attempted_at=?,reason=?,replacement_id=?
                          WHERE candidate_id=?""",(status,_time(now),reason,rid,cid))
            return True

    def armed_history(self, day):
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM armed_history WHERE day=? ORDER BY sequence", (day.isoformat(),))]

    def history(self, event_id):
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM transitions WHERE event_id=? ORDER BY sequence", (event_id,))]
