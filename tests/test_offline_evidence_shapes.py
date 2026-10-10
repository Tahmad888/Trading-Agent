"""G5 backlog repair R3: exact contract-size metadata and malformed offline evidence.

Injected transports and in-memory captures only; zero network, no mapping installed.
"""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from desk import quote_check as qc
from desk import saved_greek_audit as sga
from desk import tradier_readiness as tr
from desk.tradier_quotes import ReviewedMapping
from tests.test_afterhours_readiness import captures, saved
from tests.test_diagnostic_pair import capture, provider, selected
from tests.test_tradier_option_check import CALL, PUT
from tests.test_tradier_quotes_runtime import mapping


# ---- C-1: fractional shares-per-contract is a conflict, never absent ---------------------------
def pair_report(value, *, drop=False):
    pair = selected()

    def change(symbol, row):
        if symbol != PUT:
            return row
        if drop:
            return {k: v for k, v in row.items() if k != "shares-per-contract"}
        return dict(row, **{"shares-per-contract": value})
    client, service, calls, at = provider(pair, mutate=change)
    report = qc.diagnostic(client, service, ["SPY"], option_pair=pair, capture_fn=capture, clock=lambda: at)
    return report, service


@pytest.mark.parametrize("bad,text", [(Decimal("100.0"), "100.0"), (Decimal("100.5"), "100.5"),
                                      (Decimal("10.0"), "10.0"), (100.5, "100.5")])
def test_a_fractional_size_is_kept_as_exact_text_and_conflicts(bad, text):
    report, service = pair_report(bad)
    comparison = report["option_pair_comparison"]
    check = comparison["by_symbol"][PUT]
    assert comparison["status"] == "FAIL"
    assert check["status"] == "CONFLICT" and check["reason"] == "INVALID_PROVIDER_SIZE"
    assert check["provider_fields"]["shares-per-contract"] == text
    assert PUT not in service.identities                          # the counterpart is never subscribed
    assert CALL in service.identities and "SPY" in service.identities   # healthy peers stay
    assert report["stocks"]["checks"]


@pytest.mark.parametrize("value,drop,status", [(100, False, "MATCHED_REPORTED_FIELDS"),
                                               (None, True, "INCOMPLETE"),
                                               (10, False, "CONFLICT")])
def test_controls_integer_missing_and_contradictory_sizes(value, drop, status):
    report, service = pair_report(value, drop=drop)
    check = report["option_pair_comparison"]["by_symbol"][PUT]
    assert check["status"] == status
    assert (PUT in service.identities) == (status != "CONFLICT")
    assert "SPY" in service.identities


# ---- D O2: saved Greek audit shapes ------------------------------------------------------------
LABELS = {"decision_eligibility": "NOT_ATTESTED", "live_state": "NOT_RESTORED", "provider_calls": 0}


@pytest.mark.parametrize("damage,code", [
    (lambda c: [], "CAPTURE_ROOT_NOT_OBJECT"),
    (lambda c: "text", "CAPTURE_ROOT_NOT_OBJECT"),
    (lambda c: dict(c, measurements=[]), "MEASUREMENTS_NOT_OBJECT"),
    (lambda c: dict(c, measurements=dict(c["measurements"], records={"0": {}})), "RECORDS_NOT_LIST"),
    (lambda c: dict(c, measurements=dict(c["measurements"], records_dropped=[3])), "RECORDS_DROPPED_NOT_OBJECT"),
    (lambda c: dict(c, identity_capture={"x": 1}), "IDENTITY_CAPTURE_NOT_LIST"),
    (lambda c: dict(c, identity_capture=c["identity_capture"] + ["SPY"]), "IDENTITY_ROW_NOT_OBJECT"),
])
def test_saved_audit_refuses_unattributable_damage_with_a_fixed_code(damage, code):
    out = sga.audit(damage(saved()), "a" * 64)
    assert out["status"] == "CAPTURE_SHAPE_INVALID" and out["input_sha256"] == "a" * 64
    assert code in {i["code"] for i in out["issues"]} and out["observations"] == []
    assert {k: out[k] for k in LABELS} == LABELS


def two_rows():
    c = saved()
    good = c["measurements"]["records"][0]
    c["measurements"]["records"] = [deepcopy(good), deepcopy(good)]
    return c


