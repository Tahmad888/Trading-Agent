"""Synthetic archives exercise the eight observed failure modes; no live API calls."""
from copy import deepcopy
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from desk.earnings import EarningsDate, EarningsEvidence, evaluate, next_earnings
from desk.earnings_normalize import normalize, upcoming, sec_pair, latest_actual, main, MappingError, IntegrityError
from desk.earnings_sources import save, sec_summary
from tests.test_earnings import evidence, CUP

AT = datetime(2026, 10, 1, 20, tzinfo=timezone.utc)
ACCN = "0001045810-26-000075"


def observation(payload, **kwargs):
    return dict(payload=payload, received_at="2026-10-01T19:35:35Z", **kwargs)


def sample():
    sub = observation(dict(cik="0001045810", tickers=["NVDA"], filings={"recent": {
        "accessionNumber": [ACCN], "filingDate": ["2026-08-26"], "reportDate": ["2026-07-26"],
        "acceptanceDateTime": ["2026-08-26T20:30:00Z"], "form": ["10-Q"]}}))
    def row(start, end, val, **extra):
        return dict(start=start, end=end, val=val, accn=ACCN, filed="2026-08-26", form="10-Q", fy=2027, fp="Q2", **extra)
    eps = [row("2026-04-27", "2026-07-26", "2.46"), row("2025-04-28", "2025-07-27", "1.08"),
           row("2026-01-26", "2026-07-26", "4.85")]
    rev = [row("2026-04-27", "2026-07-26", "96221000000"), row("2025-04-28", "2025-07-27", "46743000000"),
           row("2026-01-26", "2026-07-26", "177837000000")]
    facts = observation(dict(cik=1045810, facts={"us-gaap": {
        "EarningsPerShareDiluted": {"units": {"USD/shares": eps}}, "Revenues": {"units": {"USD": rev}}}}))
    cal = observation([dict(fiscal_year=2027, fiscal_period=2, currency="USD", expected_publish_date="2026-08-26",
                            eps_actual="2.46", rev_actual="96221000000")], host="api.sandbox.webull.com")
    return facts, sub, cal


def archive(tmp_path, change=None):
    facts, sub, cal = sample()
    alert = observation(dict(fiscal_year=2027, fiscal_period=3, start_date="2026-11-17", end_date="2026-11-23",
                             eps_ly="1.30", eps_est="2.49", rev_ly="57006000000", rev_est="109010834070"),
                        host="api.sandbox.webull.com")
    obs = dict(companyfacts=facts, submissions=sub, earnings_calendar=cal, financial_alert=alert,
               quarterly_income=observation([], host="api.sandbox.webull.com"))
    if change:
        change(obs)
    report = dict(checks=[])
    for kind, value in obs.items():
        provider = "sec" if kind in ("submissions", "companyfacts") else "webull"
        if provider == "webull":
            value.update(symbol="NVDA", kind=kind)
        stored = save(tmp_path / f"{provider}-NVDA-{kind}.json", value)
        report["checks"].append(dict(provider=provider, symbol="NVDA", kind=kind, status="OBSERVATIONS_ONLY",
                                     received_at=value["received_at"], **stored))
    report["sec_identity_index"] = save(tmp_path / "sec-tickers.json", observation({"0": {"ticker": "NVDA", "cik_str": 1045810}}))
    save(tmp_path / "report.json", report)
    return tmp_path


def run(path, **kwargs):
    return normalize(path, symbol="NVDA", security_id="913257561", cik="0001045810", review_ref="synthetic mapping review",
                     reviewed_at=AT, valid_until=AT + timedelta(hours=1), **kwargs)


