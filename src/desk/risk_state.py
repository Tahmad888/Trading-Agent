"""Durable broker snapshots and human warning-review audit; no order permissions.

One authoritative snapshot includes all open/pending exposures. Rejected orders
are removed by broker reconciliation, never by counting proposal evaluations.
SQLite transactions serialize updates and preserve earlier state on failure.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from desk.contracts import RiskDecision, TradeProposal
from desk.risk import AccountState, RiskLimits, evaluate
from desk.risk_context import AccountEvidence

ET = ZoneInfo("America/New_York")


class RiskStateError(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _actor(actor, reason, now):
    if not actor.strip() or not reason.strip() or now.tzinfo is None or now.utcoffset() is None:
        raise RiskStateError("Named actor, reason and timezone-aware timestamp required")


class RiskStateStore:
    """Call only from trusted adapters/UI control handlers, never analyst tools.

    actor is an audit identity supplied by that handler, not authentication here.
    Reviews/acknowledgements are NOT execution approval tokens (Step 13).
    """
    def __init__(self, path: Path):
        self.path = path
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RiskStateError("Unsupported risk-state schema")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    account_id TEXT, observation_id TEXT, digest TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(account_id, observation_id));
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, event TEXT NOT NULL,
                    at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    expires_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS acknowledgements (
                    review_id TEXT PRIMARY KEY REFERENCES reviews(id), at TEXT NOT NULL,
                    actor TEXT NOT NULL, reason TEXT NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _read(db, account_id):
        row = db.execute("SELECT revision,payload FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise RiskStateError("No reconciled account snapshot")
        evidence = AccountEvidence.model_validate_json(row[1])
        if evidence.account_id != account_id:
            raise RiskStateError("Stored account identity mismatch")
        return row[0], evidence

    def load(self, account_id: str) -> tuple[int, AccountState]:
        with self._connect() as db:
            revision, evidence = self._read(db, account_id)
        payload = evidence.model_dump()
        payload["exposures"] = evidence.exposures
        return revision, AccountState(**payload)

    @contextmanager
    def held_revision(self, account_id: str):
        """Yield the current revision while holding a read lock on the account store.

        A dependent write (a ticket consumption) runs inside this block. In SQLite's
        rollback-journal mode no snapshot or manual control can commit until the block
        ends, so it is ordered after that write; one that committed earlier shows as a
        newer revision. Keep the block short: a writer waits at most 5 seconds.
        """
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("BEGIN")
            revision, _ = self._read(db, account_id)
            yield revision
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")
            db.close()

    def save_snapshot(self, snapshot: AccountState, observation_id: str, *, now: datetime,
                      cash_flow_usd: float = 0, actor: str = "", reason: str = "") -> int:
        evidence = AccountEvidence.model_validate(asdict(snapshot))
        if not observation_id.strip() or now.tzinfo is None or now.utcoffset() is None or evidence.as_of > now:
            raise RiskStateError("Invalid observation identity/time")
        day = evidence.as_of.astimezone(ET).date()
        if evidence.pnl_day != day:
            raise RiskStateError("Snapshot P&L day is not its ET observation date")
        # Nonzero cash flow requires explicit, audited reconciliation. json rejects NaN/Inf.
        fingerprint = _digest({"snapshot": evidence.model_dump(mode="json"), "cash_flow": cash_flow_usd,
                               "actor": actor, "reason": reason})
        if cash_flow_usd:
            _actor(actor, reason, now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT digest,revision FROM observations WHERE account_id=? AND observation_id=?",
                               (evidence.account_id, observation_id)).fetchone()
            if prior:
                if fingerprint != prior[0]:
                    raise RiskStateError("Observation ID reused with different content")
                return prior[1]
            exists = db.execute("SELECT 1 FROM accounts WHERE account_id=?", (evidence.account_id,)).fetchone()
            revision = 1
            if exists:
                revision, old = self._read(db, evidence.account_id)
                if evidence.as_of <= old.as_of:
                    raise RiskStateError("Out-of-order account snapshot")
                adjusted_high = old.equity_high_water_mark + cash_flow_usd
                if adjusted_high <= 0:
                    raise RiskStateError("Cash flow requires a separately reconciled positive equity baseline")
                evidence.equity_high_water_mark = max(adjusted_high, evidence.equity_high_water_mark)
                evidence.halted = old.halted or evidence.halted
                revision += 1
            elif cash_flow_usd:
                raise RiskStateError("Initial snapshot must already contain reconciled cash-flow baselines")
            evidence = AccountEvidence.model_validate(evidence.model_dump(warnings=False))
            db.execute("INSERT OR REPLACE INTO accounts VALUES (?,?,?)",
                       (evidence.account_id, revision, evidence.model_dump_json()))
            db.execute("INSERT INTO observations VALUES (?,?,?,?)",
                       (evidence.account_id, observation_id, fingerprint, revision))
            if cash_flow_usd:
                self._audit(db, evidence.account_id, "cash_flow_reconciliation", now, actor, reason,
                            {"cash_flow_usd": cash_flow_usd, "revision": revision})
            return revision

    @staticmethod
    def _audit(db, account_id, event, now, actor, reason, payload):
        db.execute("INSERT INTO audit(account_id,event,at,actor,reason,payload) VALUES (?,?,?,?,?,?)",
                   (account_id, event, now.isoformat(), actor, reason, _json(payload)))

    def set_manual_halt(self, account_id: str, halted: bool, *, expected_revision: int,
                        actor: str, reason: str, now: datetime) -> int:
        _actor(actor, reason, now)
        if type(halted) is not bool:
            raise RiskStateError("Manual halt must be an explicit boolean")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            revision, evidence = self._read(db, account_id)
            if revision != expected_revision:
                raise RiskStateError("Manual control requires current revision")
            if now < evidence.as_of:
                raise RiskStateError("Control time predates the account snapshot")
            evidence.halted = halted
            revision += 1
            db.execute("UPDATE accounts SET revision=?,payload=? WHERE account_id=?",
                       (revision, evidence.model_dump_json(), account_id))
            self._audit(db, account_id, "manual_halt" if halted else "manual_resume", now, actor, reason,
                        {"revision": revision})
            return revision

    def review(self, account_id: str, proposal: TradeProposal, *, now: datetime, **risk_inputs):
        """Compute the decision from the current saved snapshot before recording it."""
        revision, account = self.load(account_id)
        decision = evaluate(proposal, account, now=now, **risk_inputs)
        review_id = self._save_review(account_id, revision, proposal, decision, now=now,
                                      limits=risk_inputs.get("limits", RiskLimits())) if decision.approved and decision.warnings else None
        return decision, review_id

    def _save_review(self, account_id: str, revision: int, proposal: TradeProposal,
                    decision: RiskDecision, *, now: datetime, limits: RiskLimits) -> str:
        """Persist a locally eligible review; caller is the trusted risk/ticket handler."""
        proposal = TradeProposal.model_validate(proposal.model_dump(warnings=False))
        decision = RiskDecision.model_validate(decision.model_dump(warnings=False))
        if not decision.approved or proposal.proposal_id != decision.proposal_id or not decision.warnings:
            raise RiskStateError("Only an eligible ticket with current warnings can be acknowledged")
        if now.tzinfo is None or now.utcoffset() is None:
            raise RiskStateError("Review clock must be timezone aware")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current, evidence = self._read(db, account_id)
            if current != revision or evidence.halted or not (timedelta(0) <= now - evidence.as_of <= limits.max_account_state_age):
                raise RiskStateError("Review requires current, fresh, unhalted account state")
            payload = {"proposal": proposal.model_dump(mode="json"), "decision": decision.model_dump(mode="json"),
                       "account_id": account_id, "revision": revision, "created_at": now.isoformat()}
            review_id = _digest(payload)
            # Review can never outlive its snapshot/quote, even without a new snapshot.
            deadlines = [evidence.as_of + limits.max_account_state_age,
                         proposal.quote_as_of + limits.max_quote_age,
                         decision.market_context_as_of + limits.max_market_context_age]
            if decision.signal_terms_valid_until:
                deadlines.append(decision.signal_terms_valid_until)
            if decision.contract_metadata_as_of:
                deadlines.append(decision.contract_metadata_as_of + limits.max_contract_metadata_age)
            expires = min(deadlines)
            if expires < now:
                raise RiskStateError("Quote no longer fresh")
            db.execute("INSERT OR IGNORE INTO reviews VALUES (?,?,?,?,?)",
                       (review_id, account_id, revision, expires.isoformat(), _json(payload)))
            return review_id

    def acknowledge(self, review_id: str, codes: set[str], *, actor: str, reason: str, now: datetime):
        _actor(actor, reason, now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT account_id,revision,expires_at,payload FROM reviews WHERE id=?", (review_id,)).fetchone()
            if row is None:
                raise RiskStateError("Unknown review")
            current, evidence = self._read(db, row[0])
            payload = json.loads(row[3])
            if current != row[1] or evidence.halted or not (
                datetime.fromisoformat(payload["created_at"]) <= now <= datetime.fromisoformat(row[2])
            ):
                raise RiskStateError("Review is stale; obtain a new decision")
            expected = {w["code"] for w in payload["decision"]["warnings"]}
            if codes != expected:
                raise RiskStateError("Acknowledge exactly the warnings displayed on this review")
            if db.execute("SELECT 1 FROM acknowledgements WHERE review_id=?", (review_id,)).fetchone():
                raise RiskStateError("Review already acknowledged")
            db.execute("INSERT INTO acknowledgements VALUES (?,?,?,?)", (review_id, now.isoformat(), actor, reason))
            self._audit(db, row[0], "warning_override", now, actor, reason,
                        {"review_id": review_id, "codes": sorted(codes), "revision": current})

    def audit(self, account_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT event,at,actor,reason,payload FROM audit WHERE account_id=? ORDER BY id", (account_id,)).fetchall()
        return [dict(event=r[0], at=r[1], actor=r[2], reason=r[3], payload=json.loads(r[4])) for r in rows]
