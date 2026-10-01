"""Point-in-time reviewed earnings evidence. No provider-field guessing or orders."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from desk.calendar import clock
from desk.playbook.cards import CARDS
from desk.symbols import canonical_symbol

Text = Annotated[str, Field(min_length=1)]
Number = Annotated[Decimal, Field(allow_inf_nan=False)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Published(Record):
    source_ref: Text
    published_at: AwareDatetime | None = None
    published_on: date | None = None
    first_observed_at: AwareDatetime | None = None
    received_at: AwareDatetime

    @model_validator(mode="after")
    def time_order(self):
        if sum(v is not None for v in (self.published_at, self.published_on, self.first_observed_at)) != 1:
            raise ValueError("provide exactly one publication timestamp/date or first observation")
        earliest = self.published_at or self.first_observed_at or datetime.combine(self.published_on, time(), timezone(timedelta(hours=14)))
        if self.received_at < earliest:
            raise ValueError("receipt precedes possible publication")
        return self

    @property
    def known_published_by(self):
        if self.published_at is not None:
            return self.published_at
        if self.first_observed_at is not None:
            return self.first_observed_at  # availability bound, not a claimed publication time
        # Date-only evidence: latest possible local day end, or earlier actual receipt.
        # This is an availability upper bound, NOT a claimed exact publication time.
        end = datetime.combine(self.published_on + timedelta(days=1), time(), timezone(timedelta(hours=-12)))
        return min(end, self.received_at)


class Metric(Record):
    value: Number
    scale: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] = Decimal(1)
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    unit: Literal["currency", "currency/share"]
    accounting_basis: Literal["US_GAAP", "IFRS", "ADJUSTED"]
    adjustment_basis: Text | None = None
    definition: Literal["diluted_eps", "basic_eps", "total_revenue", "net_revenue"]
    share_basis: Text | None = None  # comparable split-adjusted per-share basis


    @model_validator(mode="after")
    def adjusted_definition(self):
        if self.accounting_basis == "ADJUSTED" and not self.adjustment_basis:
            raise ValueError("adjusted metrics require a documented adjustment basis")
        return self


class Quarter(Published):
    fiscal_year: Annotated[int, Field(ge=1900, le=2200, strict=True)]
    fiscal_quarter: Annotated[int, Field(ge=1, le=4, strict=True)]
    period_kind: Literal["quarter"]
    period_start: date
    period_end: date
    eps: Metric | None = None
    sales: Metric | None = None

    @model_validator(mode="after")
    def actual_period(self):
        publication_day = self.published_at.date() if self.published_at else self.published_on
        if publication_day is None or not self.period_start < self.period_end < publication_day:
            raise ValueError("reported quarter must end before publication")
        if self.eps and (self.eps.unit != "currency/share" or not self.eps.share_basis or self.eps.definition not in {"diluted_eps", "basic_eps"}):
            raise ValueError("EPS requires per-share units and explicit share basis")
        if self.sales and (self.sales.unit != "currency" or self.sales.definition not in {"total_revenue", "net_revenue"}):
            raise ValueError("sales require currency units")
        return self


class Catalyst(Published):
    entry_session: date
    kind: Literal["earnings", "other"]
    summary: Text
    relevance_ref: Text  # reviewed connection to this symbol/session, not inferred sentiment
    report_period_end: date | None = None


class EarningsDate(Published):
    fiscal_period: Text
    report_date: date | None = None
    range_start: date | None = None
    range_end: date | None = None
    confidence: Literal["estimated", "confirmed"]
    time_window: Literal["before_open", "after_close", "unknown"] = "unknown"

    @model_validator(mode="after")
    def date_precision(self):
        if self.report_date is not None:
            if self.range_start is not None or self.range_end is not None:
                raise ValueError("use an exact date or an estimated range")
        elif (self.range_start is None or self.range_end is None
              or self.range_start > self.range_end or self.confidence != "estimated"):
            raise ValueError("ordered date range must remain estimated")
        return self

    @property
    def bounds(self):
        return (self.report_date, self.report_date) if self.report_date else (self.range_start, self.range_end)


class EarningsEvidence(Record):
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,19}$")]
    security_id: Text
    review_ref: Text
    reviewed_at: AwareDatetime
    valid_until: AwareDatetime
    latest_fiscal_year: int | None = None
    latest_fiscal_quarter: int | None = None
    current: Quarter | None = None
    prior: Quarter | None = None
    period_comparability_ref: Text | None = None  # required if quarter durations differ
    catalysts: tuple[Catalyst, ...] = ()
    calendar: tuple[EarningsDate, ...] = ()
    calendar_source_issues: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def reviewed_window(self):
        if self.valid_until < self.reviewed_at:
            raise ValueError("review validity ends before review")
        records = [r for r in (self.current, self.prior, *self.catalysts, *self.calendar) if r]
        if any(r.received_at > self.reviewed_at for r in records):
            raise ValueError("review precedes source receipt")
        return self


def metric_growth(current, prior):
    if current is None or prior is None:
        return None, "metric missing"
    fields = ("currency", "unit", "accounting_basis", "adjustment_basis", "definition", "share_basis")
    if any(getattr(current, k) != getattr(prior, k) for k in fields):
        return None, "metric units/accounting/definition/share basis differ"
    old, new = prior.value * prior.scale, current.value * current.scale
    if old <= 0:
        return None, "nonpositive comparison value; percentage undefined"
    return (new - old) / old, None


def next_earnings(evidence, at):
    known = [r for r in evidence.calendar if r.known_published_by <= at and r.received_at <= at]
    upcoming = {r.fiscal_period for r in known if r.bounds[1] >= clock(at).date()}
    eligible = [r for r in known if r.fiscal_period in upcoming]
    if not eligible:
        if evidence.calendar_source_issues:
            return {"status": "UNAVAILABLE", "reason": "calendar source incomplete",
                    "source_issues": list(evidence.calendar_source_issues)}
        return {"status": "UNKNOWN", "reason": "no current supported future earnings date"}
    # Conflicting dates for a fiscal period cannot be resolved by choosing a convenient source.
    events = []
    for period in sorted({r.fiscal_period for r in eligible}):
        claims = [r for r in eligible if r.fiscal_period == period]
        start, end = max(r.bounds[0] for r in claims), min(r.bounds[1] for r in claims)
        if start > end or len({r.time_window for r in claims if r.time_window != "unknown"}) > 1:
            return {"status": "CONFLICT", "fiscal_period": period,
                    "source_refs": [r.source_ref for r in claims]}
        events.append((start, end, period, claims))
    start, end, period, claims = min(events, key=lambda r: (r[0], r[2]))
    result = {"status": "CONFIRMED" if any(r.confidence == "confirmed" for r in claims) else "ESTIMATED",
              "fiscal_period": period, "time_windows": sorted({r.time_window for r in claims}),
              "source_refs": [r.source_ref for r in claims]}
    if evidence.calendar_source_issues:
        result.update(coverage="PARTIAL", source_issues=list(evidence.calendar_source_issues))
    # Preserve every source interval; an intersection is not issuer confirmation.
    if all(r.report_date is not None for r in claims):
        result["date"] = start.isoformat()
    else:
        result.update(range_start=start.isoformat(), range_end=end.isoformat(),
                      claims=[r.model_dump(mode="json") for r in claims])
        if any(r.report_date is not None for r in claims):
            result["date"] = start.isoformat()
    return result


def evaluate(setup_id, symbol, security_id, evidence, at, *, trigger_at=None):
    """Evaluate now; a trigger's publication cutoff never moves forward at review."""
    card = CARDS[setup_id]
    at = clock(at).to_pydatetime()
    cutoff = clock(trigger_at).to_pydatetime() if trigger_at is not None else at
    result = {"status": "PENDING_EVIDENCE" if card.needs_earnings_numbers else "NOT_REQUIRED",
              "required": card.needs_earnings_numbers, "evaluated_at": at.isoformat(),
              "publication_cutoff": cutoff.isoformat(), "reasons": [],
              "next_earnings": {"status": "UNKNOWN"}}
    if evidence is None:
        result["reasons"] = ["reported earnings evidence unavailable"] if card.needs_earnings_numbers else []
        return result
    try:
        evidence = EarningsEvidence.model_validate(evidence)
    except ValueError:
        result["reasons"] = ["malformed earnings evidence"]
        return result
    if (evidence.symbol, evidence.security_id) != (canonical_symbol(symbol), security_id):
        result["reasons"] = ["earnings evidence identity mismatch"]
        return result
    if cutoff > at or not evidence.reviewed_at <= at <= evidence.valid_until:
        result["reasons"] = ["evidence review is stale or from the future"]
        return result
    encoded = evidence.model_dump_json()
    result.update(evidence_id=hashlib.sha256(encoded.encode()).hexdigest(),
                  evidence=evidence.model_dump(mode="json"), next_earnings=next_earnings(evidence, at))
    if not card.needs_earnings_numbers:
        return result
    current, prior = evidence.current, evidence.prior
    if not current or not prior:
        result["reasons"] = ["current and prior-year actual quarters required"]
        return result
    if (current.fiscal_year, current.fiscal_quarter) != (evidence.latest_fiscal_year, evidence.latest_fiscal_quarter):
        result["reasons"] = ["current result is not the reviewed latest reported quarter"]
        return result
    if current.fiscal_year != prior.fiscal_year + 1 or current.fiscal_quarter != prior.fiscal_quarter or current.period_start <= prior.period_end:
        result["reasons"] = ["fiscal quarters are not a matching year-on-year pair"]
        return result
    if current.period_end-current.period_start != prior.period_end-prior.period_start and not evidence.period_comparability_ref:
        result["reasons"] = ["different quarter durations need comparability evidence"]
        return result
    if any(q.known_published_by > cutoff or q.received_at > at for q in (current, prior)):
        result["reasons"] = ["reported results were unavailable at the evaluation/publication cutoff"]
        return result
    result["publication_bounds"] = {"current": current.known_published_by.isoformat(),
                                    "prior": prior.known_published_by.isoformat()}
    eps, eps_reason = metric_growth(current.eps, prior.eps)
    sales, sales_reason = metric_growth(current.sales, prior.sales)
    result["growth"] = {"eps": str(eps) if eps is not None else None,
                        "sales": str(sales) if sales is not None else None,
                        "eps_issue": eps_reason, "sales_issue": sales_reason}
    ep = setup_id == "5_qullamaggie_episodic_pivot"
    values = [eps, sales] if ep else [eps]
    threshold = Decimal(str(card.p("min_growth" if ep else "min_eps_growth")))
    passed = any(v is not None and v >= threshold for v in values)
    if not passed:
        result["status"] = "PENDING_EVIDENCE" if any(v is None for v in values) else "REJECTED"
        result["reasons"] = ["required comparable growth is unknown" if result["status"] == "PENDING_EVIDENCE" else "growth below card threshold"]
        return result
    if ep:
        catalysts = [c for c in evidence.catalysts if c.entry_session == clock(cutoff).date()
                     and c.known_published_by <= cutoff and c.received_at <= at
                     and (c.kind != "earnings" or c.report_period_end == current.period_end)]
        if not catalysts:
            result["reasons"] = ["no supported catalyst published by trigger for this entry session"]
            return result
        result["catalysts"] = [c.model_dump(mode="json") for c in catalysts]
        result["growth_group"] = "50%+" if max(v for v in values if v is not None) >= Decimal(str(card.p("strong_growth"))) else "25-50%"
    result["status"] = "QUALIFIED"
    return result


