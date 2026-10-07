"""Execute the committed Bash orchestration with synthetic provider CLIs only."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


@pytest.fixture
def host(tmp_path):
    (tmp_path / "tools").mkdir()
    script = tmp_path / "tools/g5_tradier_comparison.sh"
    script.write_text((ROOT / "tools/g5_tradier_comparison.sh").read_text())
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    secrets = home / ".config/trading-desk"
    secrets.mkdir(parents=True)
    keys = ("TRADIER_ACCESS_TOKEN", "TASTYTRADE_CLIENT_SECRET", "TASTYTRADE_REFRESH_TOKEN",
            "WEBULL_APP_KEY", "WEBULL_APP_SECRET")
    (secrets / "env").write_text("\n".join(f"export {name}=synthetic-{name.lower()}" for name in keys))
    activate = tmp_path / ".venv/bin/activate"
    activate.parent.mkdir(parents=True)
    activate.write_text("# Synthetic runtime: PATH already supplied by the test.\n")
    bins = tmp_path / "bin"
    bins.mkdir()
    git = bins / "git"
    git.write_text('#!/bin/sh\nif [ "$2" = "--abbrev-ref" ]; then\n'
                   'echo "${TEST_BRANCH:-codex/repair-step-01-baseline}"\n'
                   'elif [ "$1" = "rev-parse" ]; then\necho "' + SHA + '"\nfi\n')
    git.chmod(0o755)
    runner = tmp_path / "fake_cli.py"
    runner.write_text('''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == "-":
    code = sys.stdin.read()
    if "session_label" in code:
        # This test fixes the market-clock guard at RTH; no provider code is run.
        sys.exit(0)
    sys.argv = args  # match the actual `python - ARG...` argv contract
    exec(compile(code, "<comparison-script>", "exec"), {"__name__": "__main__"})
    sys.exit(0)
with open(os.environ["TEST_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
module = args[1]
output = Path(args[args.index("--output") + 1])
name = output.stem
pair = {"selection_id": "fixture-selection", "call": {"symbol": "fixture-call"},
        "put": {"symbol": "fixture-put"}}
data = {"status": "OBSERVATIONS_ONLY"}
if name == "selection":
    data.update(status="OPTION_PAIR_SELECTED", option_pair=pair)
elif name in {"tradier", "tastytrade"}:
    selected = json.loads(Path(args[args.index("--option-selection") + 1]).read_text())["option_pair"]
    sid = "different-selection" if os.environ.get("TEST_MISMATCH") == name else selected["selection_id"]
    data["option_pair_comparison"] = {"selection_id": sid, "status": "MATCHED_REPORTED_FIELDS"}
elif module == "desk.webull_quote_check":
    # Use the real CLI and parser with synthetic HTTP, so caller arguments cannot
    # bypass the diagnostic's validation as they did in the original harness.
    from datetime import datetime, timezone
    from functools import partial
    from urllib.parse import urlsplit, parse_qs
    from desk import webull_quote_check as probe
    from desk.webull import INSTRUMENTS_PATH
    at = datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc)
    ids = {"SPY": "913243251", "QQQ": "913243249", "NVDA": "913257561"}
    def transport(request, timeout):
        query = parse_qs(urlsplit(request.full_url).query)
        names = query["symbols"][0].split(",")
        if urlsplit(request.full_url).path == INSTRUMENTS_PATH:
            rows = [dict(symbol=n, instrument_id=ids[n], name=n, category="US_STOCK",
                         sub_category="ETF" if n in {"SPY", "QQQ"} else "COMMON_STOCK",
                         exchange_code="NSQ", currency="USD") for n in names]
        else:
            rows = [dict(symbol=n, instrument_id=ids[n], bid="100.10", ask="100.20",
                         bid_size="25", ask_size="40", price="100.15", volume="150000.25",
                         quote_time=int(at.timestamp() * 1000),
                         last_trade_time=int(at.timestamp() * 1000)) for n in names]
        return json.dumps(rows).encode()
    probe.BoundedData.from_env = classmethod(lambda cls, max_requests: cls(
        "fixture-key", "fixture-secret", host="api.sandbox.webull.com", transport=transport,
        min_interval=0, clock=lambda: at, max_requests=max_requests))
    probe.check = partial(probe.check, clock=lambda: at, sleep=lambda _: None)
    exit_code = probe.main(args[2:])
    sys.exit(1 if os.environ.get("TEST_FAIL") == name else exit_code)
output.write_text(json.dumps(data))
sys.exit(1 if os.environ.get("TEST_FAIL") == name else 0)
''')
    python = bins / "python"
    python.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(runner)) + ' "$@"\n')
    python.chmod(0o755)
    env = {"HOME": str(home), "PATH": str(bins) + ":" + os.defpath,
           "PYTHONPATH": str(ROOT / "src"), "TEST_LOG": str(tmp_path / "calls.jsonl")}
    return script, home, env


def run(host, **changes):
    script, home, env = host
    result = subprocess.run(["bash", str(script), SHA], env=dict(env, **changes),
                            text=True, capture_output=True, timeout=30)
    folder = next((home / "Desktop").iterdir())
    assert (folder / "summary.json").exists(), (result.stdout, result.stderr)
    return result, folder, json.loads((folder / "summary.json").read_text())


def test_shared_selection_precedes_captures_and_each_exit_is_preserved(host):
    result, folder, summary = run(host)
    assert result.returncode == 0 and summary["status"] == "RECORDED_REVIEW_REQUIRED"
    assert summary["shared_pair_binding"] == {"tradier": True, "tastytrade": True}
    assert summary["live_data_approval"] == "NOT_ATTESTED" and summary["G5"] == "OPEN"
    commands = [json.loads(line) for line in Path(host[2]["TEST_LOG"]).read_text().splitlines()]
    assert "--select-only" in commands[0]
    captures = [cmd for cmd in commands if cmd[1] in {"desk.tradier_option_check", "desk.quote_check"}
                and "--select-only" not in cmd]
    assert len(captures) == 2
    for command in captures:
        assert command[command.index("--option-selection") + 1] == str(folder / "selection.json")
        assert "--option-underlying" not in command


def test_webull_command_uses_real_validator_and_collects_both_rounds(host):
    result, folder, summary = run(host)
    report = json.loads((folder / "webull.json").read_text())
    assert result.returncode == 0, report
    assert summary["command_exit_codes"]["webull"] == 0
    assert report["status"] == "OBSERVATIONS_ONLY"
    assert report["requests"] == 5
    assert len(report["rounds"]) == 2 and len(report["checks"]) == 6
    assert {item["symbol"] for item in report["checks"]} == {"SPY", "QQQ", "NVDA"}
    assert all(item["identity"] == "MATCH" for item in report["checks"])
    assert report["nbbo_coverage"] == "NOT_ESTABLISHED"


def test_unsupported_webull_interval_stops_before_requests_and_preserves_peers(host):
    script = host[0]
    script.write_text(script.read_text().replace(
        "desk.webull_quote_check --symbols SPY QQQ NVDA --rounds 2 --interval-seconds 30",
        "desk.webull_quote_check --symbols SPY QQQ NVDA --rounds 2 --interval-seconds 60"))
    result, folder, summary = run(host)
    report = json.loads((folder / "webull.json").read_text())
    assert result.returncode == 1 and summary["command_exit_codes"]["webull"] == 1
    assert report["stop_reason"] == "INVALID_PROBE_ARGUMENTS" and report["requests"] == 0
    assert summary["report_statuses"]["tradier"] == "OBSERVATIONS_ONLY"
    assert summary["report_statuses"]["tastytrade"] == "OBSERVATIONS_ONLY"
    assert summary["G5"] == "OPEN"


@pytest.mark.parametrize("failed", ["tradier", "tastytrade", "webull", "clock", "tastytrade-greeks-normalized"])
def test_a_failed_command_is_not_hidden_and_other_outputs_survive(host, failed):
    result, folder, summary = run(host, TEST_FAIL=failed)
    key = "greeks" if failed == "tastytrade-greeks-normalized" else failed
    assert result.returncode == 1 and summary["command_exit_codes"][key] == 1
    assert summary["status"] == "PARTIAL_OR_FAILED_REVIEW_REQUIRED"
    assert all((folder / (name + ".json")).exists() for name in ("tradier", "tastytrade", "webull"))


def test_successful_cli_exits_cannot_hide_a_different_option_pair(host):
    result, folder, summary = run(host, TEST_MISMATCH="tastytrade")
    assert result.returncode == 1 and not any(summary["command_exit_codes"].values())
    assert summary["shared_pair_binding"] == {"tradier": True, "tastytrade": False}


def test_wrong_branch_stops_before_any_credential_or_provider_command(host):
    script, home, env = host
    result = subprocess.run(["bash", str(script), SHA], env=dict(env, TEST_BRANCH="other"),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 1 and "STOP:" in result.stdout
    assert not Path(env["TEST_LOG"]).exists() and not list((home / "Desktop").iterdir())
