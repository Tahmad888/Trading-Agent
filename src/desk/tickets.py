"""Local ticket preparation, exact human approval and single-use consumption (G4).

Serves the approve step. A ticket version binds the executable terms the risk layer
independently checked; Taz approves that exact version by typing its budget again
and acknowledging every displayed warning. Consumption reruns every check and then
marks the approval used, once. Nothing here submits, routes or simulates an order.

Trust boundary: account snapshots, signal terms, contract metadata, market context
and market observations come from injected adapters (``RiskInputs``), never from
the ticket request or a caller's claim of approval. Approval and consumption reload
the stored ticket and rerun ``risk.evaluate``; they accept no approved flag, hash or
copied risk result. The terminal records actor names as audit labels only.

Plan B: if an adapter is missing or a recheck fails, the ticket stays visible and
cannot be approved or consumed. Rollback: revert G4; tickets.sqlite is unused by
older code and can be kept for audit. See docs/TICKETS.md.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import getpass
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Annotated, Callable, Iterable, Literal
import uuid

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from desk.contracts import (OPTION_STRUCTURES, ApprovalRecord, Grade, Leg, RiskDecision, Structure, Tier,
                            TradeProposal)
from desk.instruments import ContractBook, InstrumentError, canonical_symbol, loss_measures
from desk.playbook.cards import CARDS
from desk.risk import RiskLimits, evaluate
from desk.risk_context import MarketContext, SetupRegistry
from desk.risk_state import RiskStateError, RiskStateStore
from desk.risk_terms import RiskTerms, RiskTermsSource, stop_distance

Text = Annotated[str, Field(min_length=1)]
DEFAULT_APPROVAL_LIFETIME = timedelta(seconds=120)  # Assumption; see docs/TICKETS.md
MAX_APPROVAL_LIFETIME = timedelta(hours=1)
SCHEMA = "desk-tickets-v1"


class TicketError(ValueError):
    pass


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def usd(value) -> str:
    return f"${Decimal(str(value)):,.2f}"


def price(value) -> str:
    """Exact price text: cents when exact, otherwise every accepted decimal."""
    amount = Decimal(str(value))
    if amount == amount.quantize(Decimal("0.01")):
        return f"${amount:,.2f}"
    return f"${amount.normalize():,f}"


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise TicketError("Ticket clock must be timezone aware")
    return now


class TicketLeg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    symbol: Text
    side: Literal["buy", "sell"] = "buy"
    limit_price: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    qty: Annotated[int, Field(gt=0, strict=True)] | None = None


class TicketRequest(BaseModel):
    """What Taz (or a planner draft he edits) asks for. Never a source of evidence."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: Text
    environment: Literal["paper", "live"]
    event_id: Text
    structure: Structure
    legs: tuple[TicketLeg, ...] = Field(min_length=1)
    budget_usd: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    # Shares default to stop_budget; selected_quantity must be chosen explicitly.
    sizing_mode: Literal["stop_budget", "maximum_loss_budget", "selected_quantity"] | None = None
    est_costs_usd: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    time_stop: AwareDatetime
    exit_rules: tuple[str, ...] = ()
    quote_source: Text
    stop_estimate_source: Text = "independently resolved signal event"
    sector: Text = "unclassified"
    tier: Tier = 0
    grade: Grade | None = None
    plan_id: str | None = None

    @model_validator(mode="after")
    def _choices(self) -> TicketRequest:
        for amount in (self.budget_usd, self.est_costs_usd):
            if amount.as_tuple().exponent < -2:
                raise ValueError("Dollar amounts are whole cents")
        for leg in self.legs:
            if leg.limit_price.as_tuple().exponent < -4:
                raise ValueError("Limit prices have at most four decimals")
        if self.structure == "shares":
            if len(self.legs) != 1 or self.legs[0].side != "buy":
                raise ValueError("A share ticket has one buy leg")
            mode = self.sizing_mode or "stop_budget"
            object.__setattr__(self, "sizing_mode", mode)
            if mode == "selected_quantity" and self.legs[0].qty is None:
                raise ValueError("Selected-quantity sizing needs the share quantity you chose")
            if mode != "selected_quantity" and self.legs[0].qty is not None:
                raise ValueError("A share quantity is used only with explicit selected-quantity sizing")
        elif self.structure in OPTION_STRUCTURES:
            # Engineering choice: an option ticket names its sizing mode and contracts.
            if self.sizing_mode is None:
                raise ValueError("Option tickets need an explicit sizing mode")
            if any(leg.qty is None for leg in self.legs):
                raise ValueError("Option tickets need a contract quantity on every leg")
        else:
            raise ValueError("Unsupported ticket structure")
        return self


class MarketObservation(BaseModel):
    """Trusted adapter's current quote facts; refreshed on every recheck."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    quote_as_of: AwareDatetime
    security_tradable: bool | None
    already_moved_pct: Annotated[float, Field(allow_inf_nan=False)]
    option_spread_pct_mid: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    open_interest: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)


@dataclass
class RiskInputs:
    """Independently supplied adapters. Live wiring is Step 13/15/20 work."""
    risk_state: RiskStateStore
    terms_source: RiskTermsSource
    market: Callable[[datetime], MarketContext | None]
    contract_book: Callable[[datetime], ContractBook | None]
    observe: Callable[[str, tuple[str, ...], datetime], MarketObservation]
    registry: SetupRegistry | None = None
    limits: RiskLimits = field(default_factory=RiskLimits)
    # Read again at each final transaction; UTC wall clock when unset.
    clock: Callable[[], datetime] | None = None


@dataclass(frozen=True)
class Check:
    proposal: TradeProposal | None
    decision: RiskDecision | None
    terms: RiskTerms | None
    binding: dict
    problems: tuple[str, ...]
    book: ContractBook | None
    account_revision: int | None = None


class _TermsSnapshot:
    """The single terms read of one check, so risk validates exactly what is bound."""
    def __init__(self, terms: RiskTerms):
        self.terms = terms

    def resolve(self, event_id: str, now: datetime) -> RiskTerms:
        if event_id != self.terms.event_id:
            raise ValueError("Signal terms snapshot belongs to another event")
        return self.terms


def final_clock(inputs: RiskInputs, started: datetime) -> datetime:
    """Fresh reading for a final transaction; never earlier than the check's start."""
    reading = inputs.clock() if inputs.clock else datetime.now(timezone.utc)
    return max(_aware(reading), started)


