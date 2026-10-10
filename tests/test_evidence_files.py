"""desk.evidence_files: an expected file's state, reported apart from any exit status (G5 backlog R4)."""
import json

import pytest

from desk.evidence_files import main, state

MEASURED = {"status": "MEASURED", "measured_finished_at": "2026-10-07T17:00:00+00:00",
            "offset_seconds_as_printed": "+0.104", "plus_minus_seconds_as_printed": "0.020"}
PAIR = {"selection_id": "sel-1", "call": {"symbol": "SPY261016C00777000"}, "put": {"symbol": "SPY261016P00777000"}}


@pytest.mark.parametrize("kind,content,expected", [
    ("report", None, "MISSING"),
    ("report", "{\"status\": ", "MALFORMED"),
    ("report", "[]", "MALFORMED"),
    ("report", "{}", "MALFORMED"),
    ("report", {"status": 3}, "MALFORMED"),
    ("report", {"status": "UNAVAILABLE"}, "VALID"),            # a well-formed failure report is still a report
    ("report", {"status": "OBSERVATIONS_ONLY"}, "VALID"),
    ("clock", None, "MISSING"),
    ("clock", "not json", "MALFORMED"),
    ("clock", {"status": "MEASURED"}, "MALFORMED"),             # no time, no printed offset
    ("clock", {"status": "SOMETHING"}, "MALFORMED"),
    ("clock", {"status": "UNAVAILABLE", "code": "SNTP_TIMEOUT"}, "UNAVAILABLE"),
    ("clock", MEASURED, "VALID"),
    ("selection", {"status": "OPTION_PAIR_SELECTED"}, "MALFORMED"),
    ("selection", {"status": "OPTION_PAIR_SELECTED", "option_pair": dict(PAIR, selection_id="")}, "MALFORMED"),
    ("selection", {"status": "OPTION_PAIR_SELECTED", "option_pair": dict(PAIR, put=None)}, "MALFORMED"),
    ("selection", {"status": "OPTION_PAIR_SELECTED", "option_pair": dict(PAIR, call={"symbol": 5})}, "MALFORMED"),
    ("selection", {"status": "OPTION_PAIR_SELECTED", "option_pair": PAIR}, "VALID"),
])
def test_state_separates_missing_malformed_unavailable_and_valid(tmp_path, kind, content, expected):
    path = tmp_path / "evidence.json"
    if content is not None:
        path.write_text(content if isinstance(content, str) else json.dumps(content))
    assert state(path, kind) == expected


def test_cli_prints_the_state_and_refuses_unknown_kinds(tmp_path, capsys):
    path = tmp_path / "clock.json"
    path.write_text(json.dumps(MEASURED))
    assert main(["clock", str(path)]) == 0 and capsys.readouterr().out == "VALID\n"
    assert main(["clock", str(tmp_path / "absent.json")]) == 0 and capsys.readouterr().out == "MISSING\n"
    assert main(["receipt", str(path)]) == 2 and main(["clock"]) == 2
    with pytest.raises(ValueError):
        state(path, "receipt")
