"""Durable signal observations, not approvals or orders. See checkpoint 07.

SQLite transactions own event identity and transitions. Reads always take a clock;
no persisted boolean authorizes a trade. Plan B: retain history, deny eligibility.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pandas as pd

from desk.calendar import clock, session
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
    clock(sig.as_of)
    # Rationale text may change without changing executable terms.
    terms = signal_payload(sig)
    terms.pop("saw")
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
                CREATE TABLE IF NOT EXISTS transitions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
                    state TEXT NOT NULL, at TEXT NOT NULL, reason TEXT NOT NULL);
            ''')

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
            db.execute('''INSERT OR IGNORE INTO candidates
                       (id,day,symbol,setup_id,direction,payload,card_version,expires_at,start_after)
                       VALUES (?,?,?,?,?,?,?,?,?)''',
                       (cid, day.isoformat(), sig.symbol, sig.setup_id, sig.direction,
                        _json(signal_payload(sig)), sig.setup_version, expiry,
                        max((p["last_bar"] for p in peers if p["last_bar"]), default=None)))
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
                stopped = obs["low"] <= sig.stop if long else obs["high"] >= sig.stop
                if stopped:
                    if c["active_event"]:
                        self._transition(db, c["active_event"], "invalidated", now, "setup stop touched")
                    c["blocked"] = "setup stop touched; setup must be evaluated again"
                    break
                active = db.execute("SELECT * FROM events WHERE id=?", (c["active_event"],)).fetchone() if c["active_event"] else None
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
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                           (eid, cid, "triggered", end, _time(now), level, expiry, _time(until), why))
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
        out["signal"] = json.loads(out.pop("payload"))
        card = CARDS.get(out["setup_id"])
        out["eligible"] = (out["state"] == "triggered" and not out["suspended"] and not out["blocked"]
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

    def armed_history(self, day):
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM armed_history WHERE day=? ORDER BY sequence", (day.isoformat(),))]

    def history(self, event_id):
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM transitions WHERE event_id=? ORDER BY sequence", (event_id,))]