TIME_STOP_PASSED = "The ticket's time stop has passed; no new entry is allowed"
ACCOUNT_CHANGED = "Account changed during the check (for example a manual stop or a new snapshot)"


def _contract_id(book: ContractBook | None, symbol: str) -> str | None:
    if book is None:
        return None
    try:
        wanted = canonical_symbol(symbol)
        found = [c for c in book.contracts if canonical_symbol(c.symbol) == wanted]
    except InstrumentError:
        return None
    return found[0].broker_contract_id if len(found) == 1 else None


def _expiry(book: ContractBook | None, symbol: str):
    if book is None:
        return None
    try:
        wanted = canonical_symbol(symbol)
    except InstrumentError:
        return None
    return next((c.expiry for c in book.contracts if canonical_symbol(c.symbol) == wanted), None)


def share_ceiling(request: TicketRequest, terms: RiskTerms) -> int:
    """Upper bound handed to the risk layer for stop-budget shares; risk re-sizes it.

    The risk layer only shrinks, so the request must carry a ceiling. This is the
    same budget arithmetic; the risk layer's own result is what the ticket shows.
    """
    try:
        distance = stop_distance(float(request.legs[0].limit_price), terms.stop, terms.direction)
    except ValueError:
        return 1  # risk will reject the wrong-side stop; no quantity is invented
    available = Fraction(str(request.budget_usd)) - Fraction(str(request.est_costs_usd))
    return max(1, math.floor(available / distance)) if available > 0 else 1


def build_proposal(proposal_id: str, request: TicketRequest, terms: RiskTerms,
                   observation: MarketObservation, book: ContractBook | None, now: datetime) -> TradeProposal:
    if request.structure == "shares":
        leg = request.legs[0]
        if leg.symbol != terms.symbol:
            raise TicketError("Share leg does not match the signal's instrument")
        qty = leg.qty if request.sizing_mode == "selected_quantity" else share_ceiling(request, terms)
        legs = [Leg(symbol=terms.symbol, side="buy", quantity_unit="share", qty=qty,
                    limit_price=float(leg.limit_price))]
    else:
        legs = []
        for leg in request.legs:
            try:
                key = canonical_symbol(leg.symbol)
            except InstrumentError as exc:
                raise TicketError("Malformed option symbol") from exc
            legs.append(Leg(symbol=leg.symbol, side=leg.side, quantity_unit="contract", qty=leg.qty,
                            limit_price=float(leg.limit_price), expiry=_expiry(book, leg.symbol),
                            open_interest=observation.open_interest.get(key)))
    fields = dict(
        event_id=request.event_id, stop_price=terms.stop, target_price=terms.target,
        sizing_mode=request.sizing_mode, proposal_id=proposal_id, plan_id=request.plan_id,
        setup_id=terms.setup_id, setup_version=terms.setup_version, quote_source=request.quote_source,
        stop_estimate_source=request.stop_estimate_source, tier=request.tier, grade=request.grade,
        risk_usd=float(request.budget_usd), instrument=terms.symbol, structure=request.structure,
        legs=legs, est_costs_usd=float(request.est_costs_usd), sector=request.sector,
        option_spread_pct_mid=observation.option_spread_pct_mid,
        already_moved_pct=observation.already_moved_pct, quote_as_of=observation.quote_as_of,
        security_tradable=observation.security_tradable, time_stop=request.time_stop,
        exit_rules=list(request.exit_rules))
    # The legacy worst-case field is an assertion; fill it with the same deterministic
    # calculation, or a placeholder the risk layer will reject as inconsistent.
    draft = TradeProposal(worst_case_loss_usd=max(1.0, sum(l.qty * l.limit_price for l in legs)), **fields)
    try:
        worst = float(loss_measures(draft, book, now).maximum_loss)
    except (InstrumentError, ValidationError):
        return draft
    return TradeProposal(worst_case_loss_usd=worst, **fields)


