"""Synthetic reviewed evidence; no live earnings or profitability claims."""
from decimal import Decimal
import json

import pytest

from desk.earnings import EarningsEvidence, evaluate, ReviewedEarningsSource, qualify, configured_source
from desk import scanner as sc
from desk.playbook.cards import CARDS
from desk.playbook.triggers import growth_group
from tests.test_signal_lifecycle import now, BASE
from tests.test_scanner import sig, m15, Fake, DAY

EP="5_qullamaggie_episodic_pivot"
CUP="3_oneil_cup_with_handle"


def metric(value="1",sales=False):
    return dict(value=value, scale="1",currency="USD", unit="currency" if sales else "currency/share",
        accounting_basis="US_GAAP",definition="total_revenue" if sales else "diluted_eps",
        share_basis=None if sales else "synthetic split-adjusted common shares")


def evidence(eps="1.25",sales="1.25"):
    def quarter(year,value,revenue):
        return dict(fiscal_year=year,fiscal_quarter=2,period_kind="quarter",
            period_start=f"{year}-04-01",period_end=f"{year}-06-30",
            source_ref=f"https://issuer.example/earnings/{year}",
            published_at=f"{year}-08-01T12:00:00Z",received_at="2026-09-29T13:40:00Z",
            eps=metric(value) if value is not None else None,
            sales=metric(revenue,True) if revenue is not None else None)
    return dict(symbol="LEAD",security_id="fixture:LEAD",review_ref="synthetic reviewer file",
        reviewed_at="2026-09-29T13:45:00Z",valid_until="2026-09-29T20:00:00Z",
        latest_fiscal_year=2026,latest_fiscal_quarter=2,
        current=quarter(2026,eps,sales),prior=quarter(2025,"1","1"),
        catalysts=[dict(entry_session="2026-09-29",kind="other",summary="synthetic material news",
            relevance_ref="synthetic session review",source_ref="https://issuer.example/news",
            published_at="2026-09-29T12:00:00Z",received_at="2026-09-29T13:40:00Z")],
        calendar=[dict(fiscal_period="2026Q3",report_date="2026-10-29",confidence="estimated",
            source_ref="https://calendar.example/event",published_at="2026-09-01T12:00:00Z",
            received_at="2026-09-29T13:40:00Z")])


def run(bundle,setup=EP,at=None,trigger=None):
    return evaluate(setup,"LEAD","fixture:LEAD",bundle,at or now(),trigger_at=trigger or now())


@pytest.mark.parametrize("value,status,tag",[("1.249","REJECTED",None),("1.25","QUALIFIED","25-50%"),
    ("1.499","QUALIFIED","25-50%"),("1.50","QUALIFIED","50%+")])
def test_exact_growth_boundaries(value,status,tag):
    result=run(evidence(value,value))
    assert result["status"]==status and result.get("growth_group")==tag
    assert Decimal(result["growth"]["eps"])==Decimal(value)-1


@pytest.mark.parametrize("old",["0","-1"])
def test_nonpositive_eps_baseline_allows_sales_only_ep(old):
    data=evidence("2","1.25")
    data["prior"]["eps"]["value"]=old
    result=run(data)
    assert result["status"]=="QUALIFIED" and result["growth"]["eps"] is None
    assert "nonpositive" in result["growth"]["eps_issue"]
    assert run(data,CUP)["status"]=="PENDING_EVIDENCE"


def test_missing_eps_sales_only_and_cup_eps_only():
    assert run(evidence(None,"1.25"))["status"]=="QUALIFIED"
    assert run(evidence(None,"1.25"),CUP)["status"]=="PENDING_EVIDENCE"
    assert run(evidence("1.249","2"),CUP)["status"]=="REJECTED"
    assert run(evidence("1.25",None),CUP)["status"]=="QUALIFIED"


def test_negative_current_eps_is_a_decline_not_positive_growth():
    result=run(evidence("-1","1"))
    assert result["status"]=="REJECTED" and result["growth"]["eps"]=="-2"


