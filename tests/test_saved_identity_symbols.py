"""G5 re-audit R3b: a saved identity that names no usable symbol is a capture-level refusal.

The saved Greek audit groups identity rows by `desk_symbol`. A row whose symbol is
missing, not text or not a symbol cannot be attributed, so it could contradict any
observation: no observation is audited. In-memory captures only; zero network.
"""
from copy import deepcopy
import hashlib
import json
import subprocess
import sys

import pytest

from desk import saved_greek_audit as sga
from tests.test_afterhours_readiness import saved

LABELS = {"decision_eligibility": "NOT_ATTESTED", "live_state": "NOT_RESTORED", "provider_calls": 0,
          "quote_size_units": "UNRESOLVED_NO_CONVERSION", "tradier_conventions": "PROVISIONAL_NOT_CHANGED"}
MISSING = object()
OPTION = 2                                                        # identity_capture[2] is the option


@pytest.mark.parametrize("value,code", [
    ([], "IDENTITY_SYMBOL_NOT_TEXT"), ({}, "IDENTITY_SYMBOL_NOT_TEXT"), (["SPY"], "IDENTITY_SYMBOL_NOT_TEXT"),
    (True, "IDENTITY_SYMBOL_NOT_TEXT"), (False, "IDENTITY_SYMBOL_NOT_TEXT"), (5, "IDENTITY_SYMBOL_NOT_TEXT"),
    (1.5, "IDENTITY_SYMBOL_NOT_TEXT"), (None, "IDENTITY_SYMBOL_MISSING"), (MISSING, "IDENTITY_SYMBOL_MISSING"),
    ("", "IDENTITY_SYMBOL_INVALID_TEXT"), ("   ", "IDENTITY_SYMBOL_INVALID_TEXT"),
    ("SPY ", "IDENTITY_SYMBOL_INVALID_TEXT"), ("SP\nY", "IDENTITY_SYMBOL_INVALID_TEXT"),
    ("SPY\x00", "IDENTITY_SYMBOL_INVALID_TEXT"),
])
@pytest.mark.parametrize("row", [0, OPTION])
def test_an_unattributable_identity_refuses_the_whole_capture(value, code, row):
    capture = deepcopy(saved())
    if value is MISSING:
        del capture["identity_capture"][row]["desk_symbol"]
    else:
        capture["identity_capture"][row]["desk_symbol"] = value
    out = sga.audit(capture, "a" * 64)
    assert out["status"] == "CAPTURE_SHAPE_INVALID" and out["input_sha256"] == "a" * 64
    assert out["issues"] == [{"path": f"identity_capture[{row}].desk_symbol", "code": code}]
    assert out["observations"] == [] and out["retained_greek_count"] == 0
    assert {k: out[k] for k in LABELS} == LABELS


def test_every_unattributable_row_is_listed():
    capture = deepcopy(saved())
    capture["identity_capture"][0]["desk_symbol"] = []
    capture["identity_capture"][1]["desk_symbol"] = ""
    out = sga.audit(capture, "a" * 64)
    assert out["issues"] == [{"path": "identity_capture[0].desk_symbol", "code": "IDENTITY_SYMBOL_NOT_TEXT"},
                             {"path": "identity_capture[1].desk_symbol", "code": "IDENTITY_SYMBOL_INVALID_TEXT"}]


# ---- controls -------------------------------------------------------------------------------
def test_control_healthy_identities_keep_the_historical_labels_and_greek_units():
    out = sga.audit(saved(), "a" * 64)
    assert out["status"] == "OBSERVATIONS_AUDITED" and out["issues"] == []
    seen, = out["observations"]
    assert seen["status"] == "INDEXED_OBSERVATION_VALID" and seen["label"] == "HISTORICAL_OBSERVATION"
    assert seen["snapshot_acceptance"] == "NOT_ATTESTED" and seen["fields"]["delta"]["status"] == "DOCUMENTED"
    assert {k: out[k] for k in LABELS} == LABELS


def test_control_conflicting_identities_for_one_symbol_stay_record_level():
    capture = deepcopy(saved())
    other = deepcopy(capture["identity_capture"][OPTION])
    other["streamer_symbol"] = ".OTHER"
    capture["identity_capture"].append(other)
    out = sga.audit(capture, "a" * 64)
    assert out["status"] == "PARTIAL_OBSERVATIONS" and out["issues"] == []
    assert out["observations"][0]["reason"] == "SAVED_ROW_OR_IDENTITY_INVALID"


@pytest.mark.parametrize("field,value", [("kind", []), ("streamer_symbol", {}), ("kind", None)])
def test_control_other_bad_identity_fields_are_attributable_and_isolated(field, value):
    capture = deepcopy(saved())
    capture["identity_capture"][OPTION][field] = value            # still names its symbol
    out = sga.audit(capture, "a" * 64)
    assert out["status"] == "PARTIAL_OBSERVATIONS" and out["issues"] == []
    assert out["observations"][0]["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("value", [[], {}, None, 5])
def test_control_a_bad_record_symbol_is_isolated_and_keeps_the_peer(value):
    capture = deepcopy(saved())
    good = capture["measurements"]["records"][0]
    bad = deepcopy(good)
    bad["symbol"] = value
    capture["measurements"]["records"] = [bad, deepcopy(good)]
    out = sga.audit(capture, "a" * 64)
    assert out["status"] == "PARTIAL_OBSERVATIONS"
    assert [o["status"] for o in out["observations"]] == ["UNAVAILABLE", "INDEXED_OBSERVATION_VALID"]


# ---- command ------------------------------------------------------------------------------
def test_command_returns_a_json_refusal_and_nonzero_exit_without_a_traceback(tmp_path):
    capture = deepcopy(saved())
    capture["identity_capture"][0]["desk_symbol"] = {}
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(capture, default=str))
    out = tmp_path / "audit.json"
    run = subprocess.run([sys.executable, "-m", "desk.saved_greek_audit", "--capture", str(path),
                          "--output", str(out)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 1 and run.stderr == "" and "Traceback" not in run.stdout
    assert json.loads(run.stdout) == {"status": "CAPTURE_SHAPE_INVALID", "retained_greek_count": 0,
                                      "live_state": "NOT_RESTORED", "provider_calls": 0}
    report = json.loads(out.read_text())
    assert report["input_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["issues"] == [{"path": "identity_capture[0].desk_symbol", "code": "IDENTITY_SYMBOL_NOT_TEXT"}]
