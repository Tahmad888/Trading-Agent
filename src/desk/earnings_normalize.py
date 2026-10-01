"""Normalize saved earnings observations under an explicit, bounded review.

No network calls, inferred catalysts, annual-minus-YTD EPS, or runner activation.
Only supported US-GAAP USD quarterly contexts are mapped automatically.
"""
import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from desk.earnings import EarningsDate, EarningsEvidence, Quarter, metric_growth
from desk.earnings_check import aware_time
from desk.earnings_sources import REVENUE_CONCEPTS, save
from desk.sec import SecData
from desk.symbols import canonical_symbol


class MappingError(ValueError):
    pass


class IntegrityError(MappingError):
    pass


class NeedsIssuerReview(MappingError):
    pass


def number(value):
    if value is None or value == "None" or isinstance(value, bool):
        raise MappingError("actual number missing")
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise MappingError("invalid actual number") from None


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise MappingError("timezone required")
    return result


def fiscal(row):
    year, quarter = row.get("fiscal_year"), row.get("fiscal_period")
    if type(year) is not int or type(quarter) is not int or not 1900 <= year <= 2200 or not 1 <= quarter <= 4:
        raise MappingError("invalid calendar fiscal period")
    return year, quarter


def records(observation):
    data = observation["payload"]
    # financial-alert was observed as an object; calendar as a list.
    rows = [data] if isinstance(data, dict) and data else data
    if rows == {}:
        rows = []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise MappingError("unsupported source envelope")
    return rows


def upcoming(observation, kind):
    """Calendar-only mapping. eps_est/eps_ly/rev_est/rev_ly are never consumed."""
    out = []
    receipt = timestamp(observation["received_at"])
    for row in records(observation):
        year, quarter = fiscal(row)
        fields = dict(fiscal_period=f"{year}Q{quarter}", confidence="estimated",
                      source_ref=f"webull:{observation['host']}:{kind}:{year}Q{quarter}",
                      received_at=receipt, first_observed_at=receipt)
        if kind == "financial_alert":
            fields.update(range_start=row["start_date"], range_end=row["end_date"])
        else:
            # A reported entry cannot masquerade as an upcoming release.
            if any(row.get(k) not in (None, "", "None") for k in ("eps_actual", "rev_actual")):
                continue
            fields["report_date"] = row["expected_publish_date"]
        out.append(EarningsDate(**fields))
    return out


def latest_actual(observation, at):
    candidates = []
    for row in records(observation):
        if not any(row.get(k) not in (None, "", "None") for k in ("eps_actual", "rev_actual")):
            continue
        period = fiscal(row)
        if row.get("currency") != "USD":
            raise MappingError("unsupported reported currency")
        if date.fromisoformat(row["expected_publish_date"]) > at.date():
            raise MappingError("future calendar row contains actuals")
        for key in ("eps_actual", "rev_actual"):
            if row.get(key) not in (None, "", "None"):
                number(row[key])
        candidates.append((period, row))
    if not candidates:
        raise MappingError("latest reported quarter unavailable; estimates are not actuals")
    period = max(p for p, _ in candidates)
    rows = [r for p, r in candidates if p == period]
    if len({tuple(str(r.get(k)) for k in ("eps_actual", "rev_actual", "expected_publish_date", "currency")) for r in rows}) != 1:
        raise MappingError("conflicting latest reported calendar rows")
    return rows[0]


def filings(observation, at):
    recent = observation["payload"]["filings"]["recent"]
    keys = ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form")
    if not all(isinstance(recent.get(k), list) for k in keys) or len({len(recent[k]) for k in keys}) != 1:
        raise MappingError("invalid filing columns")
    rows = [dict(zip(keys, values)) for values in zip(*(recent[k] for k in keys))]
    result = []
    for row in rows:
        if row["form"] not in {"10-Q", "10-Q/A", "10-K", "10-K/A"}:
            continue
        accepted = timestamp(row["acceptanceDateTime"])
        if accepted <= at:
            row["accepted"] = accepted
            row["end"] = date.fromisoformat(row["reportDate"])
            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", row["accessionNumber"]):
                raise MappingError("invalid filing accession")
            result.append(row)
    if not result:
        raise MappingError("supported financial filing unavailable")
    return result


