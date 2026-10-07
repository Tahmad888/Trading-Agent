"""Option field conventions (G5a CP3, Tradier Step 2). Deterministic; no provider call."""
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from desk import option_conventions as oc

UTC = timezone.utc
CHECK = datetime(2026, 10, 6, 20, 30, tzinfo=UTC)


def tasty(**values):
    raw = dict(delta=0.5, gamma=0.06, theta=-0.2, vega=0.1, rho=0.01, volatility=0.2)
    raw.update(values)
    return oc.normalize_greeks("tastytrade", "dxlink", raw)


def tradier(**values):
    raw = dict(delta=0.449, gamma=0.066, theta=-0.65, vega=0.22, rho=0.0191, phi=-0.0192, bid_iv=0.1,
               mid_iv=0.11, ask_iv=0.12, smv_vol=0.105, updated_at="2026-10-06 20:00:06")
    raw.update(values)
    return oc.normalize_greeks("tradier", "rest", raw)


HUNDRED = oc.multiplier_from(100)


# ---- 1-3: exposure is raw x verified multiplier x signed contracts, once ----

def test_delta_share_equivalents_follow_contract_sign():
    view = tasty()["fields"]["delta"]
    assert oc.exposure(view, HUNDRED, 2)["value"] == "100.0"
    assert oc.exposure(view, HUNDRED, -2)["value"] == "-100.0"
    assert oc.exposure(view, HUNDRED, 2)["status"] == "SUPPORTED"


def test_put_delta_is_not_converted_a_second_time():
    view = tasty(delta=-0.5)["fields"]["delta"]
    assert view["value"] == "-0.5"
    assert oc.exposure(view, HUNDRED, 2)["value"] == "-100.0"


def test_theta_dollars_per_day_for_the_documented_daily_convention():
    view = tasty()["fields"]["theta"]
    assert view["status"] == "DOCUMENTED"
    assert oc.exposure(view, HUNDRED, 2)["value"] == "-40.0"
    assert oc.exposure(view, HUNDRED, -1)["value"] == "20.0"
    assert oc.position_view(tasty(), HUNDRED, 2)["theta"]["unit"] == "$ per day"


def test_zero_contracts_or_bool_contracts_are_refused():
    view = tasty()["fields"]["delta"]
    for bad in (0, True, 1.0):
        with pytest.raises(oc.ConventionError):
            oc.exposure(view, HUNDRED, bad)


# ---- 4: multiplier comes from metadata, never a default ----

def test_non_100_multiplier_follows_metadata():
    assert oc.exposure(tasty()["fields"]["delta"], oc.multiplier_from(10), 1)["value"] == "5.0"


@pytest.mark.parametrize("values,status", [((), "MISSING"), ((None,), "MISSING"), ((0,), "INVALID"),
                                           ((100.0,), "INVALID"), ((True,), "INVALID"), ((100, 10), "CONFLICT")])
def test_unknown_or_conflicting_terms_are_never_replaced_by_100(values, status):
    terms = oc.multiplier_from(*values)
    assert terms["status"] == status and terms["value"] is None
    result = oc.exposure(tasty()["fields"]["delta"], terms, 1)
    assert result["status"] == "UNAVAILABLE" and "value" not in result


def test_saved_text_multiplier_digits_are_read_and_agreement_required():
    assert oc.multiplier_from("100", 100)["value"] == 100
    assert oc.multiplier_from("100", 10)["status"] == "CONFLICT"


# ---- 5: sizes stay raw and are never scaled ----

def test_option_size_stays_raw_and_provisional():
    view = oc.size_view("tradier", "rest", "option", 53)
    assert (view["value"], view["size_unit_status"], view["interpreted_unit"], view["arithmetic"]) == (
        "53", "UNVERIFIED", "CONTRACTS_PROVISIONAL", "EXCLUDED")
    assert view["label"]
    stream = oc.size_view("tradier", "stream", "option", 53)
    assert stream["interpreted_unit"] == "CONTRACTS_PROVISIONAL"   # separate encoding, same label


def test_stock_size_is_a_sample_observation_and_unknown_sources_are_unresolved():
    assert oc.size_view("tradier", "rest", "stock", 40)["size_unit_status"] == "OBSERVED_SAMPLE"
    assert oc.size_view("tastytrade", "dxlink", "option", 40)["size_unit_status"] == "UNRESOLVED"
    for bad in (True, -1, 1.5, "NaN"):
        assert oc.size_view("tradier", "rest", "option", bad)["state"] != "VALUE"


# ---- 6: IV display is mapping-specific ----