class ReviewedEarningsSource:
    """Opt-in local normalized evidence; reread every evaluation so revisions take effect."""
    def __init__(self, source, path):
        self.source, self.path = source, Path(path)

    def __getattr__(self, name):
        return getattr(self.source, name)

    def earnings_evidence(self, symbol):
        # Files are explicit trusted reviewer input, not raw API output or a news instruction.
        raw = json.loads(self.path.read_text())
        if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("securities"), list):
            raise ValueError("invalid evidence file")
        rows = [r for r in raw["securities"] if isinstance(r, dict) and r.get("symbol") == canonical_symbol(symbol)]
        if len(rows) != 1:
            raise ValueError("missing or ambiguous evidence identity")
        return EarningsEvidence.model_validate(rows[0])


def configured_source(source, env=None):
    env = os.environ if env is None else env
    path = env.get("DESK_EARNINGS_EVIDENCE")
    return ReviewedEarningsSource(source, path) if path else source


def qualify(source, signal, at, *, trigger_at=None):
    try:
        method = getattr(source, "earnings_evidence", None)
        evidence = method(signal.symbol) if method else None
        return evaluate(signal.setup_id, signal.symbol, (signal.price_basis or {}).get("security_id"),
                        evidence, at, trigger_at=trigger_at)
    except Exception:
        # External adapter errors may contain secrets. No raw error text in tickets/logs.
        result = evaluate(signal.setup_id, signal.symbol, None, None, at, trigger_at=trigger_at)
        result["reasons"] = ["earnings evidence source unavailable or malformed"]
        return result
