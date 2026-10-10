"""G5 backlog R5: diagnostic timing, local-argument order, no-overwrite outputs, attempt chronology.

Synthetic transports and temporary stores only; no provider, credential or network use.
"""
from datetime import timedelta
import json
import time

import pytest

from desk import close_jobs as cj
from desk import quote_measure
from desk import snapshot_quote_check as snap
from desk import webull_quote_check as wq
from desk.calendar import clock
from desk.tastytrade_quotes import QuoteUnavailable
from desk.webull import SNAPSHOT_PATH, WebullError
from tests.test_close_recovery_audit import prepare, waiting_job
from tests.test_close_jobs import TUE, at
from tests.test_webull_quote_check import AT, Transport

WEBULL_ENV = ("WEBULL_APP_KEY", "WEBULL_APP_SECRET", "WEBULL_ACCESS_TOKEN")


class FakeTime:
    """Monotonic time that only moves when the code under test sleeps or the wire is used."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0, seconds)

    def wall(self):
        return AT + timedelta(seconds=self.t - 1000.0)


@pytest.fixture
def fake_time(monkeypatch):
    ft = FakeTime()
    monkeypatch.setattr(time, "monotonic", ft.monotonic)
    monkeypatch.setattr(time, "sleep", ft.sleep)
    return ft


def slow(ft, transport, seconds):
    def call(req, timeout):
        ft.t += seconds                                   # time on the wire
        return transport(req, timeout)
    return call


# ------------------------------------------------------------------ B-1: transport bracket ------
def test_transport_bracket_excludes_the_throttle_wait_and_workload_is_labelled(fake_time):
    source = wq.BoundedData("fixture-key", "fixture-secret", transport=slow(fake_time, Transport(), 0.05),
                            host="api.sandbox.webull.com", min_interval=2.1, clock=fake_time.wall, max_requests=5)
    report = wq.check(source, ["NVDA"], rounds=2, interval_seconds=0, clock=fake_time.wall, sleep=fake_time.sleep)
    assert report["status"] == "OBSERVATIONS_ONLY" and report["requests"] == 3
    second = report["rounds"][1]["groups"][0]                 # throttled: same path, 2.1 s interval owed
    transport = second["transport_bracket"]
    assert transport["state"] == "MEASURED" and transport["path"] == SNAPSHOT_PATH
    assert transport["elapsed_ms"] == pytest.approx(50, abs=0.01)
    assert transport["throttle_and_signing_ms"] == pytest.approx(2100, abs=0.01)
    assert second["workload_elapsed_ms"] == pytest.approx(2150, abs=0.01)
    assert "request_elapsed_ms" not in second and "Not a latency" in second["workload_note"]
    assert "Not a provider send or receipt time" in transport["basis"]
    assert report["checks"][-1]["transport_bracket"] == transport
    first = report["rounds"][0]["groups"][0]["transport_bracket"]
    assert first["throttle_and_signing_ms"] == pytest.approx(0, abs=0.01)   # control: no wait owed


def test_failed_transport_is_bracketed_and_budget_refusals_have_no_bracket(fake_time):
    def fail(req, timeout):
        raise OSError("fixture network down")
    source = wq.BoundedData("fixture-key", "fixture-secret", transport=fail, host="api.sandbox.webull.com",
                            min_interval=0, clock=fake_time.wall, max_requests=1)
    report = wq.check(source, ["NVDA"], rounds=1, clock=fake_time.wall, sleep=fake_time.sleep)
    assert report["status"] == "UNAVAILABLE" and report["requests"] == 1
    assert [b["outcome"] for b in source.transport_brackets] == ["RAISED"]
    with pytest.raises(wq.ProbeStop, match="REQUEST_BUDGET_EXHAUSTED"):
        source.snapshot(["NVDA"], category="US_STOCK")
    assert len(source.transport_brackets) == 1               # nothing was sent, nothing bracketed


def test_a_source_without_brackets_is_labelled_not_recorded():
    inner = wq.BoundedData("fixture-key", "fixture-secret", transport=Transport(), host="api.sandbox.webull.com",
                           min_interval=0, clock=lambda: AT)

    class Plain:                                          # same replies, no bracket record
        requests = 0
        security_metadata, snapshot = inner.security_metadata, inner.snapshot

    report = wq.check(Plain(), ["NVDA"], rounds=1, clock=lambda: AT, sleep=lambda _: None)
    assert report["status"] == "OBSERVATIONS_ONLY"
    assert report["rounds"][0]["groups"][0]["transport_bracket"]["state"] == "NOT_RECORDED"


# ----------------------------------------------- B-2: local arguments before any client ------
@pytest.fixture
def no_webull_client(monkeypatch):
    for name in WEBULL_ENV:
        monkeypatch.delenv(name, raising=False)
    sent = []
    monkeypatch.setattr(wq, "_read_once", lambda req, timeout: sent.append(req) or b"[]")
    return sent


@pytest.mark.parametrize("argv,reason", [
    (["--rounds", "9"], "INVALID_PROBE_ARGUMENTS"),
    (["--interval-seconds", "nan"], "INVALID_PROBE_ARGUMENTS"),
    (["--symbols", "SPY", "Q$Q"], "INVALID_PROBE_SYMBOL"),
    (["--max-requests", "50"], "INVALID_REQUEST_BUDGET"),       # control: refused before credentials already
])
def test_webull_argument_errors_win_over_missing_credentials(tmp_path, no_webull_client, argv, reason, capsys):
    out = tmp_path / "webull.json"
    assert wq.main([*argv, "--output", str(out)]) == 1
    report = json.loads(out.read_text())
    assert report["status"] == "UNAVAILABLE" and report["stop_reason"] == reason and report["requests"] == 0
    assert no_webull_client == []


def test_webull_valid_arguments_without_credentials_report_unavailable_safely(tmp_path, no_webull_client):
    out = tmp_path / "webull.json"
    assert wq.main(["--output", str(out)]) == 1
    report = json.loads(out.read_text())
    assert report["status"] == "UNAVAILABLE" and report["error_type"] == "WebullError"
    assert "stop_reason" not in report and report["requests"] == 0 and no_webull_client == []


@pytest.mark.parametrize("argv,reason", [
    (["--symbols", "SPY", "--rounds", "9"], "SNAPSHOT_PROBE_ARGUMENTS_INVALID"),
    (["--symbols", "SPY", "SPY"], "SNAPSHOT_PROBE_ARGUMENTS_INVALID"),
    (["--symbols", "S$Y"], "IDENTITY_INVALID"),
    (["--symbols", "SPY", "--option-strike", "-5", "--option-underlying", "SPY"], "SNAPSHOT_PROBE_ARGUMENTS_INVALID"),
    (["--symbols", "SPY", "--max-requests", "500"], "INVALID_REST_CONFIG"),   # control: ReadClient checked it first
    (["--symbols", "SPY"], "CREDENTIALS_MISSING"),                            # control: valid input, no credentials
])
def test_snapshot_argument_errors_win_over_missing_credentials(tmp_path, monkeypatch, argv, reason):
    out = tmp_path / "snapshot.json"
    sent = []
    monkeypatch.setattr(snap, "diagnostic", lambda *a, **k: sent.append(a) or {})
    assert snap.main(["--environment", "production", *argv, "--output", str(out)], env={}) == 1
    report = json.loads(out.read_text())
    assert report["status"] == "UNAVAILABLE" and report["reason"] == reason and report["requests"] == 0
    assert sent == []


# --------------------------------------------------------- B-3: never overwrite evidence ------
ORIGINAL = b"ORIGINAL evidence \x00\xff bytes\n"


def test_webull_refuses_an_existing_output_before_any_client(tmp_path, no_webull_client, monkeypatch):
    out = tmp_path / "webull.json"
    out.write_bytes(ORIGINAL)
    monkeypatch.setattr(wq.BoundedData, "from_env", classmethod(lambda cls, **kw: pytest.fail("client built")))
    with pytest.raises(SystemExit) as stop:
        wq.main(["--output", str(out)])
    assert stop.value.code == 2 and out.read_bytes() == ORIGINAL


def test_snapshot_refuses_an_existing_output_before_any_client(tmp_path, monkeypatch):
    out = tmp_path / "snapshot.json"
    out.write_bytes(ORIGINAL)
    monkeypatch.setattr(snap, "ReadClient", lambda *a, **k: pytest.fail("client built"))
    with pytest.raises(SystemExit) as stop:
        snap.main(["--environment", "production", "--symbols", "SPY", "--output", str(out)], env={})
    assert stop.value.code == 2 and out.read_bytes() == ORIGINAL


def test_existing_symlink_output_is_refused_too(tmp_path, no_webull_client):
    out = tmp_path / "webull.json"
    out.symlink_to(tmp_path / "dangling-target.json")
    with pytest.raises(SystemExit):
        wq.main(["--output", str(out)])
    assert out.is_symlink() and not (tmp_path / "dangling-target.json").exists()


def test_webull_output_created_during_the_run_is_kept_and_reported(tmp_path, no_webull_client, monkeypatch, capsys):
    out = tmp_path / "webull.json"

    def racing_from_env(cls, **kw):
        out.write_bytes(ORIGINAL)                       # another process takes the path mid-run
        raise WebullError("Webull app key and secret are not set")
    monkeypatch.setattr(wq.BoundedData, "from_env", classmethod(racing_from_env))
    assert wq.main(["--output", str(out)]) == 1
    assert out.read_bytes() == ORIGINAL
    printed = json.loads(capsys.readouterr().out)
    assert printed["output_write"] == "OUTPUT_EXISTS_NOT_OVERWRITTEN" and printed["status"] == "UNAVAILABLE"


def test_snapshot_output_created_during_the_run_is_kept_and_reported(tmp_path, monkeypatch, capsys):
    out = tmp_path / "snapshot.json"

    def racing_client(*a, **k):
        out.write_bytes(ORIGINAL)
        raise QuoteUnavailable("CREDENTIALS_MISSING")
    monkeypatch.setattr(snap, "ReadClient", racing_client)
    assert snap.main(["--environment", "production", "--symbols", "SPY", "--output", str(out)], env={}) == 1
    assert out.read_bytes() == ORIGINAL
    assert json.loads(capsys.readouterr().out)["output_write"] == "OUTPUT_EXISTS_NOT_OVERWRITTEN"


def test_a_write_error_is_reported_as_a_write_failure_not_as_existing_output(tmp_path, no_webull_client, capsys):
    blocker = tmp_path / "not-a-directory"
    blocker.write_bytes(ORIGINAL)
    assert wq.main(["--output", str(blocker / "webull.json")]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["output_write"].startswith("OUTPUT_WRITE_FAILED_") and blocker.read_bytes() == ORIGINAL


def test_a_successful_exclusive_write_is_unchanged_and_shared_callers_keep_overwriting(tmp_path):
    out = tmp_path / "new" / "report.json"
    report, failed = quote_measure.write_new_report({"status": "OBSERVATIONS_ONLY"}, out)
    assert failed == 0 and report == {"status": "OBSERVATIONS_ONLY"}
    assert json.loads(out.read_text()) == {"status": "OBSERVATIONS_ONLY"}
    quote_measure.write_report({"status": "SECOND"}, out)   # deliberate internal rewrite (e.g. summary.json)
    assert json.loads(out.read_text()) == {"status": "SECOND"}


def test_a_credential_in_a_report_that_cannot_be_written_is_not_printed(tmp_path):
    out = tmp_path / "report.json"
    out.write_bytes(ORIGINAL)
    shown, failed = quote_measure.write_new_report({"status": "X", "leak": "fixture-secret-value"}, out,
                                                   env={"WEBULL_APP_SECRET": "fixture-secret-value"})
    assert failed == 1 and shown["status"] == "REPORT_REJECTED" and "fixture-secret-value" not in json.dumps(shown)
    assert out.read_bytes() == ORIGINAL


# ------------------------------------------------- A-O2 / N1: this attempt's own chronology ------
def test_attempt_reports_its_own_chronology_not_a_later_workers_last_attempt(tmp_path, monkeypatch):
    log, source = waiting_job(tmp_path)
    real, calls = log.signals.close_job, []

    def later_worker_committed_first(day):
        job = real(day)
        calls.append(day)
        if len(calls) == 2:                              # the read after this attempt's commit
            job["history"].append({"attempt": job["attempts"] + 1, "at": at(TUE, 10, 9).isoformat(),
                                   "status": "FINISHED", "kind": "close_recovery"})
        return job
    monkeypatch.setattr(log.signals, "close_job", later_worker_committed_first)
    started, evaluated, published = at(TUE, 9, 59), at(TUE, 10, 1), at(TUE, 10, 2)
    rec = prepare(log, source, started, evaluated, published)
    mine = rec.discovery["this_attempt"]
    assert mine["committed"] is True and clock(mine["final_at"]) == published
    assert clock(mine["started_at"]) == started and clock(mine["evaluated_at"]) == evaluated
    assert "earnings evidence" in mine["evaluated_at_basis"]
    assert rec.discovery["close_job"]["last_attempt"]["at"] == at(TUE, 10, 9).isoformat()   # job history kept
    assert rec.discovery["close_job"]["last_attempt_is_this_attempt"] is False


def test_attempt_chronology_control_when_no_other_worker_ran(tmp_path):
    log, source = waiting_job(tmp_path)
    rec = prepare(log, source, at(TUE, 9, 59), at(TUE, 10, 1), at(TUE, 10, 2))
    assert rec.discovery["close_job"]["last_attempt_is_this_attempt"] is True
    assert rec.discovery["this_attempt"]["final_at"] == rec.discovery["close_job"]["last_attempt"]["at"]


@pytest.mark.parametrize("tool", ["webull", "snapshot"])
def test_successful_observations_still_exit_nonzero_when_the_report_cannot_be_written(tmp_path, monkeypatch,
                                                                                    capsys, tool):
    blocker = tmp_path / "not-a-directory"
    blocker.write_bytes(ORIGINAL)
    out = str(blocker / "report.json")
    good = {"purpose": "fixture", "status": "OBSERVATIONS_ONLY"}
    if tool == "webull":
        monkeypatch.setattr(wq.BoundedData, "from_env", classmethod(lambda cls, **kw: object()))
        monkeypatch.setattr(wq, "check", lambda *a, **k: dict(good))
        code = wq.main(["--output", out])
    else:
        monkeypatch.setattr(snap, "ReadClient", lambda *a, **k: object())
        monkeypatch.setattr(snap, "diagnostic", lambda *a, **k: dict(good))
        code = snap.main(["--environment", "production", "--symbols", "SPY", "--output", out], env={})
    printed = json.loads(capsys.readouterr().out)
    assert code == 1 and printed["status"] == "OBSERVATIONS_ONLY"
    assert printed["output_write"].startswith("OUTPUT_WRITE_FAILED_") and blocker.read_bytes() == ORIGINAL