def test_iv_decimal_displays_as_percent_and_does_not_scale_with_quantity():
    ivs = tradier(mid_iv=0.20)["implied_volatility"]
    assert ivs["mid_iv"]["display"] == "20%"
    assert tasty(volatility=0.2)["implied_volatility"]["volatility"]["display"] == "20%"
    with pytest.raises(oc.ConventionError):
        oc.iv_display(0.2, "UNKNOWN")   # never guessed from magnitude
    assert "implied_volatility" not in oc.position_view(tradier(), HUNDRED, 5)


# ---- 7-8: provider-specific times ----

def test_tradier_naive_greek_time_is_utc_with_its_evidence_and_et_display():
    parsed = oc.provider_time("tradier", "greeks.updated_at", "2026-10-06 20:00:06")
    assert parsed["utc"] == "2026-10-06T20:00:06+00:00"
    assert parsed["et"] == "2026-10-06 16:00:06 EDT"
    assert parsed["interpretation"] == "NAIVE_TEXT_READ_AS_UTC" and parsed["evidence_status"] == "INFERRED"
    assert "Relayed vendor answer" in parsed["qualification"] and parsed["raw"] == "2026-10-06 20:00:06"
    winter = oc.provider_time("tradier", "greeks.updated_at", "2026-12-07 20:00:06")
    assert winter["et"] == "2026-12-07 15:00:06 EST"


def test_explicit_offsets_are_respected_and_other_formats_refused():
    parsed = oc.provider_time("tradier", "greeks.updated_at", "2026-10-06T16:00:06-04:00")
    assert parsed["utc"] == "2026-10-06T20:00:06+00:00" and parsed["interpretation"] == "EXPLICIT_OFFSET_RESPECTED"
    for bad in ("2026-10-06", "2026/10/06 20:00:06", "2026-10-06 20:00", "20:00:06", 1791331200000, "", True):
        with pytest.raises(oc.ConventionError):
            oc.provider_time("tradier", "greeks.updated_at", bad)
    with pytest.raises(oc.ConventionError, match="TIME_MISSING"):
        oc.provider_time("tradier", "greeks.updated_at", None)


def test_naive_text_is_not_accepted_for_other_fields_or_providers():
    with pytest.raises(oc.ConventionError, match="TIME_FIELD_UNSUPPORTED"):
        oc.provider_time("tastytrade", "greeks.updated_at", "2026-10-06 20:00:06")
    with pytest.raises(oc.ConventionError):
        oc.provider_time("tradier", "bid_date", "2026-10-06 20:00:06")


def test_epoch_milliseconds_and_refusals():
    parsed = oc.provider_time("tradier", "bid_date", 1791331195000)
    assert parsed["utc"] == "2026-10-06T23:59:55+00:00"   # 19:59:55 ET, end of the extended session
    assert oc.provider_time("tradier", "bid_date", "1791331195000")["utc"] == parsed["utc"]
    with pytest.raises(oc.ConventionError, match="TIME_UNIT_UNSUPPORTED"):
        oc.provider_time("tradier", "trade_date", 1612196262)   # seconds magnitude: not rescaled
    for bad, code in ((0, "TIME_UNAVAILABLE"), (True, "TIME_INVALID"), (1.7e12, "TIME_INVALID")):
        with pytest.raises(oc.ConventionError, match=code):
            oc.provider_time("tradier", "ask_date", bad)


def test_future_greek_time_is_kept_as_an_anomaly_not_clamped():
    parsed = oc.provider_time("tradier", "greeks.updated_at", "2026-10-06 20:31:00")
    age = oc.age_view(parsed, CHECK)
    assert age["age_seconds"] == -60.0 and age["anomaly"] == "FUTURE_AT_CHECK" and age["threshold"] == "NONE_DEFINED"
    assert oc.age_view(oc.provider_time("tradier", "greeks.updated_at", "2026-10-06 19:30:00"), CHECK)["age_seconds"] == 3600.0
    with pytest.raises(oc.ConventionError):
        oc.age_view(parsed, datetime(2026, 10, 6, 20, 30))   # naive check time


def test_quote_and_greek_times_are_distinct_fields():
    side = oc.provider_time("tradier", "bid_date", 1791331195000)
    greek = oc.provider_time("tradier", "greeks.updated_at", "2026-10-06 19:00:06")
    assert side["meaning"] != greek["meaning"] and side["utc"] != greek["utc"]


# ---- 8: numeric validation is per field ----