def _quarter_rows(facts, concept, unit, filing):
    if not isinstance(facts, dict):
        raise MappingError("invalid taxonomy")
    item = facts.get(concept, {})
    if not isinstance(item, dict) or not isinstance(item.get("units", {}), dict):
        raise MappingError("invalid fact units")
    rows = item.get("units", {}).get(unit, [])
    if not isinstance(rows, list):
        raise MappingError("invalid fact rows")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise MappingError("invalid fact row")
        if row.get("accn") != filing["accessionNumber"]:
            continue
        if row.get("filed") != filing["filingDate"] or row.get("form") != filing["form"]:
            raise MappingError("fact filing provenance mismatch")
        if "start" not in row:
            continue  # Instant facts cannot be quarter results.
        start, end = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
        if 70 <= (end - start).days + 1 <= 110:
            out.append((start, end, number(row["val"]), row))
    return out


def _pair(facts, concept, unit, filing, label):
    rows = _quarter_rows(facts, concept, unit, filing)
    current = [r for r in rows if r[1] == filing["end"]]
    if not current:
        return None
    # These labels belong to the filing. Only the current context can anchor them.
    fy, q = label
    if any(r[3].get("fy") != fy or r[3].get("fp") != ("FY" if q == 4 else f"Q{q}") for r in current):
        raise MappingError("latest filing and reported calendar fiscal periods disagree")
    unique = {(r[0], r[1], r[2]) for r in current}
    if len(unique) != 1:
        raise MappingError("conflicting current quarter contexts")
    cur = next(iter(unique))
    prior = {(r[0], r[1], r[2]) for r in rows if 350 <= (cur[1] - r[1]).days <= 380}
    if not prior:
        return None
    if len(prior) != 1:
        raise MappingError("conflicting prior-year quarter contexts")
    old = next(iter(prior))
    if cur[1] - cur[0] != old[1] - old[0]:
        raise NeedsIssuerReview("different quarter durations require issuer comparability review")
    return cur, old


def sec_pair(facts_observation, submissions_observation, actual, at):
    label = fiscal(actual)
    candidates = filings(submissions_observation, at)
    # Do not skip a newer amendment just because its facts are inconvenient/missing.
    filing = max(candidates, key=lambda r: (r["end"], r["accepted"]))
    if date.fromisoformat(actual["expected_publish_date"]) > date.fromisoformat(filing["filingDate"]):
        raise NeedsIssuerReview("latest release is newer than available SEC filing; issuer evidence required")
    if filing["end"] >= date.fromisoformat(actual["expected_publish_date"]):
        raise MappingError("filing/report release period mismatch")
    gaap = facts_observation["payload"]["facts"].get("us-gaap", {})
    current_revenue_claims = {r[:3] for tag in REVENUE_CONCEPTS
                              for r in _quarter_rows(gaap, tag, "USD", filing) if r[1] == filing["end"]}
    if len(current_revenue_claims) > 1:
        raise MappingError("conflicting current-period revenue concepts")
    eps = _pair(gaap, "EarningsPerShareDiluted", "USD/shares", filing, label)
    revenues = [(tag, pair) for tag in REVENUE_CONCEPTS
                if (pair := _pair(gaap, tag, "USD", filing, label)) is not None]
    if len({pair for _, pair in revenues}) > 1:
        raise MappingError("conflicting current-period revenue concepts")
    sales = revenues[0][1] if revenues else None
    if eps is None and sales is None:
        if filing["form"].endswith("/A"):
            raise MappingError("new amendment lacks comparable facts; resolve amendment before fallback")
        raise NeedsIssuerReview("explicit comparable quarterly facts missing; issuer evidence required (no EPS subtraction)")
    if eps and sales and (eps[0][:2], eps[1][:2]) != (sales[0][:2], sales[1][:2]):
        raise MappingError("EPS and sales quarter contexts differ")
    for pair, key in ((eps, "eps_actual"), (sales, "rev_actual")):
        if pair and actual.get(key) not in (None, "", "None") and pair[0][2] != number(actual[key]):
            raise MappingError("SEC actuals and reported calendar disagree; basis review required")
    cik = str(int(facts_observation["payload"]["cik"]))
    accn = filing["accessionNumber"]
    ref = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{accn}-index.html"
    receipt = max(timestamp(facts_observation["received_at"]), timestamp(submissions_observation["received_at"]))
    basis = f"US-GAAP comparative diluted EPS as presented together in SEC {accn}"
    quarters = []
    for i, year in enumerate((label[0], label[0] - 1)):
        context = (eps or sales)[i]
        def metric(pair, revenue=False):
            if pair is None:
                return None
            return dict(value=pair[i][2], currency="USD", unit="currency" if revenue else "currency/share",
                        accounting_basis="US_GAAP", definition="total_revenue" if revenue else "diluted_eps",
                        share_basis=None if revenue else basis)
        quarters.append(Quarter(source_ref=ref, published_at=filing["accepted"], received_at=receipt,
                                fiscal_year=year, fiscal_quarter=label[1], period_kind="quarter",
                                period_start=context[0], period_end=context[1], eps=metric(eps), sales=metric(sales, True)))
    provenance = dict(accession=accn, filing_accepted_at=filing["accepted"].isoformat(),
                      eps_concept="EarningsPerShareDiluted" if eps else None,
                      revenue_concepts=[tag for tag, _ in revenues],
                      period_selection="explicit quarter contexts in same filing; comparative fy/fp/frame ignored")
    return quarters, provenance