def test_empty_webull_income_uses_sec_with_same_filing_comparatives(tmp_path):
    result = run(archive(tmp_path))
    assert result["status"] == "MAPPED" and result["source_health"]["quarterly_income"] == "EMPTY"
    current, prior = result["evidence"]["current"], result["evidence"]["prior"]
    assert (current["fiscal_year"], prior["fiscal_year"]) == (2027, 2026)
    assert (current["eps"]["value"], prior["eps"]["value"]) == ("2.46", "1.08")
    assert current["published_at"] == "2026-08-26T20:30:00Z"
    output = evaluate(CUP, "NVDA", "913257561", result["evidence"], AT)
    assert output["status"] == "QUALIFIED"
    assert Decimal(output["growth"]["eps"]) == Decimal("1.38") / Decimal("1.08")
    assert output["next_earnings"]["range_start"] == "2026-11-17"
    assert "date" not in output["next_earnings"]
    assert output["next_earnings"]["status"] == "ESTIMATED"


def test_forecasts_and_upcoming_ly_never_change_reported_growth(tmp_path):
    def poison(obs):
        obs["financial_alert"]["payload"].update(eps_ly="0.00001", eps_est="99999", rev_ly="0", rev_est="999999999")
    result = run(archive(tmp_path, poison))
    assert result["evidence"]["prior"]["eps"]["value"] == "1.08"
    assert result["evidence"]["prior"]["sales"]["value"] == "46743000000"


def test_obsolete_revenue_tag_cannot_supplant_current_tag(tmp_path):
    def old(obs):
        gaap = obs["companyfacts"]["payload"]["facts"]["us-gaap"]
        stale = deepcopy(gaap["Revenues"])
        for r in stale["units"]["USD"]:
            r.update(accn="0001045810-22-000001", end="2022-07-31", start="2022-05-01")
        gaap["RevenueFromContractWithCustomerExcludingAssessedTax"] = stale
    result = run(archive(tmp_path, old))
    assert result["provenance"]["revenue_concepts"] == ["Revenues"]


@pytest.mark.parametrize("tag", ["Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax"])
def test_current_revenue_mapping_depends_on_context_not_concept_priority(tmp_path, tag):
    def change(obs):
        gaap = obs["companyfacts"]["payload"]["facts"]["us-gaap"]
        gaap[tag] = gaap.pop("Revenues")
    result = run(archive(tmp_path, change))
    assert result["status"] == "MAPPED" and result["provenance"]["revenue_concepts"] == [tag]


@pytest.mark.parametrize("fault", ["current_conflict", "prior_conflict", "revenue_conflict", "context_mismatch", "wrong_filing_label", "different_duration", "wrong_actual", "new_release"])
def test_ambiguous_period_basis_and_conflicts_never_export_qualified_pair(tmp_path, fault):
    def change(obs):
        gaap = obs["companyfacts"]["payload"]["facts"]["us-gaap"]
        eps = gaap["EarningsPerShareDiluted"]["units"]["USD/shares"]
        if fault in {"current_conflict", "prior_conflict"}:
            eps.append({**eps[0 if fault == "current_conflict" else 1], "val": "88"})
        elif fault == "revenue_conflict":
            gaap["SalesRevenueNet"] = deepcopy(gaap["Revenues"])
            gaap["SalesRevenueNet"]["units"]["USD"][0]["val"] = "88"
        elif fault == "context_mismatch":
            for r in gaap["Revenues"]["units"]["USD"][:2]:
                r["start"] = str(date.fromisoformat(r["start"]) + timedelta(days=1))
        elif fault == "wrong_filing_label": eps[0]["fy"] = 2026
        elif fault == "different_duration": eps[1]["start"] = "2025-04-27"
        elif fault == "wrong_actual": obs["earnings_calendar"]["payload"][0]["eps_actual"] = "9"
        elif fault == "new_release": obs["earnings_calendar"]["payload"][0]["expected_publish_date"] = "2026-09-30"
    result = run(archive(tmp_path, change))
    assert result["status"] == "PENDING_EVIDENCE"
    assert result["evidence"]["current"] is None


def test_comparative_fiscal_labels_and_frames_do_not_change_pair(tmp_path):
    def change(obs):
        for concept in obs["companyfacts"]["payload"]["facts"]["us-gaap"].values():
            concept["units"][next(iter(concept["units"]))][1].update(fy=2027, fp="Q2", frame="CY2025Q3")
    assert run(archive(tmp_path, change))["status"] == "MAPPED"