@pytest.mark.parametrize("damage", ["source_time_list", "source_time_text", "record_not_object"])
def test_saved_audit_isolates_a_malformed_row_and_keeps_the_healthy_peer(damage):
    c = two_rows()
    if damage == "record_not_object":
        c["measurements"]["records"].insert(0, ["GREEKS"])
    else:
        c["measurements"]["records"][0]["source_time"] = [] if damage == "source_time_list" else "raw"
    out = sga.audit(c, "a" * 64)
    assert out["status"] == "PARTIAL_OBSERVATIONS"
    statuses = [o["status"] for o in out["observations"]]
    assert "INDEXED_OBSERVATION_VALID" in statuses
    if damage == "record_not_object":
        assert out["issues"] == [{"path": "measurements.records[0]", "code": "RECORD_NOT_OBJECT"}]
        assert statuses == ["INDEXED_OBSERVATION_VALID"] * 2
    else:
        assert out["observations"][0]["reason"] == "SAVED_ROW_SHAPE_INVALID"
    assert all(o["label"] == "HISTORICAL_OBSERVATION" for o in out["observations"])
    assert {k: out[k] for k in LABELS} == LABELS


def test_saved_audit_command_refuses_invalid_json_and_shapes(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert sga.main(["--capture", str(bad), "--output", str(tmp_path / "a.json")]) == 1
    report = json.loads((tmp_path / "a.json").read_text())
    assert report["status"] == "CAPTURE_JSON_INVALID" and len(report["input_sha256"]) == 64
    shape = tmp_path / "shape.json"
    shape.write_text("[]")
    assert sga.main(["--capture", str(shape), "--output", str(tmp_path / "b.json")]) == 1
    assert json.loads((tmp_path / "b.json").read_text())["status"] == "CAPTURE_SHAPE_INVALID"


# ---- D O2: readiness shapes --------------------------------------------------------------------
def test_astra_malformed_round_returns_a_structured_refusal():
    out = tr.prepare({"identity_capture": []}, {"rounds": [None]}, ["SPY"], "a" * 64, "b" * 64)
    assert out["status"] == "CAPTURE_SHAPE_INVALID" and not out["candidates"]
    assert out["capture_issues"] == [{"path": "tradier.rounds[0]", "code": "TRADIER_ROUND_NOT_OBJECT"}]
    assert out["input_sha256"] == {"webull": "a" * 64, "tradier": "b" * 64}
    assert out["installed_mappings"] == 0 and out["provider_calls"] == 0


def with_peer():
    w, t = captures()
    peer = mapping("PEER")
    w["identity_capture"].append(peer.webull.model_dump())
    t["rounds"][0]["observations"]["PEER"] = {"issuer_reference": peer.tradier.model_dump()}
    return w, t


@pytest.mark.parametrize("damage,code", [
    (lambda w, t: ([], t), "WEBULL_CAPTURE_NOT_OBJECT"),
    (lambda w, t: (w, None), "TRADIER_CAPTURE_NOT_OBJECT"),
    (lambda w, t: (dict(w, identity_capture="LEAD"), t), "WEBULL_IDENTITY_CAPTURE_NOT_LIST"),
    (lambda w, t: (dict(w, identity_capture=w["identity_capture"] + [7]), t), "WEBULL_IDENTITY_ROW_NOT_OBJECT"),
    (lambda w, t: (w, dict(t, rounds={"0": {}})), "TRADIER_ROUNDS_NOT_LIST"),
    (lambda w, t: (w, dict(t, rounds=t["rounds"] + [{"observations": []}])), "TRADIER_OBSERVATIONS_NOT_OBJECT"),
])
def test_readiness_unattributable_damage_withholds_every_draft(damage, code):
    w, t = damage(*with_peer())
    out = tr.prepare(w, t, ["LEAD", "PEER"], "a" * 64, "b" * 64)
    assert out["status"] == "CAPTURE_SHAPE_INVALID" and not out["candidates"]
    assert code in {i["code"] for i in out["capture_issues"]}
    assert set(out["issues"]) == {"LEAD", "PEER"}


@pytest.mark.parametrize("damage", ["row_not_object", "reference_not_object", "reference_list"])
def test_readiness_isolates_a_symbols_malformed_row_and_keeps_the_peer(damage):
    w, t = with_peer()
    obs = t["rounds"][0]["observations"]
    obs["LEAD"] = (None if damage == "row_not_object" else
                   {"issuer_reference": "LEAD"} if damage == "reference_not_object" else {"issuer_reference": []})
    out = tr.prepare(w, t, ["LEAD", "PEER"], "a" * 64, "b" * 64)
    assert out["issues"]["LEAD"] == ["TRADIER_IDENTITY_MISSING_OR_CONTRADICTORY",
                                     "TRADIER_ISSUER_METADATA_MISSING_OR_INVALID"]
    assert out["candidates"]["PEER"]["review_status"] == "DRAFT_NOT_INSTALLABLE"
    assert out["status"] == "INCOMPLETE_CAPTURE_EVIDENCE"


def test_readiness_command_refuses_invalid_json(tmp_path, monkeypatch):
    for k in ("DESK_TRADIER_BASE_FACTORY", "DESK_TRADIER_ENVIRONMENT", "DESK_TRADIER_QUOTE_STORE", "TRADIER_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "w.json").write_text("{")
    (tmp_path / "t.json").write_text("{}")
    code = tr.main(["--webull-capture", str(tmp_path / "w.json"), "--tradier-capture", str(tmp_path / "t.json"),
                    "--output", str(tmp_path / "out.json")])
    out = json.loads((tmp_path / "out.json").read_text())
    assert code == 1 and out["crosswalk_preparation"]["status"] == "CAPTURE_JSON_INVALID"


# ---- D-O1: alternate spellings are visible, never approved ------------------------------------
def brk():
    w, t = captures()
    w["identity_capture"][0]["symbol"] = "BRK.B"
    t["rounds"] = [{"observations": {"BRK.B": {"issuer_reference": {
        "symbol": "BRK/B", "type": "stock", "description": "Berkshire class B"}}}}]
    return w, t


def test_an_alternate_spelling_with_another_identity_is_shown_and_withholds_the_draft():
    w, t = brk()
    other = dict(w["identity_capture"][0], symbol="BRK/B", instrument_id="different-id")
    w["identity_capture"].append(other)
    out = tr.prepare(w, t, ["BRK.B"], "a", "b")
    assert "ALTERNATE_SPELLING_IDENTITY_CONFLICT" in out["issues"]["BRK.B"] and not out["candidates"]
    assert out["evidence_inventory"]["BRK.B"]["alternate_spellings"] == [
        {"source": "webull", "symbol": "BRK/B", "instrument_id": "different-id"}]


def test_an_alternate_spelling_with_the_same_identity_is_shown_but_stays_a_draft():
    w, t = brk()
    w["identity_capture"].append(dict(w["identity_capture"][0], symbol="BRK/B"))
    out = tr.prepare(w, t, ["BRK.B"], "a", "b")
    draft = out["candidates"]["BRK.B"]
    assert draft["review_status"] == "DRAFT_NOT_INSTALLABLE"
    assert out["evidence_inventory"]["BRK.B"]["alternate_spellings"][0]["symbol"] == "BRK/B"
    with pytest.raises(ValueError):
        ReviewedMapping.model_validate(draft)


def test_a_tradier_observation_under_another_spelling_is_shown_and_withholds_the_draft():
    w, t = brk()
    t["rounds"][0]["observations"]["BRK/B"] = deepcopy(t["rounds"][0]["observations"]["BRK.B"])
    out = tr.prepare(w, t, ["BRK.B"], "a", "b")
    assert out["issues"]["BRK.B"] == ["TRADIER_ALTERNATE_SPELLING_UNREVIEWED"]
    assert out["evidence_inventory"]["BRK.B"]["alternate_spellings"] == [
        {"source": "tradier", "observation_key": "BRK/B"}]


def test_control_exact_spellings_still_draft_with_an_inventory():
    w, t = brk()
    out = tr.prepare(w, t, ["BRK.B"], "a", "b")
    assert out["candidates"]["BRK.B"]["tradier"]["symbol"] == "BRK/B"
    assert out["evidence_inventory"]["BRK.B"] == {"webull_rows": 1, "tradier_rows": 1, "alternate_spellings": []}