class Archive:
    """Relocatable archive, verified against the collector's recorded byte hashes."""
    def __init__(self, directory, at):
        self.directory, self.at = Path(directory), at
        self.report = json.loads((self.directory / "report.json").read_text())
        if not isinstance(self.report, dict) or not isinstance(self.report.get("checks"), list) or not all(isinstance(r, dict) for r in self.report["checks"]):
            raise IntegrityError("invalid source report")
        self.used = []

    def _read(self, record):
        path = self.directory / Path(record["file"]).name
        if path.is_symlink() or path.resolve().parent != self.directory.resolve():
            raise IntegrityError("archive path outside run directory")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise IntegrityError("source archive hash mismatch")
        data = json.loads(raw)
        if not isinstance(data, dict) or "payload" not in data:
            raise IntegrityError("invalid observation wrapper")
        received = timestamp(data["received_at"])
        if received > self.at:
            raise IntegrityError("source receipt after review")
        if record.get("received_at") and timestamp(record["received_at"]) != received:
            raise IntegrityError("source receipt mismatch")
        self.used.append(dict(file=path.name, sha256=record["sha256"], received_at=data["received_at"]))
        return data

    def get(self, provider, symbol, kind):
        rows = [r for r in self.report["checks"] if (r.get("provider"), r.get("symbol"), r.get("kind")) == (provider, symbol, kind)]
        if len(rows) > 1:
            raise IntegrityError("duplicate source archive entries")
        if not rows or rows[0]["status"] not in {"OBSERVATIONS_ONLY", "EMPTY_UNVERIFIED"}:
            raise MappingError(f"{provider} {kind} unavailable")
        value = self._read(rows[0])
        if provider == "webull" and (value.get("symbol"), value.get("kind")) != (symbol, kind):
            raise IntegrityError("Webull observation identity mismatch")
        if provider == "webull" and value.get("host") not in {"api.webull.com", "api.sandbox.webull.com"}:
            raise IntegrityError("unsupported Webull observation host")
        if provider == "sec" and not isinstance(value["payload"], dict):
            raise IntegrityError("invalid SEC observation")
        return value

    def identity(self, symbol, cik, submissions, facts):
        index = self._read(self.report["sec_identity_index"])
        if not isinstance(index["payload"], dict):
            raise IntegrityError("invalid SEC identity index")
        if SecData.resolve(index, symbol) != cik:
            raise MappingError("review CIK and SEC ticker identity differ")
        for observed in (submissions, facts):
            if observed is not None and str(observed["payload"].get("cik", "")).lstrip("0") != cik.lstrip("0"):
                raise MappingError("SEC payload identity mismatch")
        if submissions is not None and symbol not in {canonical_symbol(s) for s in submissions["payload"].get("tickers", [])}:
            raise MappingError("SEC submissions ticker mismatch")