def test_duplicate_equal_facts_are_harmless(tmp_path):
    def change(obs):
        rows = obs["companyfacts"]["payload"]["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"]
        rows.append(deepcopy(rows[0]))
    assert run(archive(tmp_path, change))["status"] == "MAPPED"


def test_latest_amendment_with_missing_facts_does_not_fall_back_silently(tmp_path):
    def change(obs):
        recent = obs["submissions"]["payload"]["filings"]["recent"]
        extra = dict(accessionNumber="0001045810-26-000099", filingDate="2026-09-30", reportDate="2026-07-26",
                     acceptanceDateTime="2026-09-30T20:00:00Z", form="10-Q/A")
        for k, value in extra.items(): recent[k].append(value)
    assert run(archive(tmp_path, change))["status"] == "PENDING_EVIDENCE"


def test_future_amendment_does_not_pollute_earlier_review(tmp_path):
    def change(obs):
        recent = obs["submissions"]["payload"]["filings"]["recent"]
        extra = dict(accessionNumber="0001045810-26-000099", filingDate="2026-10-02", reportDate="2026-07-26",
                     acceptanceDateTime="2026-10-02T20:00:00Z", form="10-Q/A")
        for k, value in extra.items(): recent[k].append(value)
    assert run(archive(tmp_path, change))["status"] == "MAPPED"


def no_quarters(obs):
    for concept in obs["companyfacts"]["payload"]["facts"]["us-gaap"].values():
        unit = next(iter(concept["units"]))
        for row in concept["units"][unit]:
            row["start"] = row["end"][:4] + "-01-01"


def test_ytd_annual_only_never_synthesize_quarter_eps(tmp_path):
    result = run(archive(tmp_path, no_quarters))
    assert result["status"] == "PENDING_EVIDENCE" and "no EPS subtraction" in result["issues"][0]


def issuer():
    data = json.loads((Path(__file__).resolve().parents[1] / "config/step09-review/NVDA-2026-10-01.json").read_text())
    return data["securities"][0]


def test_explicit_issuer_fallback_integrates_when_sec_quarter_absent(tmp_path):
    result = run(archive(tmp_path, no_quarters), issuer=issuer())
    assert result["status"] == "MAPPED" and result["mode"] == "REVIEWED_ISSUER"
    assert result["provenance"]["fallback_for"]
    assert result["evidence"]["current"]["eps"]["value"] == "2.46"
    assert not result["evidence"]["catalysts"]


@pytest.mark.parametrize("fault", ["wrong_id", "stale", "wrong_quarter", "wrong_amount", "basis"])
def test_invalid_issuer_fallback_cannot_override_source_gap(tmp_path, fault):
    item = issuer()
    if fault == "wrong_id": item["security_id"] = "wrong"
    if fault == "stale": item["valid_until"] = "2026-10-01T19:30:00Z"
    if fault == "wrong_quarter": item["latest_fiscal_quarter"] = 3
    if fault == "wrong_amount": item["current"]["eps"]["value"] = "99"
    if fault == "basis": item["prior"]["eps"]["share_basis"] = "different"
    with pytest.raises(MappingError):
        run(archive(tmp_path, no_quarters), issuer=item)


