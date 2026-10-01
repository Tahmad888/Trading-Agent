"""Persistent, reviewed split/dividend evidence; no price adjustment is guessed.

Source event keys use security, type and effective date. A date correction is a
removal plus addition, requiring review. SQLite transactions preserve generations.
Plan B: failed refreshes disable publication, retaining previous evidence.
"""
from contextlib import closing
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from desk.action_evidence import ActionRecord
from desk.alphavantage import ActionSnapshot, ActionObservation, _date, _number, AlphaVantageError
from desk.bars import BarDataError
from desk.calendar import clock
from desk.data_basis import Evidence, PriceBasis, Text


class SecurityReview(Evidence):
    """Operator-reviewed source crosswalk and coverage, never inferred from ticker.

    coverage evidence names the source/history checked and exceptional-action
    review. USD cash amounts must be effective-date (not later split-adjusted).
    """
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
    source_symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
    security_id: Text
    currency: Literal["USD"]
    exchange_mic: Text
    instrument_name: Text
    identity_evidence_ref: Text
    coverage_evidence_ref: Text
    coverage_start: date
    valid_through: date
    reviewed_at: AwareDatetime
    cash_amount_basis: Literal["effective_date_per_share"]
    exceptional_actions: Literal["none_found", "unresolved"]

    @model_validator(mode="after")
    def dates(self):
        if self.coverage_start > self.valid_through:
            raise ValueError("Invalid mapping/coverage interval")
        return self


def snapshot_from_report(raw: dict) -> ActionSnapshot:
    """Read the safe --include-records output, with original receipt time required.

    Accept one check or a one-check report. Do not assign today's time to imports.
    """
    try:
        if not isinstance(raw, dict):
            raise ValueError()
        if "checks" in raw:
            if raw.get("stopped_on_error") or len(raw["checks"]) != 1:
                raise ValueError()
            raw = raw["checks"][0]
        if not isinstance(raw, dict):
            raise ValueError()
        if raw.get("status") not in ("OBSERVATIONS_ONLY", "EMPTY_UNVERIFIED"):
            raise ValueError()
        symbol, function = raw["symbol"], raw["function"]
        if function not in ("SPLITS", "DIVIDENDS"):
            raise ValueError()
        received = datetime.fromisoformat(raw["received_at"])
        if received.tzinfo is None or received.utcoffset() is None:
            raise ValueError()
        rows = []
        for item in raw["records"]:
            value = _number(item["value"])
            if function == "SPLITS" and value in (0, 1):
                raise ValueError()
            # Recompute known issue flags, never trust supplied status annotations.
            aux = tuple(_date(item.get(k), optional=True) for k in
                        ("declaration_date", "record_date", "payment_date"))
            issues = []
            if function == "DIVIDENDS":
                if value == 0:
                    issues.append("ZERO_AMOUNT_IGNORED")
                if None in aux:
                    issues.append("MISSING_AUXILIARY_DATE")
            rows.append(ActionObservation(_date(item["event_date"]), value, *aux, tuple(issues)))
        if len(rows) != raw["row_count"] or len({r.event_date for r in rows}) != len(rows):
            raise ValueError()
        return ActionSnapshot(symbol, function, received, tuple(sorted(rows, key=lambda r: r.event_date)))
    except (KeyError, TypeError, ValueError, AlphaVantageError):
        raise BarDataError("Invalid dated corporate-action snapshot report") from None


def _prepare(review, splits, dividends, now):
    day = clock(now).date()
    if (clock(review.reviewed_at) > clock(now) or
            not review.coverage_start <= day <= review.valid_through or
            review.exceptional_actions != "none_found"):
        raise BarDataError("Security/coverage review is unavailable for this session")
    actions, snapshots = [], []
    for snap, function in ((splits, "SPLITS"), (dividends, "DIVIDENDS")):
        # Revalidate even in-process dataclass instances; their types are not enforced.
        snap = snapshot_from_report(snap.report(include_records=True))
        if snap.function != function or snap.symbol != review.source_symbol:
            raise BarDataError("Corporate-action snapshot identity/function mismatch")
        if clock(snap.received_at).date() != day or clock(snap.received_at) > clock(now):
            raise BarDataError("Both corporate-action sources must be refreshed this session")
        report = snap.report(include_records=True)
        evidence = "sha256:" + hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
        snapshots.append(report)
        for row in snap.rows:
            if not review.coverage_start <= row.event_date <= day or row.value == 0:
                continue  # future/out-of-interval/zero observations remain in snapshots
            kind = "split" if function == "SPLITS" else "cash_dividend"
            terms = {"new_shares": row.value, "old_shares": 1} if kind == "split" else {"cash_amount": row.value}
            actions.append(ActionRecord(source="Alpha Vantage REST", evidence_ref=evidence,
                event_id=f"av:{review.security_id}:{function}:{row.event_date}",
                security_id=review.security_id, currency=review.currency, kind=kind,
                effective_session=row.event_date, status="confirmed", **terms))
    return actions, snapshots


class ActionLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS generations (
                    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, published_at TEXT NOT NULL,
                    review TEXT NOT NULL, records TEXT NOT NULL, snapshots TEXT NOT NULL,
                    changes TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS current (
                    symbol TEXT PRIMARY KEY, generation INTEGER, status TEXT NOT NULL);
            ''')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def unavailable(self, symbol, reason="REFRESH_FAILED"):
        # Fixed codes only; never provider bodies or exception strings in the store.
        if reason not in {"REFRESH_FAILED", "REMOVALS_REQUIRE_REVIEW"}:
            raise ValueError("Unsupported ledger status")
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO current(symbol,status) VALUES(?,?) "
                       "ON CONFLICT(symbol) DO UPDATE SET status=excluded.status", (symbol, reason))

    def publish(self, review: SecurityReview, splits: ActionSnapshot, dividends: ActionSnapshot,
                now: datetime, *, accept_removals=False) -> dict:
        """Publish both successful sources together; failures invalidate old publication.

        Removing an event or changing its date is not silently interpreted as a
        cancellation. Explicit reconciliation must accept the removal first.
        """
        try:
            records, snapshots = _prepare(review, splits, dividends, now)
            current_terms = {r.event_id: r.to_action(security_id=review.security_id,
                             currency=review.currency).revision for r in records}
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute("SELECT g.review,g.records,g.published_at,g.snapshots FROM generations g "
                    "JOIN current c ON g.id=c.generation WHERE c.symbol=?", (review.symbol,)).fetchone()
                old_terms = {}
                if prior:
                    previous = SecurityReview.model_validate_json(prior[0])
                    if (previous.security_id, previous.source_symbol, previous.currency,
                        previous.exchange_mic, previous.coverage_start) != (
                        review.security_id, review.source_symbol, review.currency,
                        review.exchange_mic, review.coverage_start):
                        raise BarDataError("Ledger security/history mapping changed; requires separate reconciliation")
                    if clock(now) < clock(prior[2]):
                        raise BarDataError("Cannot replace newer corporate-action evidence with older data")
                    old_receipts = {s["function"]: clock(s["received_at"]) for s in json.loads(prior[3])}
                    if any(clock(s["received_at"]) < old_receipts[s["function"]] for s in snapshots):
                        raise BarDataError("Cannot replace newer source snapshots with older observations")
                    for raw in json.loads(prior[1]):
                        r = ActionRecord.model_validate(raw)
                        old_terms[r.event_id] = r.to_action(security_id=review.security_id,
                                                          currency=review.currency).revision
                changes = {"added": sorted(current_terms.keys() - old_terms.keys()),
                           "removed": sorted(old_terms.keys() - current_terms.keys()),
                           "corrected": sorted(k for k in old_terms.keys() & current_terms.keys()
                                               if old_terms[k] != current_terms[k])}
                if changes["removed"] and not accept_removals:
                    db.execute("UPDATE current SET status='REMOVALS_REQUIRE_REVIEW' WHERE symbol=?", (review.symbol,))
                    return {"status": "REMOVALS_REQUIRE_REVIEW", **changes}
                cursor = db.execute("INSERT INTO generations(symbol,published_at,review,records,snapshots,changes) "
                    "VALUES(?,?,?,?,?,?)", (review.symbol, now.isoformat(), review.model_dump_json(),
                    json.dumps([r.model_dump(mode="json") for r in records]),
                    json.dumps(snapshots), json.dumps(changes)))
                db.execute("INSERT INTO current(symbol,generation,status) VALUES(?,?,'READY') "
                    "ON CONFLICT(symbol) DO UPDATE SET generation=excluded.generation,status='READY'",
                    (review.symbol, cursor.lastrowid))
                return {"status": "READY", "generation": cursor.lastrowid,
                        "rebuild_required": bool(prior and any(changes.values())), **changes}
        except (BarDataError, ValidationError):
            self.unavailable(review.symbol)
            raise

    def basis(self, symbol: str, now: datetime, *, normalization: str) -> PriceBasis:
        with closing(self._connect()) as db:
            row = db.execute("SELECT c.status,g.id,g.published_at,g.review,g.records FROM current c "
                "LEFT JOIN generations g ON c.generation=g.id WHERE c.symbol=?", (symbol,)).fetchone()
        if row is None or row[0] != "READY":
            raise BarDataError("Corporate-action ledger unavailable; refresh/reconcile this symbol")
        _, generation, published, raw_review, raw_records = row
        review = SecurityReview.model_validate_json(raw_review)
        if clock(published).date() != clock(now).date() or clock(published) > clock(now):
            raise BarDataError("Corporate-action ledger is stale or later than the decision clock")
        records = [ActionRecord.model_validate(r) for r in json.loads(raw_records)]
        return PriceBasis(source="Reviewed Alpha Vantage ledger",
            evidence_ref=f"ledger:{generation}; {review.coverage_evidence_ref}",
            symbol=symbol, security_id=review.security_id, currency=review.currency,
            coverage_start=review.coverage_start, basis_session=clock(now).date(),
            verified_at=published, coverage_complete=True, normalization=normalization,
            actions=tuple(r.to_action(security_id=review.security_id, currency=review.currency) for r in records))