@pytest.mark.parametrize("field,value",[("currency","EUR"),("accounting_basis","IFRS"),
    ("definition","basic_eps"),("share_basis","different split scale")])
def test_eps_comparability_independent_of_sales(field,value):
    data=evidence()
    data["prior"]["eps"][field]=value
    assert run(data)["status"]=="QUALIFIED"
    data["current"]["sales"]=None
    assert run(data)["status"]=="PENDING_EVIDENCE"


def test_revenue_scale_normalization():
    data=evidence(None,"1.25")
    data["current"]["sales"]["scale"]="1000000"
    data["prior"]["sales"]["value"]="1000000"
    result=run(data)
    assert result["status"]=="QUALIFIED" and Decimal(result["growth"]["sales"])==Decimal(".25")


@pytest.mark.parametrize("fault",["fiscal_quarter","fiscal_year","latest","annual","duration","identity","future_review","expired"])
def test_period_identity_and_review_rejections(fault):
    data=evidence()
    if fault=="fiscal_quarter": data["prior"]["fiscal_quarter"]=1
    if fault=="fiscal_year": data["prior"]["fiscal_year"]=2024
    if fault=="latest": data["latest_fiscal_quarter"]=3
    if fault=="annual": data["current"]["period_kind"]="annual"
    if fault=="duration": data["current"]["period_start"]="2026-04-08"
    if fault=="identity": data["security_id"]="other"
    if fault=="future_review": data["reviewed_at"]="2026-09-29T15:00:00Z"
    if fault=="expired": data["valid_until"]="2026-09-29T13:59:59Z"
    assert run(data)["status"]=="PENDING_EVIDENCE"


def test_explicit_period_comparability_for_different_week_counts():
    data=evidence()
    data["current"]["period_start"]="2026-04-08"
    data["period_comparability_ref"]="synthetic 12/13-week quarter comparability review"
    assert run(data)["status"]=="QUALIFIED"


@pytest.mark.parametrize("fault",["missing","wrong_day","post_trigger","wrong_report"])
def test_catalyst_must_be_supported_at_original_trigger(fault):
    data=evidence()
    if fault=="missing": data["catalysts"]=[]
    if fault=="wrong_day": data["catalysts"][0]["entry_session"]="2026-09-28"
    if fault=="post_trigger":
        data["reviewed_at"]="2026-09-29T14:02:00Z"
        data["catalysts"][0].update(published_at="2026-09-29T14:01:00Z",received_at="2026-09-29T14:01:30Z")
    if fault=="wrong_report": data["catalysts"][0].update(kind="earnings",report_period_end="2025-06-30")
    result=run(data,at=now(minute=3))
    assert result["status"]=="PENDING_EVIDENCE"
    assert "catalyst" in result["reasons"][0]


def test_later_receipt_cannot_rewrite_earlier_decision_but_can_inform_current_review():
    data=evidence()
    data["reviewed_at"]="2026-09-29T14:02:00Z"
    data["catalysts"][0]["received_at"]="2026-09-29T14:01:00Z"
    assert run(data)["status"]=="PENDING_EVIDENCE"
    result=run(data,at=now(minute=3))
    assert result["status"]=="QUALIFIED" and "10:03" in result["evaluated_at"]
    assert "10:00" in result["publication_cutoff"]


def test_result_revision_published_after_trigger_cannot_backfill():
    data=evidence()
    data["current"].update(published_at="2026-09-29T14:01:00Z",received_at="2026-09-29T14:01:00Z")
    data["reviewed_at"]="2026-09-29T14:02:00Z"
    assert run(data,at=now(minute=3))["status"]=="PENDING_EVIDENCE"


def test_calendar_confidence_and_conflicts_do_not_supply_growth():
    data=evidence()
    assert run(data)["next_earnings"]["status"]=="ESTIMATED"
    data["calendar"][0]["confidence"]="confirmed"
    assert run(data)["next_earnings"]["status"]=="CONFIRMED"
    data["calendar"].append({**data["calendar"][0],"report_date":"2026-10-30"})
    assert run(data)["next_earnings"]["status"]=="CONFLICT"
    data["current"]=data["prior"]=None
    assert run(data)["status"]=="PENDING_EVIDENCE"