@pytest.mark.parametrize("fault", ["hash", "symbol", "cik", "receipt"])
def test_archive_integrity_and_identity_failures_are_not_hidden_by_fallback(tmp_path, fault):
    def change(obs):
        if fault == "cik": obs["companyfacts"]["payload"]["cik"] = 320193
        if fault == "receipt": obs["companyfacts"]["received_at"] = "2026-10-02T20:00:00Z"
    archive(tmp_path, change)
    if fault in {"hash", "symbol"}:
        p = tmp_path / "webull-NVDA-earnings_calendar.json"
        if fault == "hash":
            p.write_text(p.read_text() + " ")
        else:
            data = json.loads(p.read_text())
            data["symbol"] = "AAPL"
            p.write_text(json.dumps(data))
            report_path = tmp_path / "report.json"
            report = json.loads(report_path.read_text())
            next(r for r in report["checks"] if r["kind"] == "earnings_calendar")["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
            report_path.write_text(json.dumps(report))
    with pytest.raises(MappingError): run(tmp_path, issuer=issuer())


def test_missing_provider_does_not_erase_available_calendar(tmp_path):
    archive(tmp_path)
    (tmp_path / "sec-NVDA-companyfacts.json").unlink()
    result = run(tmp_path)
    assert result["status"] == "PENDING_EVIDENCE"
    assert result["evidence"]["calendar"][0]["range_start"] == "2026-11-17"


def test_targeted_summary_keeps_financial_reports_and_exact_eps_only():
    facts, sub, _ = sample()
    gaap = facts["payload"]["facts"]["us-gaap"]
    gaap["WeightedAverageNumberOfDilutedSharesOutstandingForEarningsPerShare"] = deepcopy(gaap["EarningsPerShareDiluted"])
    gaap["EarningsPerShareDilutedProForma"] = deepcopy(gaap["EarningsPerShareDiluted"])
    assert set(sec_summary("companyfacts", facts["payload"])["concept_observations"]) == {"EarningsPerShareDiluted", "Revenues"}
    recent = sub["payload"]["filings"]["recent"]
    for key, values in recent.items():
        recent[key] = (["4"] * 20 if key == "form" else [values[0]] * 20) + values
    result = sec_summary("submissions", sub["payload"])
    assert result["financial_rows"] == 1 and result["financial_sample"][0]["form"] == "10-Q"


def test_calendar_ranges_confirmation_conflict_and_precision():
    item = EarningsEvidence(**evidence())
    claim = item.calendar[0].model_dump(exclude_none=True)
    claim.pop("report_date")
    claim.update(range_start="2026-10-27", range_end="2026-11-02")
    interval = EarningsDate(**claim)
    at = datetime(2026, 9, 29, 14, tzinfo=timezone.utc)
    result = next_earnings(item.model_copy(update={"calendar": (interval,)}), at)
    assert result["status"] == "ESTIMATED" and "date" not in result
    result = next_earnings(item.model_copy(update={"calendar": (interval, item.calendar[0].model_copy(update={"confidence": "confirmed"}))}), at)
    assert result["status"] == "CONFIRMED" and result["date"] == "2026-10-29"
    outside = item.calendar[0].model_copy(update={"report_date": date(2026, 11, 3)})
    assert next_earnings(item.model_copy(update={"calendar": (interval, outside)}), at)["status"] == "CONFLICT"


@pytest.mark.parametrize("fields", [dict(range_start="2026-11-02", range_end="2026-10-27"),
                                       dict(range_start="2026-10-27"),
                                       dict(range_start="2026-10-27", range_end="2026-11-02", confidence="confirmed")])
def test_invalid_or_falsely_confirmed_interval_rejected(fields):
    with pytest.raises(ValueError):
        EarningsDate(**{**dict(fiscal_period="2026Q3", source_ref="synthetic", confidence="estimated",
                              received_at=AT, first_observed_at=AT), **fields})


def test_cli_exports_only_explicit_new_files_and_scanner_can_read(tmp_path, capsys):
    archive(tmp_path)
    args = ["--run-dir", str(tmp_path), "--symbol", "NVDA", "--security-id", "913257561", "--cik", "0001045810",
            "--review-ref", "synthetic test", "--reviewed-at", AT.isoformat(), "--valid-until", (AT+timedelta(hours=1)).isoformat(),
            "--output", str(tmp_path / "mapped.json"), "--evidence-output", str(tmp_path / "evidence.json")]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "MAPPED"
    from desk.earnings import ReviewedEarningsSource
    item = ReviewedEarningsSource(None, tmp_path / "evidence.json").earnings_evidence("NVDA")
    assert evaluate(CUP, "NVDA", "913257561", item, AT)["status"] == "QUALIFIED"
    assert evaluate(CUP, "NVDA", "913257561", item, AT+timedelta(hours=2))["status"] == "PENDING_EVIDENCE"
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "UNAVAILABLE"


def test_true_q4_annual_and_nine_month_eps_are_not_subtracted():
    facts, sub, cal = sample()
    actual = cal["payload"][0]
    actual.update(fiscal_year=2026, fiscal_period=4, expected_publish_date="2026-07-29", eps_actual="4.81", rev_actual="90007000000")
    recent = sub["payload"]["filings"]["recent"]
    recent.update(filingDate=["2026-07-29"], reportDate=["2026-06-30"], acceptanceDateTime=["2026-07-29T20:30:00Z"], form=["10-K"])
    gaap = facts["payload"]["facts"]["us-gaap"]
    for concept, values in (("EarningsPerShareDiluted", ("17.95", "13.14")), ("Revenues", ("331839000000", "241832000000"))):
        unit = next(iter(gaap[concept]["units"]))
        gaap[concept]["units"][unit] = [dict(start="2025-07-01", end=end, val=value, fy=2026, fp="FY", form="10-K", filed="2026-07-29", accn=ACCN)
                                         for end, value in zip(("2026-06-30", "2026-03-31"), values)]
    with pytest.raises(MappingError, match="no EPS subtraction"):
        sec_pair(facts, sub, actual, AT)
    # Explicit quarter disclosures anywhere in the FULL rows are accepted.
    for concept, values in (("EarningsPerShareDiluted", ("4.81", "3.65")), ("Revenues", ("90007000000", "76441000000"))):
        unit = next(iter(gaap[concept]["units"]))
        gaap[concept]["units"][unit].extend(dict(start=f"{year}-04-01", end=f"{year}-06-30", val=value,
                                                fy=2026, fp="FY", form="10-K", filed="2026-07-29", accn=ACCN)
                                           for year, value in zip((2026, 2025), values))
    pair, _ = sec_pair(facts, sub, actual, AT)
    assert pair[0].fiscal_quarter == pair[1].fiscal_quarter == 4
    assert pair[0].eps.value == Decimal("4.81") and pair[1].eps.value == Decimal("3.65")


def test_partial_conflicting_revenue_concept_cannot_hide_behind_missing_comparative(tmp_path):
    def change(obs):
        gaap = obs["companyfacts"]["payload"]["facts"]["us-gaap"]
        row = {**gaap["Revenues"]["units"]["USD"][0], "val": "999"}
        gaap["SalesRevenueNet"] = {"units": {"USD": [row]}}
    assert run(archive(tmp_path, change))["status"] == "PENDING_EVIDENCE"


def test_receipt_bound_never_invents_original_calendar_publication():
    obs = observation(dict(fiscal_year=2027, fiscal_period=3, start_date="2026-11-17", end_date="2026-11-23"),
                      host="api.sandbox.webull.com")
    claim = upcoming(obs, "financial_alert")[0]
    assert claim.published_at is None and claim.published_on is None
    assert claim.first_observed_at.isoformat() == "2026-10-01T19:35:35+00:00"
    item = EarningsEvidence(**evidence()).model_copy(update={"calendar": (claim,)})
    assert next_earnings(item, AT-timedelta(hours=1))["status"] == "UNKNOWN"


def test_ep_still_requires_separate_catalyst_after_source_mapping(tmp_path):
    item = run(archive(tmp_path))["evidence"]
    result = evaluate("5_qullamaggie_episodic_pivot", "NVDA", "913257561", item, AT)
    assert result["status"] == "PENDING_EVIDENCE" and "catalyst" in result["reasons"][0]


def test_provider_failure_and_date_conflict_do_not_become_unknown_silently(tmp_path):
    def change(obs):
        obs["earnings_calendar"]["payload"].append(dict(fiscal_year=2027, fiscal_period=3, currency="USD",
                                                       expected_publish_date="2026-11-30", eps_actual=None, rev_actual=None))
    result = run(archive(tmp_path, change))
    item = EarningsEvidence(**result["evidence"])
    assert next_earnings(item, AT)["status"] == "CONFLICT"


def test_scanner_qualify_reads_export_and_preserves_price_identity(tmp_path):
    from types import SimpleNamespace
    from desk.earnings import ReviewedEarningsSource, qualify
    result = run(archive(tmp_path))
    path = tmp_path / "scanner.json"
    path.write_text(json.dumps(dict(schema_version=1, securities=[result["evidence"]])))
    source = ReviewedEarningsSource(None, path)
    signal = SimpleNamespace(symbol="NVDA", setup_id=CUP, price_basis={"security_id": "913257561"})
    assert qualify(source, signal, AT)["status"] == "QUALIFIED"
    signal.price_basis["security_id"] = "wrong"
    assert qualify(source, signal, AT)["status"] == "PENDING_EVIDENCE"


def test_calendar_outage_survives_export_instead_of_becoming_no_event(tmp_path):
    archive(tmp_path)
    (tmp_path / "webull-NVDA-financial_alert.json").unlink()
    (tmp_path / "webull-NVDA-earnings_calendar.json").unlink()
    result = run(tmp_path)
    item = EarningsEvidence(**result["evidence"])
    output = next_earnings(item, AT)
    assert output["status"] == "UNAVAILABLE" and len(output["source_issues"]) == 2


def test_partial_calendar_coverage_remains_visible(tmp_path):
    archive(tmp_path)
    (tmp_path / "webull-NVDA-earnings_calendar.json").unlink()
    item = EarningsEvidence(**run(tmp_path)["evidence"])
    output = next_earnings(item, AT)
    assert output["status"] == "ESTIMATED" and output["coverage"] == "PARTIAL"


def test_sales_pair_without_eps_is_visible_and_does_not_qualify_cup(tmp_path):
    def change(obs):
        obs["companyfacts"]["payload"]["facts"]["us-gaap"].pop("EarningsPerShareDiluted")
    result = run(archive(tmp_path, change))
    assert result["metric_coverage"] == {"eps": False, "sales": True}
    assert evaluate(CUP, "NVDA", "913257561", result["evidence"], AT)["status"] == "PENDING_EVIDENCE"


def test_issuer_fallback_does_not_hide_conflicting_sec_actuals(tmp_path):
    def change(obs):
        obs["companyfacts"]["payload"]["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"][0]["val"] = "99"
    with pytest.raises(MappingError, match="conflict"):
        run(archive(tmp_path, change), issuer=issuer())


def test_annual_issuer_period_mislabeled_quarter_is_rejected(tmp_path):
    item = issuer()
    item["current"]["period_start"] = "2025-07-28"
    item["prior"]["period_start"] = "2024-07-28"
    with pytest.raises(MappingError, match="quarterly"):
        run(archive(tmp_path, no_quarters), issuer=item)


def test_invalid_fact_structure_fails_visibly(tmp_path):
    def change(obs):
        obs["companyfacts"]["payload"]["facts"]["us-gaap"]["Revenues"] = []
    result = run(archive(tmp_path, change))
    assert result["status"] == "PENDING_EVIDENCE" and "invalid fact" in result["issues"][0]


def test_issuer_can_fill_missing_eps_without_discarding_sec_sales(tmp_path):
    def change(obs):
        obs["companyfacts"]["payload"]["facts"]["us-gaap"].pop("EarningsPerShareDiluted")
    result = run(archive(tmp_path, change), issuer=issuer())
    assert result["mode"] == "REVIEWED_ISSUER" and result["metric_coverage"] == {"eps": True, "sales": True}


def test_partial_issuer_fallback_cannot_change_available_prior_sec_metric(tmp_path):
    def change(obs):
        obs["companyfacts"]["payload"]["facts"]["us-gaap"].pop("EarningsPerShareDiluted")
    item = issuer()
    item["prior"]["sales"]["value"] = "40000"
    with pytest.raises(MappingError, match="available SEC metric"):
        run(archive(tmp_path, change), issuer=item)