def test_bad_fields_affect_only_themselves_and_zero_negative_are_values():
    greeks = tasty(delta=True, gamma=float("nan"), theta=0, vega="abc", rho=-0.02)
    fields = greeks["fields"]
    assert fields["delta"]["state"] == "INVALID_TYPE" and fields["gamma"]["state"] == "NONFINITE"
    assert fields["vega"]["state"] == "INVALID_NUMBER"
    assert fields["theta"]["value"] == "0" and fields["rho"]["value"] == "-0.02"
    assert oc.exposure(fields["theta"], HUNDRED, 1)["value"] == "0"
    assert oc.exposure(fields["delta"], HUNDRED, 1)["status"] == "UNAVAILABLE"
    missing = oc.normalize_greeks("tastytrade", "dxlink", {"delta": 0.4})
    assert missing["fields"]["gamma"]["state"] == "MISSING" and missing["fields"]["delta"]["state"] == "VALUE"
    with pytest.raises(oc.ConventionError):
        oc.normalize_greeks("alpaca", "indicative", {})


# ---- 9: Tradier mappings ----

def test_tradier_rho_phi_stay_raw_and_theta_vega_gamma_are_provisional():
    greeks = tradier()
    view = oc.position_view(greeks, HUNDRED, 1)
    assert view["rho"]["status"] == view["phi"]["status"] == "RAW_ONLY"
    assert greeks["fields"]["rho"]["value"] == "0.0191" and greeks["fields"]["rho"]["normalized"] is False
    for name in ("gamma", "theta", "vega"):
        assert view[name]["status"] == "PROVISIONAL" and view[name]["label"].startswith("PROVISIONAL")
    assert view["delta"]["status"] == "SUPPORTED" and view["delta"]["value"] == "44.900"


def test_pair_check_reports_shared_fields_without_transforming_the_put():
    call, put = tradier(), tradier(delta=-0.551)
    check = oc.pair_check(call, put)
    assert {"rho", "phi", "theta", "vega", "gamma"} <= set(check["identical_fields"])
    assert "delta" not in check["identical_fields"]
    assert check["delta_call_minus_put"] == "1.000" and check["put_delta_sign"] == "NEGATIVE"
    assert put["fields"]["delta"]["value"] == "-0.551"


# ---- offline tastytrade normalizer: observations only ----

def greeks_record(**over):
    record = {"type": "GREEKS", "symbol": "SPY261008C00780000", "generation": "g1", "flags": [], "index": 0,
              "sequence": 0, "received_at": "2026-10-06T23:59:48+00:00",
              "source_time": {"state": "VALUE", "raw": 1791331187780},
              **{k: {"state": "VALUE", "value": v} for k, v in
                 dict(delta="0.4943", gamma="0.0662", theta="-0.6516", vega="0.2211", rho="0.0194",
                      volatility="0.1087").items()}}
    record.update(over)
    return record


def test_tastytrade_records_are_normalized_as_observations_only():
    report = oc.tastytrade_greek_records({"measurements": {"records": [
        greeks_record(), greeks_record(source_time={"state": "ZERO", "raw": 0}), {"type": "BBO"}]}})
    assert report["status"] == "OBSERVATIONS_ONLY" and report["current_state"] == "NOT_REDUCED"
    assert len(report["records"]) == 2 and report["time_failures"] == 1
    first = report["records"][0]
    assert first["label"] == "RAW_OBSERVATION_NORMALIZED_NOT_CURRENT"
    assert first["greeks"]["fields"]["theta"]["value"] == "-0.6516"
    assert first["time"]["receipt_age"]["age_seconds"] == pytest.approx(0.22)
    assert report["exposure"] == "NOT_COMPUTED_MULTIPLIER_NOT_IN_CAPTURE"
    assert oc.tastytrade_greek_records({})["status"] == "NO_GREEKS_RECORDS"


def test_offline_cli_refuses_to_overwrite(tmp_path, capsys):
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({"measurements": {"records": [greeks_record()]}}))
    out = tmp_path / "out.json"
    assert oc.main(["tastytrade-greeks", "--capture", str(capture), "--output", str(out)]) == 0
    assert json.loads(out.read_text())["status"] == "OBSERVATIONS_ONLY"
    with pytest.raises(SystemExit):
        oc.main(["tastytrade-greeks", "--capture", str(capture), "--output", str(out)])


def test_decimal_values_keep_their_text_precision():
    assert tradier(delta=Decimal("0.5652816604132407"))["fields"]["delta"]["value"] == "0.5652816604132407"


def test_conventions_stay_out_of_decision_modules():
    # Analysis-only Greek state may normalize fields. Tickets, risk and setups
    # still cannot import these analytics or the diagnostic.
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src" / "desk"
    importer = re.compile(r"^\s*(from desk(\.option_conventions|\.tradier_option_check| import "
                          r"(option_conventions|tradier_option_check))|import desk\.(option_conventions|tradier_option_check))",
                          re.M)
    users = sorted(p.name for p in root.rglob("*.py") if importer.search(p.read_text()))
    assert users == ["tastytrade_greeks.py", "tradier_option_check.py"]