def normalize(directory, *, symbol, security_id, cik, review_ref, reviewed_at, valid_until, issuer=None):
    """Reviewer supplies identity and validity; mapping remains deterministic.

    Unknown source components stay explicit. An issuer fallback is an existing
    reviewed EarningsEvidence, not arbitrary raw text, forecast or synthesized EPS.
    """
    symbol = canonical_symbol(symbol)
    if not re.fullmatch(r"\d{10}", cik) or int(cik) == 0:
        raise MappingError("review requires ten-digit SEC CIK")
    archive = Archive(directory, reviewed_at)
    health, observations = {}, {}
    for provider, kinds in (("webull", ("financial_alert", "earnings_calendar", "quarterly_income")),
                            ("sec", ("submissions", "companyfacts"))):
        for kind in kinds:
            try:
                observations[kind] = archive.get(provider, symbol, kind)
                health[kind] = "EMPTY" if not observations[kind]["payload"] else "AVAILABLE"
            except IntegrityError:
                raise
            except (OSError, KeyError, ValueError, TypeError):
                health[kind] = "UNAVAILABLE"
    # Hash/identity failures cannot be papered over with an issuer fallback.
    archive.identity(symbol, cik, observations.get("submissions"), observations.get("companyfacts"))
    calendar, calendar_issues = [], []
    for kind in ("financial_alert", "earnings_calendar"):
        try:
            calendar.extend(upcoming(observations[kind], kind))
        except (KeyError, ValueError, TypeError):
            calendar_issues.append(f"{kind} unavailable or malformed")
    base = dict(symbol=symbol, security_id=security_id, review_ref=review_ref,
                reviewed_at=reviewed_at, valid_until=valid_until, calendar=calendar,
                calendar_source_issues=calendar_issues)
    issues, provenance, mode, fallback_allowed = [], {}, "SEC", False
    try:
        actual = latest_actual(observations["earnings_calendar"], reviewed_at)
        base.update(zip(("latest_fiscal_year", "latest_fiscal_quarter"), fiscal(actual)))
        pair, provenance = sec_pair(observations["companyfacts"], observations["submissions"], actual, reviewed_at)
        base.update(current=pair[0], prior=pair[1])
    except MappingError as exc:
        issues.append(str(exc))
        fallback_allowed = isinstance(exc, NeedsIssuerReview)
    except KeyError:
        issues.append("reported source missing or malformed")
        fallback_allowed = any(k not in observations for k in ("companyfacts", "submissions"))
    except (ValueError, TypeError):
        issues.append("reported source missing or malformed")
    partial_pair = "current" in base and any(getattr(base["current"], k) is None or getattr(base["prior"], k) is None
                                             for k in ("eps", "sales"))
    if partial_pair and issuer is not None and not issues:
        fallback_allowed = True
        issues.append("explicit quarterly metric missing from otherwise supported SEC pair")
    if issuer is not None and issues:
        if not fallback_allowed:
            raise MappingError("source conflict or malformed data requires resolution before issuer fallback")
        item = EarningsEvidence.model_validate(issuer)
        actual = latest_actual(observations["earnings_calendar"], reviewed_at)
        if (item.symbol, item.security_id) != (symbol, security_id) or not item.reviewed_at <= reviewed_at <= item.valid_until:
            raise MappingError("issuer review identity or validity mismatch")
        if (item.latest_fiscal_year, item.latest_fiscal_quarter) != fiscal(actual) or not item.current or not item.prior:
            raise MappingError("issuer fallback is not the latest reported quarter pair")
        # Require the same reported period, comparable units, and calendar cross-check.
        if (item.current.fiscal_year, item.current.fiscal_quarter) != fiscal(actual) or (
            item.prior.fiscal_year, item.prior.fiscal_quarter) != (item.current.fiscal_year - 1, item.current.fiscal_quarter):
            raise MappingError("issuer fallback quarter mismatch")
        if item.current.period_start <= item.prior.period_end or any(q.known_published_by > reviewed_at for q in (item.current, item.prior)):
            raise MappingError("issuer fallback period or publication mismatch")
        for name in ("current", "prior"):
            if name in base and (base[name].period_start, base[name].period_end) != (getattr(item, name).period_start, getattr(item, name).period_end):
                raise MappingError("issuer fallback conflicts with available SEC period")
        if any(not 70 <= (q.period_end - q.period_start).days + 1 <= 110 for q in (item.current, item.prior)):
            raise MappingError("issuer fallback must contain explicit supported quarterly periods")
        if not any(getattr(item.current, k) is not None for k in ("eps", "sales")):
            raise MappingError("issuer fallback metrics missing")
        if item.current.period_end - item.current.period_start != item.prior.period_end - item.prior.period_start and not item.period_comparability_ref:
            raise MappingError("issuer quarter comparability missing")
        for key, attr in (("eps_actual", "eps"), ("rev_actual", "sales")):
            metric, prior_metric = getattr(item.current, attr), getattr(item.prior, attr)
            if metric is not None:
                _, problem = metric_growth(metric, prior_metric)
                if problem and not problem.startswith("nonpositive"):
                    raise MappingError("issuer metric comparability missing")
                if metric.currency != "USD" or metric.accounting_basis != "US_GAAP" or metric.value * metric.scale != number(actual[key]):
                    raise MappingError("issuer/calendar actuals disagree")
                if attr == "eps" and metric.definition != "diluted_eps":
                    raise MappingError("issuer fallback requires diluted EPS")
            for name in ("current", "prior"):
                if name in base:
                    original, replacement = getattr(base[name], attr), getattr(getattr(item, name), attr)
                    if original is not None and (replacement is None or original.value * original.scale != replacement.value * replacement.scale
                                                 or original.definition != replacement.definition):
                        raise MappingError("issuer fallback conflicts with available SEC metric")
        base.update(current=item.current, prior=item.prior, period_comparability_ref=item.period_comparability_ref,
                    valid_until=min(valid_until, item.valid_until))
        provenance = {"issuer_review_ref": item.review_ref, "fallback_for": issues[:]}
        issues, mode = [], "REVIEWED_ISSUER"
        # Do not inherit a catalyst or future dates for a different observation window.
    evidence = EarningsEvidence(**base)
    return dict(purpose="reviewed earnings mapping; no chart/order activation", symbol=symbol,
                status="MAPPED" if not issues else "PENDING_EVIDENCE", mode=mode, issues=issues,
                calendar_issues=calendar_issues, source_health=health, provenance=provenance,
                metric_coverage={key: bool(evidence.current and evidence.prior and
                                           getattr(evidence.current, key) is not None and getattr(evidence.prior, key) is not None)
                                 for key in ("eps", "sales")},
                source_files=archive.used, evidence=evidence.model_dump(mode="json"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--security-id", required=True)
    parser.add_argument("--cik", required=True)
    parser.add_argument("--review-ref", required=True)
    parser.add_argument("--reviewed-at", required=True, type=aware_time)
    parser.add_argument("--valid-until", required=True, type=aware_time)
    parser.add_argument("--issuer-evidence", help="optional existing reviewed evidence file")
    parser.add_argument("--output", required=True, help="new diagnostic file; never overwrites")
    parser.add_argument("--evidence-output", help="optional new opt-in scanner evidence file")
    args = parser.parse_args(argv)
    try:
        paths = [Path(p) for p in (args.output, args.evidence_output) if p]
        if len({p.resolve() for p in paths}) != len(paths) or any(p.exists() for p in paths):
            raise MappingError("output must name distinct new files")
        issuer = None
        if args.issuer_evidence:
            from desk.earnings import ReviewedEarningsSource
            issuer = ReviewedEarningsSource(None, args.issuer_evidence).earnings_evidence(args.symbol)
        result = normalize(args.run_dir, symbol=args.symbol, security_id=args.security_id, cik=args.cik,
                           review_ref=args.review_ref, reviewed_at=args.reviewed_at, valid_until=args.valid_until, issuer=issuer)
        if args.evidence_output:
            save(Path(args.evidence_output), {"schema_version": 1, "securities": [result["evidence"]]})
        save(Path(args.output), result)
        public = {k: v for k, v in result.items() if k != "evidence"}
    except (OSError, ValueError, KeyError, TypeError):
        public = {"status": "UNAVAILABLE", "reason": "archive/review invalid or output unavailable"}
    print(json.dumps(public, indent=2))
    return 0 if public["status"] == "MAPPED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