def binding(ticket_id: str, version: int, request: TicketRequest, proposal: TradeProposal | None,
            decision: RiskDecision | None, terms: RiskTerms | None, book: ContractBook | None) -> dict:
    """Executable terms and evidence versions. Receipt timestamps are excluded."""
    d = decision
    legs = []
    for i, leg in enumerate(request.legs):
        built = proposal.legs[i] if proposal else None
        legs.append({
            "symbol": leg.symbol, "side": leg.side, "limit_price": str(leg.limit_price),
            "requested_qty": leg.qty, "ceiling_qty": built.qty if built else None,
            "final_qty": d.final_leg_quantities[i] if d and d.final_leg_quantities else 0,
            "contract_id": _contract_id(book, leg.symbol) if request.structure != "shares" else None,
            "expiry": built.expiry.isoformat() if built and built.expiry else None,
        })
    return {
        "ticket_id": ticket_id, "version": version,
        "account_id": request.account_id, "environment": request.environment,
        "signal": {"event_id": request.event_id, "event_digest": terms.event_digest if terms else None,
                   "setup_id": terms.setup_id if terms else None,
                   "setup_version": terms.setup_version if terms else None,
                   "direction": terms.direction if terms else None},
        "instrument": terms.symbol if terms else None, "structure": request.structure, "legs": legs,
        "stop": terms.stop if terms else None, "target": terms.target if terms else None,
        "exits": {"time_stop": request.time_stop.isoformat(), "rules": list(request.exit_rules)},
        "budget_usd": f"{request.budget_usd:.2f}", "sizing_mode": request.sizing_mode,
        "est_costs_usd": f"{request.est_costs_usd:.2f}",
        "eligible": bool(d and d.approved),
        "disclosures": {k: getattr(d, k) if d else None for k in (
            "estimated_stop_loss_usd", "estimated_total_risk_usd", "requested_stop_loss_usd",
            "stop_loss_basis", "computed_max_loss_usd", "requested_max_loss_usd", "net_premium_usd",
            "position_value_usd", "estimated_funding_usd", "broker_requirement_verified", "loss_basis",
            "contract_metadata_source")},
        "warnings": sorted(({"code": w.code, "message": w.message, "value": w.value,
                             "threshold": w.threshold} for w in (d.warnings if d else [])),
                           key=lambda w: (w["code"], w["value"])),
        "failed_checks": sorted(c.rule for c in (d.checks if d else []) if not c.passed),
    }


def ack_token(binding_sha256: str, warning: dict) -> str:
    """Per-version, per-value token; copying it to another ticket cannot match."""
    digest = _sha({"binding": binding_sha256, "code": warning["code"], "message": warning["message"],
                   "value": warning["value"], "threshold": warning["threshold"]})
    return f"{warning['code']}:{digest[:10]}"


_AMOUNT = re.compile(r"\$?(\d{1,3}(,\d{3})+|\d+)(\.\d{1,2})?")


def parse_amount(text: str | None) -> Decimal | None:
    """Typed dollars: '25000', '25,000', '$25,000.00'. Anything else is no confirmation."""
    if text is None:
        return None
    text = text.strip()
    if not _AMOUNT.fullmatch(text):
        return None
    try:
        return Decimal(text.lstrip("$").replace(",", ""))
    except InvalidOperation:
        return None


def recheck(ticket_id: str, version: int, request: TicketRequest, inputs: RiskInputs,
            now: datetime) -> Check:
    """Independent rerun from the stored request and fresh trusted evidence."""
    problems: list[str] = []
    terms = book = proposal = decision = None
    try:
        revision, account = inputs.risk_state.load(request.account_id)
    except (RiskStateError, ValidationError, ValueError) as exc:
        return Check(None, None, None, binding(ticket_id, version, request, None, None, None, None),
                     (f"account snapshot unavailable ({type(exc).__name__})",), None)
    try:
        terms = RiskTerms.model_validate(inputs.terms_source.resolve(request.event_id, now).model_dump())
        book = inputs.contract_book(now)
        observation = MarketObservation.model_validate(
            inputs.observe(terms.symbol, tuple(l.symbol for l in request.legs), now).model_dump())
        proposal = build_proposal(f"{ticket_id}:v{version}", request, terms, observation, book, now)
        decision = evaluate(proposal, account, inputs.limits, now, live=request.environment == "live",
                            contract_book=book, registry=inputs.registry, market=inputs.market(now),
                            terms_source=_TermsSnapshot(terms))
    except Exception as exc:  # provider faults are isolated; raw responses are never shown
        problems.append(f"independent evidence unavailable or invalid ({type(exc).__name__})")
    if decision is not None and not decision.approved:
        problems.append("blocking checks failed: " + ", ".join(sorted(c.rule for c in decision.checks if not c.passed)))
    if now >= request.time_stop:
        problems.append("time stop has passed; no new entry")
    bound = json.loads(_json(binding(ticket_id, version, request, proposal, decision, terms, book)))
    return Check(proposal, decision, terms, bound, tuple(problems), book, revision)