def test_unknown_calendar_has_no_invented_date_or_trading_ban():
    data=evidence(); data["calendar"]=[]
    result=run(data)
    assert result["status"]=="QUALIFIED" and result["next_earnings"]["status"]=="UNKNOWN"


@pytest.mark.parametrize("value",["NaN","Infinity","-Infinity"])
def test_nonfinite_data_cannot_qualify(value):
    data=evidence(); data["current"]["eps"]["value"]=value
    assert run(data)["status"]=="PENDING_EVIDENCE"
    assert growth_group(CARDS[EP],float(value),None) is None
    assert growth_group(CARDS[EP],float(value),.25)=="25-50%"


def test_file_adapter_reloads_revisions_and_does_not_cache_qualification(tmp_path):
    path=tmp_path/"evidence.json"
    source=ReviewedEarningsSource(Fake({}),path)
    signal=sig(EP)
    data=evidence()
    path.write_text(json.dumps({"schema_version":1,"securities":[data]}))
    first=qualify(source,signal,now(),trigger_at=now())
    data["current"]["eps"]["value"]="1"
    data["current"]["sales"]["value"]="1"
    path.write_text(json.dumps({"schema_version":1,"securities":[data]}))
    second=qualify(source,signal,now(),trigger_at=now())
    assert first["status"]=="QUALIFIED" and second["status"]=="REJECTED"
    assert first["evidence_id"]!=second["evidence_id"]
    path.write_text('{bad')
    assert qualify(source,signal,now())["status"]=="PENDING_EVIDENCE"


def test_configuration_is_opt_in_and_source_errors_are_safe():
    source=Fake({})
    assert configured_source(source,{}) is source
    class Broken(Fake):
        def earnings_evidence(self,symbol): raise RuntimeError("SECRET")
    result=qualify(Broken({}),sig(EP),now())
    assert result["status"]=="PENDING_EVIDENCE" and "SECRET" not in json.dumps(result)


def test_trigger_visible_but_review_requires_fundamentals_and_records_changes(tmp_path):
    # Generic breakout observation with cup's earnings requirement; not a cup detector fixture.
    signal=sig(CUP)
    log=sc.ScanLog(tmp_path)
    class Source(Fake):
        bundle=None
        def earnings_evidence(self,symbol): return self.bundle
    source=Source({("LEAD","M15"):m15(BASE)})
    rec=sc.intraday_scan(source,[signal],now(),store=log.signals)
    event=rec.triggered[0]
    assert not event["qualified_for_analysis"]
    assert event["fundamentals"]["status"]=="PENDING_EVIDENCE"
    result=sc.revalidate_signal(source,log,event["event_id"],now(),symbol="LEAD",price=101.5,quote_at=now())
    assert not result["eligible"]
    source.bundle=evidence()
    result=sc.revalidate_signal(source,log,event["event_id"],now(),symbol="LEAD",price=101.5,quote_at=now())
    assert result["eligible"]
    source.bundle=evidence("1","1")
    result=sc.revalidate_signal(source,log,event["event_id"],now(),symbol="LEAD",price=101.5,quote_at=now())
    assert not result["eligible"] and result["fundamentals"]["status"]=="REJECTED"
    reviews=[json.loads(line) for line in (tmp_path/"earnings-reviews.jsonl").read_text().splitlines()]
    assert [r["status"] for r in reviews]==["PENDING_EVIDENCE","QUALIFIED","REJECTED"]
    assert len(log.signals.events(now()))==1  # no new technical event from evidence changes


def test_webull_quarterly_request_has_no_estimate_to_actual_conversion():
    from tests.test_webull import client, FakeTransport
    from urllib.parse import urlparse, parse_qs
    raw={"undocumented_fields":[{"estimated_eps":"99"}]}
    t=FakeTransport(raw)
    assert client(t).quarterly_income("BRK.B")==raw
    request=t.requests[0]
    assert request.method=="GET" and "/income-statements/get" in request.full_url
    assert parse_qs(urlparse(request.full_url).query)=={
        "symbol":["BRK B"],"category":["US_STOCK"],"type":["QUARTERLY"],"count":["5"]}


def test_probe_keeps_calendar_and_income_separate_redacts_and_never_qualifies():
    from desk.earnings_probe import probe
    class Source:
        _key="SECRETKEY"
        def earnings_calendar(self,symbol): return [{"expected_publish_date":"2026-10-29"}]
        def quarterly_income(self,symbol): return {"unknown":"SECRETKEY"}
    result=probe(Source(),"NVDA")
    assert result["status"]=="OBSERVATIONS_ONLY"
    assert [r["kind"] for r in result["observations"]]==["calendar","quarterly_income"]
    assert "SECRETKEY" not in json.dumps(result)


def test_probe_stops_on_error_no_raw_exception_or_retry():
    from desk.earnings_probe import probe
    class Source:
        calls=[]
        def earnings_calendar(self,symbol):
            self.calls.append(symbol)
            raise RuntimeError("SECRET")
        def quarterly_income(self,symbol): raise AssertionError("must not be called")
    source=Source()
    result=probe(source,"NVDA")
    assert result["status"]=="UNAVAILABLE" and len(source.calls)==1
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("mutation",["unknown_basis","adjusted_without_definition","segment_sales","date_only_publication","receipt_before_publication"])
def test_unknown_semantics_are_not_qualified(mutation):
    data=evidence()
    if mutation=="unknown_basis": data["current"]["eps"]["accounting_basis"]="UNKNOWN"
    if mutation=="adjusted_without_definition": data["current"]["eps"]["accounting_basis"]="ADJUSTED"
    if mutation=="segment_sales": data["current"]["sales"]["definition"]="segment_revenue"
    if mutation=="date_only_publication": data["current"]["published_at"]="2026-08-01"
    if mutation=="receipt_before_publication": data["current"]["received_at"]="2026-07-01T12:00:00Z"
    assert run(data)["status"]=="PENDING_EVIDENCE"


def test_adjusted_eps_comparison_requires_same_documented_adjustments():
    data=evidence("1.25",None)
    for q in ("current","prior"):
        data[q]["eps"].update(accounting_basis="ADJUSTED",adjustment_basis="synthetic identical exclusions")
    assert run(data)["status"]=="QUALIFIED"
    data["prior"]["eps"]["adjustment_basis"]="different exclusions"
    assert run(data)["status"]=="PENDING_EVIDENCE"


def test_calendar_conflict_is_not_hidden_when_one_claimed_date_has_passed():
    data=evidence()
    data["calendar"].append({**data["calendar"][0],"report_date":"2026-09-28"})
    assert run(data)["next_earnings"]["status"]=="CONFLICT"


def test_file_duplicate_identity_is_rejected(tmp_path):
    path=tmp_path/"evidence.json"
    path.write_text(json.dumps({"schema_version":1,"securities":[evidence(),evidence()]}))
    result=qualify(ReviewedEarningsSource(Fake({}),path),sig(EP),now())
    assert result["status"]=="PENDING_EVIDENCE"


def test_confirmed_earnings_catalyst_requires_matching_report_period():
    data=evidence()
    data["catalysts"][0].update(kind="earnings",report_period_end="2026-06-30")
    assert run(data)["status"]=="QUALIFIED"



def test_new_ep_candidate_is_visible_with_pending_evidence(tmp_path):
    from tests.test_discovery import picks, ep_frames
    from tests.test_scanner import frames
    from datetime import date
    log=sc.ScanLog(tmp_path)
    picks(log,add=["NEW"])
    rec=sc.run(Fake({**frames(date(2026,9,28)),**ep_frames()}),[],log,now())
    assert any(s["symbol"]=="NEW" and s["setup_id"]==EP for s in rec.armed)
    assert rec.qualification["NEW/"+EP]["status"]=="PENDING_EVIDENCE"