class TicketStore:
    """SQLite ticket versions, approvals, consumptions and an append-only audit."""

    def __init__(self, path: Path, *, approval_lifetime: timedelta = DEFAULT_APPROVAL_LIFETIME):
        if not timedelta(seconds=1) <= approval_lifetime <= MAX_APPROVAL_LIFETIME:
            raise TicketError("Approval lifetime must be between 1 second and 1 hour")
        self.path = Path(path)
        self.lifetime = approval_lifetime
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, valid_until TEXT NOT NULL, request TEXT NOT NULL,
                    proposal TEXT, decision TEXT, terms TEXT, binding TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL, approval_id TEXT,
                    PRIMARY KEY(ticket_id, version));
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, version INTEGER NOT NULL,
                    record TEXT NOT NULL, record_sha256 TEXT NOT NULL, expires_at TEXT NOT NULL,
                    consumed_request TEXT, consumed_at TEXT, consumption TEXT);
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, ticket_id TEXT NOT NULL,
                    version INTEGER, event TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS audit_append_only_update BEFORE UPDATE ON audit
                    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_append_only_delete BEFORE DELETE ON audit
                    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
            """)
            row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA,))
            elif row[0] != SCHEMA:
                raise TicketError("Unsupported ticket database schema")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _tx(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @contextmanager
    def _final_tx(self, inputs: RiskInputs, account_id: str):
        """Ticket write lock plus a held account revision, so a manual stop or new
        snapshot either shows here as a changed revision or commits after this write."""
        with inputs.risk_state.held_revision(account_id) as revision, self._tx() as db:
            yield db, revision

    @staticmethod
    def _audit(db, now, ticket_id, version, event, actor, payload):
        db.execute("INSERT INTO audit(at,ticket_id,version,event,actor,payload) VALUES (?,?,?,?,?,?)",
                   (now.isoformat(), ticket_id, version, event, actor, _json(payload)))

    def _refused(self, now, ticket_id, version, event, actor, reason):
        with self._tx() as db:
            self._audit(db, now, ticket_id, version, event, actor, {"reason": reason})
        raise TicketError(reason)

    def _row(self, db, ticket_id, version):
        if version is None:
            row = db.execute("SELECT * FROM tickets WHERE ticket_id=? ORDER BY version DESC LIMIT 1",
                             (ticket_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM tickets WHERE ticket_id=? AND version=?",
                             (ticket_id, version)).fetchone()
        if row is None:
            raise TicketError("Unknown ticket or version")
        return row

    def _expire(self, db, row, now):
        state = row["state"]
        if state in {"pending", "blocked"} and now >= datetime.fromisoformat(row["valid_until"]):
            new = "expired"
        elif state == "approved":
            approval = db.execute("SELECT expires_at FROM approvals WHERE approval_id=?",
                                  (row["approval_id"],)).fetchone()
            new = "expired" if approval is None or now >= datetime.fromisoformat(approval["expires_at"]) else None
        else:
            new = None
        if new:
            db.execute("UPDATE tickets SET state=? WHERE ticket_id=? AND version=? AND state=?",
                       (new, row["ticket_id"], row["version"], state))
            self._audit(db, now, row["ticket_id"], row["version"], "expired", "system",
                        {"from": state, "reason": "approval or evidence validity ended"})
            row = self._row(db, row["ticket_id"], row["version"])
        return row

    # ---- prepare / revise ------------------------------------------------------
    def prepare(self, request: TicketRequest, inputs: RiskInputs, *, now: datetime,
                ticket_id: str | None = None) -> tuple[str, int]:
        now = _aware(now)
        request = TicketRequest.model_validate(request.model_dump())
        ticket_id = ticket_id or "T-" + uuid.uuid4().hex[:12]
        with self._db() as db:
            last = db.execute("SELECT MAX(version) FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()[0]
        version = (last or 0) + 1
        result = recheck(ticket_id, version, request, inputs, now)
        d = result.decision
        valid = d.signal_event_valid_until if d and d.signal_event_valid_until else now + self.lifetime
        state = "pending" if d is not None and d.approved and not result.problems else "blocked"
        digest = _sha(result.binding)
        with self._tx() as db:
            current = db.execute("SELECT MAX(version) FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()[0]
            if (current or 0) + 1 != version:
                raise TicketError("Ticket changed concurrently; prepare again")
            for old in db.execute("SELECT version,state FROM tickets WHERE ticket_id=? AND state IN "
                                  "('pending','blocked','approved')", (ticket_id,)).fetchall():
                db.execute("UPDATE tickets SET state='superseded' WHERE ticket_id=? AND version=?",
                           (ticket_id, old["version"]))
                self._audit(db, now, ticket_id, old["version"], "superseded", "system",
                            {"from": old["state"], "by_version": version})
            db.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)", (
                ticket_id, version, state, now.isoformat(), valid.isoformat(), request.model_dump_json(),
                result.proposal.model_dump_json() if result.proposal else None,
                d.model_dump_json() if d else None,
                result.terms.model_dump_json() if result.terms else None,
                _json(result.binding), digest))
            self._audit(db, now, ticket_id, version, "prepared", "system",
                        {"state": state, "binding_sha256": digest, "problems": list(result.problems)})
        return ticket_id, version

    def revise(self, ticket_id: str, inputs: RiskInputs, *, now: datetime, **changes) -> tuple[str, int]:
        """A changed budget, quantity, account, leg, price, cost or exit is a new version."""
        with self._db() as db:
            latest = TicketRequest.model_validate_json(self._row(db, ticket_id, None)["request"])
        data = latest.model_dump()
        data.update(changes)
        return self.prepare(TicketRequest.model_validate(data), inputs, now=now, ticket_id=ticket_id)

    # ---- reading ---------------------------------------------------------------
    def get(self, ticket_id: str, version: int | None = None, *, now: datetime) -> dict:
        now = _aware(now)
        with self._tx() as db:
            row = self._expire(db, self._row(db, ticket_id, version), now)
            approval = db.execute("SELECT * FROM approvals WHERE approval_id=?", (row["approval_id"],)).fetchone() \
                if row["approval_id"] else None
        binding_ = json.loads(row["binding"])
        return {
            "ticket_id": row["ticket_id"], "version": row["version"], "state": row["state"],
            "created_at": row["created_at"], "valid_until": row["valid_until"],
            "request": json.loads(row["request"]), "binding": binding_, "binding_sha256": row["binding_sha256"],
            "proposal": json.loads(row["proposal"]) if row["proposal"] else None,
            "decision": json.loads(row["decision"]) if row["decision"] else None,
            "terms": json.loads(row["terms"]) if row["terms"] else None,
            "acknowledgement_tokens": [ack_token(row["binding_sha256"], w) for w in binding_["warnings"]],
            "approval": json.loads(approval["record"]) if approval else None,
            "consumption": json.loads(approval["consumption"]) if approval and approval["consumption"] else None,
        }

    def history(self, ticket_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM audit WHERE ticket_id=? ORDER BY sequence", (ticket_id,)).fetchall()
        if not rows:
            raise TicketError("Unknown ticket")
        return [{"at": r["at"], "version": r["version"], "event": r["event"], "actor": r["actor"],
                 "payload": json.loads(r["payload"])} for r in rows]

    def tickets(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT ticket_id,version,state,created_at FROM tickets ORDER BY created_at,ticket_id,version").fetchall()
        return [dict(r) for r in rows]

    def display(self, ticket_id: str, version: int | None = None, *, now: datetime) -> str:
        return render(self.get(ticket_id, version, now=now))

    # ---- human decisions -------------------------------------------------------
    @staticmethod
    def _actor(actor: str, channel: str):
        if not actor or not actor.strip() or channel not in {"terminal", "automated_fixture"}:
            raise TicketError("A named actor and a known channel are required")
        if (channel == "automated_fixture") != actor.startswith("fixture:"):
            raise TicketError("Automated fixtures must be labelled 'fixture:'; terminal actors must not be")

    def approve(self, ticket_id: str, version: int, *, budget_confirmation: str | None,
                acknowledgements: Iterable[str], actor: str, channel: Literal["terminal", "automated_fixture"],
                inputs: RiskInputs, now: datetime, terminal_user: str | None = None) -> ApprovalRecord:
        now = _aware(now)
        self._actor(actor, channel)
        acknowledgements = tuple(acknowledgements)
        with self._tx() as db:
            row = self._expire(db, self._row(db, ticket_id, version), now)
        if row["state"] != "pending":
            reason = {"blocked": "Ticket failed blocking checks and cannot be approved",
                      "approved": "Ticket version is already approved"}.get(
                row["state"], f"Ticket version is {row['state']}; it cannot be approved")
            self._refused(now, ticket_id, version, "approval_refused", actor, reason)
        stored = json.loads(row["binding"])
        if _sha(stored) != row["binding_sha256"]:
            self._refused(now, ticket_id, version, "approval_refused", actor, "Stored ticket record is inconsistent")
        request = TicketRequest.model_validate_json(row["request"])
        result = recheck(ticket_id, version, request, inputs, now)
        if result.problems:
            self._refused(now, ticket_id, version, "approval_refused", actor,
                          "Recheck failed: " + "; ".join(result.problems))
        if result.binding != stored:
            self._refused(now, ticket_id, version, "approval_refused", actor,
                          "Ticket terms or warnings changed since this version was prepared; prepare a new version")
        typed = parse_amount(budget_confirmation)
        if typed is None or typed != request.budget_usd:
            self._refused(now, ticket_id, version, "approval_refused", actor,
                          f"Budget confirmation does not match the ticket budget of {usd(request.budget_usd)}")
        expected = [ack_token(row["binding_sha256"], w) for w in stored["warnings"]]
        if len(set(acknowledgements)) != len(acknowledgements) or set(acknowledgements) != set(expected):
            self._refused(now, ticket_id, version, "approval_refused", actor,
                          "Acknowledge exactly the warnings displayed on this ticket version: "
                          + (", ".join(expected) or "none"))
        d = result.decision
        deadlines = [now + self.lifetime, request.time_stop]
        if d.signal_event_valid_until:
            deadlines.append(d.signal_event_valid_until)
        if d.contract_metadata_as_of:
            deadlines.append(d.contract_metadata_as_of + inputs.limits.max_contract_metadata_age)
        expires = min(deadlines)
        if expires <= now:
            self._refused(now, ticket_id, version, "approval_refused", actor, "Evidence validity has already ended")
        approval_id = "A-" + uuid.uuid4().hex[:16]
        record = ApprovalRecord(
            schema_version=2, approval_id=approval_id, ticket_id=ticket_id, ticket_version=version,
            decision="approve", actor=actor, channel=channel, terminal_user=terminal_user, decided_at=now,
            expires_at=expires, budget_confirmed_usd=f"{typed:.2f}", acknowledgements=tuple(sorted(acknowledgements)),
            snapshot=stored, snapshot_sha256=row["binding_sha256"])
        payload = record.model_dump(mode="json")
        refused = None
        try:
            with self._final_tx(inputs, request.account_id) as (db, account_revision):
                at = final_clock(inputs, now)
                current = db.execute("SELECT state,binding_sha256 FROM tickets WHERE ticket_id=? AND version=?",
                                     (ticket_id, version)).fetchone()
                if current["state"] != "pending" or current["binding_sha256"] != row["binding_sha256"]:
                    refused = "Ticket changed during approval; nothing was approved"
                elif account_revision != result.account_revision:
                    refused = ACCOUNT_CHANGED + "; nothing was approved"
                elif at >= expires:
                    refused = "Evidence validity ended during the approval check; nothing was approved"
                if refused:
                    self._audit(db, at, ticket_id, version, "approval_refused", actor, {"reason": refused})
                else:
                    db.execute("INSERT INTO approvals(approval_id,ticket_id,version,record,record_sha256,expires_at) "
                               "VALUES (?,?,?,?,?,?)", (approval_id, ticket_id, version, _json(payload),
                                                        _sha(payload), expires.isoformat()))
                    db.execute("UPDATE tickets SET state='approved', approval_id=? WHERE ticket_id=? AND version=?",
                               (approval_id, ticket_id, version))
                    self._audit(db, now, ticket_id, version, "approved", actor, {
                        "approval_id": approval_id, "channel": channel, "terminal_user": terminal_user,
                        "expires_at": expires.isoformat(), "budget_confirmed_usd": f"{typed:.2f}",
                        "acknowledgements": sorted(acknowledgements), "binding_sha256": row["binding_sha256"]})
        except RiskStateError as exc:
            self._refused(now, ticket_id, version, "approval_refused", actor,
                          f"Account snapshot unavailable at the final check ({type(exc).__name__})")
        if refused:
            raise TicketError(refused)
        return record

    def reject(self, ticket_id: str, version: int, *, actor: str, reason: str,
               channel: Literal["terminal", "automated_fixture"], now: datetime) -> None:
        self._decide(ticket_id, version, actor=actor, reason=reason, channel=channel, now=now,
                     allowed={"pending", "blocked"}, new="rejected")

    def revoke(self, ticket_id: str, version: int, *, actor: str, reason: str,
               channel: Literal["terminal", "automated_fixture"], now: datetime) -> None:
        self._decide(ticket_id, version, actor=actor, reason=reason, channel=channel, now=now,
                     allowed={"approved"}, new="revoked")

    def _decide(self, ticket_id, version, *, actor, reason, channel, now, allowed, new):
        now = _aware(now)
        self._actor(actor, channel)
        if not reason or not reason.strip():
            raise TicketError("A reason is required")
        with self._tx() as db:
            row = self._expire(db, self._row(db, ticket_id, version), now)
            if row["state"] not in allowed:
                raise TicketError(f"Ticket version is {row['state']}; cannot mark it {new}")
            db.execute("UPDATE tickets SET state=? WHERE ticket_id=? AND version=? AND state=?",
                       (new, ticket_id, version, row["state"]))
            self._audit(db, now, ticket_id, version, new, actor,
                        {"reason": reason, "channel": channel, "from": row["state"]})

    # ---- local single-use consumption -------------------------------------------
    def consume(self, ticket_id: str, version: int, *, request_id: str, inputs: RiskInputs,
                now: datetime) -> dict:
        """Validate and atomically use an approval once. Performs NO broker action."""
        now = _aware(now)
        if not request_id or not request_id.strip():
            raise TicketError("A consumption request id is required")
        actor = "local-consumer"
        with self._tx() as db:
            row = self._expire(db, self._row(db, ticket_id, version), now)
            approval = db.execute("SELECT * FROM approvals WHERE approval_id=?", (row["approval_id"],)).fetchone() \
                if row["approval_id"] else None
        if row["state"] == "consumed" and approval is not None:
            if approval["consumed_request"] == request_id:
                return {**json.loads(approval["consumption"]), "replay": True}
            self._refused(now, ticket_id, version, "consumption_refused", actor,
                          "Approval was already consumed by another request")
        if row["state"] != "approved" or approval is None:
            self._refused(now, ticket_id, version, "consumption_refused", actor,
                          f"Ticket version is {row['state']}; there is no usable approval")
        try:
            record = ApprovalRecord.model_validate_json(approval["record"])
            stored = json.loads(row["binding"])
            valid = (_sha(json.loads(approval["record"])) == approval["record_sha256"]
                     and record.approval_id == approval["approval_id"] == row["approval_id"]
                     and (record.ticket_id, record.ticket_version) == (ticket_id, version)
                     and record.snapshot == stored and record.snapshot_sha256 == row["binding_sha256"] == _sha(stored)
                     and record.expires_at.isoformat() == approval["expires_at"])
        except (ValidationError, ValueError, TypeError):
            valid = False
        if not valid:
            self._refused(now, ticket_id, version, "consumption_refused", actor,
                          "Approval record is unknown, legacy or does not match this ticket version")
        request = TicketRequest.model_validate_json(row["request"])
        if now >= request.time_stop:
            self._refused(now, ticket_id, version, "consumption_refused", actor, TIME_STOP_PASSED)
        if now >= record.expires_at:
            self._refused(now, ticket_id, version, "consumption_refused", actor, "Approval has expired")
        result = recheck(ticket_id, version, request, inputs, now)
        if result.problems:
            self._refused(now, ticket_id, version, "consumption_refused", actor,
                          "Recheck failed: " + "; ".join(result.problems))
        if result.binding != stored:
            self._refused(now, ticket_id, version, "consumption_refused", actor,
                          "Current terms, evidence or warnings differ from the approved ticket")
        permission = _sha({"approval": record.approval_id, "request": request_id})[:16]
        outcome = {
            "ticket_id": ticket_id, "version": version, "approval_id": record.approval_id,
            "request_id": request_id, "permission_id": permission, "consumed_at": now.isoformat(),
            "final_leg_quantities": result.decision.final_leg_quantities, "replay": False,
            "broker_action": "none", "order_submitted": False,
            "note": "Local single-use consumption only. No order was submitted, routed or filled.",
        }
        refused = None
        try:
            with self._final_tx(inputs, request.account_id) as (db, account_revision):
                at = final_clock(inputs, now)  # a slow check cannot reuse its start time
                current = db.execute("SELECT state,approval_id FROM tickets WHERE ticket_id=? AND version=?",
                                     (ticket_id, version)).fetchone()
                used = db.execute("SELECT consumed_request,consumption FROM approvals WHERE approval_id=?",
                                  (record.approval_id,)).fetchone()
                if current["state"] == "consumed" and used["consumed_request"] == request_id:
                    return {**json.loads(used["consumption"]), "replay": True}
                if current["state"] != "approved" or current["approval_id"] != record.approval_id:
                    refused = f"Ticket version is {current['state']}; there is no usable approval"
                elif at >= request.time_stop:
                    refused = TIME_STOP_PASSED
                elif at >= record.expires_at:
                    refused = "Approval has expired"
                elif account_revision != result.account_revision:
                    refused = ACCOUNT_CHANGED + "; nothing was consumed"
                if refused:
                    self._audit(db, at, ticket_id, version, "consumption_refused", actor, {"reason": refused})
                else:
                    outcome["consumed_at"] = at.isoformat()
                    db.execute("UPDATE tickets SET state='consumed' WHERE ticket_id=? AND version=? "
                               "AND state='approved'", (ticket_id, version))
                    db.execute("UPDATE approvals SET consumed_request=?, consumed_at=?, consumption=? "
                               "WHERE approval_id=?", (request_id, at.isoformat(), _json(outcome), record.approval_id))
                    self._audit(db, at, ticket_id, version, "consumed", actor,
                                {"request_id": request_id, "permission_id": permission, "order_submitted": False})
        except RiskStateError as exc:
            self._refused(now, ticket_id, version, "consumption_refused", actor,
                          f"Account snapshot unavailable at the final check ({type(exc).__name__})")
        if refused:
            raise TicketError(refused)
        return outcome


# ---- plain-language display ----------------------------------------------------------
SIZING_TEXT = {
    "stop_budget": "Stop budget: the desk sized the quantity so the estimated loss at the stop plus costs fits your budget.",
    "selected_quantity": "Selected quantity: you chose the quantity. Your budget does not cap it; any mismatch is a warning you must acknowledge.",
    "maximum_loss_budget": "Maximum-loss budget: the desk sized whole strategies so the full strategy loss plus costs fits your budget.",
}
WARNING_TEXT = {
    "daily_loss": "Today's account loss is at or above your daily warning level.",
    "weekly_loss": "This week's account loss is at or above your weekly warning level.",
    "account_drawdown": "The account is down from its high by at least your drawdown warning level.",
    "mixed_market": "The market filter reads mixed (HALF). Your budget is unchanged.",
    "bearish_market": "The market filter reads bearish (no new longs) and this trade is long or neutral.",
    "stop_estimate_above_budget": "The quantity you chose loses more than your budget if the stop fills.",
    "exposure_above_budget": "The full option strategy exposure plus costs is above your budget.",
}
MONEY_WARNINGS = {"daily_loss", "weekly_loss", "stop_estimate_above_budget", "exposure_above_budget"}


def _warning_values(w: dict) -> str:
    if w["code"] in MONEY_WARNINGS:
        return f"{usd(w['value'])} against {usd(w['threshold'])}"
    if w["code"] == "account_drawdown":
        return f"{w['value']:.2%} against {w['threshold']:.2%}"
    return "flag on"


def render(view: dict) -> str:
    b, req, d = view["binding"], view["request"], view["decision"] or {}
    disc = b["disclosures"]
    option = b["structure"] != "shares"
    setup = CARDS.get(b["signal"]["setup_id"] or "")
    state = view["state"]
    note = {"pending": "locally eligible; NOT approved", "blocked": "failed blocking checks; cannot be approved",
            "approved": "approved for one local consumption", "consumed": "approval used once"}.get(state, "not usable")
    out = [f"TICKET {view['ticket_id']} version {view['version']}  state: {state} ({note})",
           f"Account: {b['account_id']} ({b['environment']})",
           f"Signal: {setup.name if setup else 'unknown setup'} on {b['instrument'] or 'unknown'}"
           f" ({b['signal']['direction'] or 'unknown'}), event {b['signal']['event_id']}",
           f"Structure: {b['structure'].replace('_', ' ')}"]
    for leg in b["legs"]:
        ident = f" contract {leg['contract_id']}" if leg["contract_id"] else ""
        requested = leg["requested_qty"] if leg["requested_qty"] is not None else "none (sized from budget)"
        out.append(f"  {leg['side'].upper()} {leg['symbol']}{ident} limit {price(leg['limit_price'])}; "
                   f"requested quantity {requested}; final calculated quantity {leg['final_qty']}")
    out += [f"Sizing: {SIZING_TEXT[b['sizing_mode']]}",
            f"Budget entered: {usd(b['budget_usd'])}",
            f"Stop (structural, from the signal event): {price(b['stop']) if b['stop'] else 'unavailable'}",
            f"Target: {price(b['target']) if b['target'] else 'none set by the setup'}"]
    if not b["eligible"] and any(leg["ceiling_qty"] for leg in b["legs"]):
        ceiling = ", ".join(str(leg["ceiling_qty"]) for leg in b["legs"])
        at = []
        if disc["requested_stop_loss_usd"] is not None:
            at.append(f"stop loss {usd(disc['requested_stop_loss_usd'])} before costs")
        if disc["requested_max_loss_usd"] is not None:
            at.append(f"{'strategy exposure' if option else 'position value'} {usd(disc['requested_max_loss_usd'])}")
        out.append(f"Quantity considered before the failed checks: {ceiling}" + (f" ({'; '.join(at)})" if at else ""))
    if disc["estimated_stop_loss_usd"] is not None:
        out.append(f"Estimated stop loss: {usd(disc['estimated_stop_loss_usd'])} ({disc['stop_loss_basis']})")
    else:
        why = disc["stop_loss_basis"] if option else "unavailable until the ticket passes its checks"
        out.append(f"Estimated stop loss: unavailable ({why})")
    out.append(f"Reserved costs: {usd(b['est_costs_usd'])} (estimate, not broker-confirmed)")
    if disc["estimated_total_risk_usd"] is not None:
        out.append(f"Estimated stop loss including costs: {usd(disc['estimated_total_risk_usd'])}")
    else:
        out.append("Estimated stop loss including costs: unavailable")
    if option:
        if disc["computed_max_loss_usd"] is not None:
            out.append(f"Option strategy exposure: {usd(disc['computed_max_loss_usd'])} "
                       f"(intact-strategy loss before costs; {disc['loss_basis']})")
            premium = disc["net_premium_usd"]
            out.append(f"Net premium: {usd(abs(premium))} {'debit' if premium >= 0 else 'credit'}")
        elif disc["requested_max_loss_usd"] is not None:
            out.append(f"Option strategy exposure at requested quantity: {usd(disc['requested_max_loss_usd'])}")
    elif disc["position_value_usd"] is not None:
        out.append(f"Position value: {usd(disc['position_value_usd'])} (information, not a warning)")
    if disc["estimated_funding_usd"] is not None:
        out.append(f"Funding estimate: {usd(disc['estimated_funding_usd'])} local estimate; "
                   "NOT a broker-verified buying-power or margin requirement")
    out.append(f"Exits: time stop {b['exits']['time_stop']}; rules: {'; '.join(b['exits']['rules']) or 'none declared'}")
    out.append("Next earnings date, timeframe notes and option rationale: not supplied by this G4 ticket "
               "(Step 09/11-13 dependency)")
    if b["failed_checks"]:
        out.append("Blocking checks failed (no warning acknowledgement can override these): "
                   + ", ".join(b["failed_checks"]))
    if b["warnings"]:
        out.append("Warnings (approval needs each acknowledgement code typed exactly):")
        for i, (w, token) in enumerate(zip(b["warnings"], view["acknowledgement_tokens"]), 1):
            out.append(f"  {i}. {w['code']}: {WARNING_TEXT.get(w['code'], w['message'])} "
                       f"Value {_warning_values(w)}.")
            out.append(f"     acknowledgement code: {token}")
    else:
        out.append("Warnings: none")
    if state == "pending":
        out.append(f"To approve: python -m desk.tickets approve {view['ticket_id']} --version {view['version']} "
                   "(you will type the budget and each acknowledgement code)")
    out.append("This ticket is not an order. Approval allows one local consumption; no broker action exists in G4.")
    return "\n".join(out)


# ---- terminal interface -----------------------------------------------------------
def _load_inputs(spec: str | None) -> RiskInputs:
    if not spec:
        raise TicketError("No trusted adapters configured (--adapters module:factory or DESK_TICKET_ADAPTERS). "
                          "Live adapter wiring is Step 13/15/20 work.")
    module, _, name = spec.partition(":")
    inputs = getattr(importlib.import_module(module), name)()
    if not isinstance(inputs, RiskInputs):
        raise TicketError("Adapter factory must return RiskInputs")
    return inputs


def _lifetime(env) -> timedelta:
    raw = env.get("DESK_APPROVAL_LIFETIME_SECONDS")
    return timedelta(seconds=int(raw)) if raw else DEFAULT_APPROVAL_LIFETIME


def main(argv=None, *, stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    ap = argparse.ArgumentParser(prog="python -m desk.tickets", description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("DESK_TICKET_DB", "data/tickets.sqlite"))
    ap.add_argument("--adapters", default=os.environ.get("DESK_TICKET_ADAPTERS"))
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="prepare and display a ticket from a request JSON file")
    p.add_argument("request")
    p = sub.add_parser("revise", help="prepare a new version from a full request JSON file")
    p.add_argument("ticket"); p.add_argument("request")
    for name in ("show", "approve", "reject", "revoke", "consume", "history"):
        p = sub.add_parser(name)
        p.add_argument("ticket")
        if name != "history":
            p.add_argument("--version", type=int, required=name != "show")
        if name in {"approve", "reject", "revoke"}:
            p.add_argument("--actor", default=os.environ.get("DESK_APPROVER", "Taz"))
        if name in {"reject", "revoke"}:
            p.add_argument("--reason", required=True)
        if name == "consume":
            p.add_argument("--request-id", required=True)
    sub.add_parser("list")
    args = ap.parse_args(argv)
    write = lambda text: print(text, file=stdout)
    try:
        store = TicketStore(Path(args.db), approval_lifetime=_lifetime(os.environ))
        needs = {"prepare", "revise", "approve", "consume"}
        # One clock for every command: the adapters' clock when configured.
        inputs = _load_inputs(args.adapters) if args.command in needs or args.adapters else None
        clock = (inputs.clock if inputs and inputs.clock else None) or (lambda: datetime.now(timezone.utc))
        now = clock()
        if args.command in {"prepare", "revise"}:
            request = TicketRequest.model_validate_json(Path(args.request).read_text())
            tid, version = store.prepare(request, inputs, now=now,
                                         ticket_id=args.ticket if args.command == "revise" else None)
            write(store.display(tid, version, now=now))
        elif args.command == "show":
            write(store.display(args.ticket, args.version, now=now))
        elif args.command == "approve":
            view = store.get(args.ticket, args.version, now=now)
            write(render(view))
            write("Type the budget amount to confirm (separate from the amount you entered):")
            typed = stdin.readline().strip() or None
            acks = []
            for i, w in enumerate(view["binding"]["warnings"], 1):
                write(f"Type the acknowledgement code for warning {i} ({w['code']}):")
                acks.append(stdin.readline().strip())
            record = store.approve(args.ticket, args.version, budget_confirmation=typed, acknowledgements=acks,
                                   actor=args.actor, channel="terminal", inputs=inputs, now=clock(),
                                   terminal_user=getpass.getuser())
            write(f"APPROVED {record.ticket_id} version {record.ticket_version} by {record.actor} "
                  f"until {record.expires_at.isoformat()} (approval {record.approval_id}). Not an order.")
        elif args.command in {"reject", "revoke"}:
            getattr(store, args.command)(args.ticket, args.version, actor=args.actor, reason=args.reason,
                                         channel="terminal", now=now)
            write(f"{args.command.upper()}ED {args.ticket} version {args.version}".replace("EED", "ED"))
        elif args.command == "consume":
            write(_json(store.consume(args.ticket, args.version, request_id=args.request_id, inputs=inputs, now=now)))
        elif args.command == "history":
            for event in store.history(args.ticket):
                write(_json(event))
        elif args.command == "list":
            for t in store.tickets():
                write(f"{t['ticket_id']} v{t['version']} {t['state']} {t['created_at']}")
    except (TicketError, ValidationError, OSError, ValueError) as exc:
        write(f"REFUSED: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    # Run through the importable module so adapter factories share its classes.
    from desk.tickets import main as _main
    raise SystemExit(_main())
