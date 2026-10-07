"""Bounded offline Greek mutations in disposable copies. Zero provider calls."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

CASES = [
    ("transaction boundary", "tastytrade_greeks.py", "if state.snapshot or flags & TX:", "if False:",
     "test_transaction_publishes_only_after_final_event"),
    ("overlapping snapshot", "tastytrade_greeks.py", "state.pending.clear()  # discard residues of overlapping snapshots",
     "pass  # mutant retains residues", "test_repeated_begin_discards_residual_newer_calculation"),
    ("latest removal", "tastytrade_greeks.py", "if state.removed_head is not None:", "if False:",
     "test_remove_latest_never_revives_older_calculation"),
    ("index consistency", "tastytrade_greeks.py", "if (millis > 999 or row.get(\"time\") != encoded_time",
     "if False and (millis > 999 or row.get(\"time\") != encoded_time",
     "test_nonzero_index_timestamp_mismatch_refuses_even_when_both_times_are_plausible"),
    ("read clock", "tastytrade_greeks.py", "if row[\"source_at\"] > at or row[\"received_at\"] > at:",
     "if False:", "test_clock_backwards_rejected_at_receive_and_use"),
    ("schema revision", "tastytrade_greeks.py", "if changed:\n                self._states.clear()",
     "if False:\n                self._states.clear()", "test_schema_change_and_reconnect_require_new_calculations"),
    ("corrupt transaction recovery", "tastytrade_greeks.py", "if state.requires_snapshot and not flags & BEGIN:",
     "if False:", "test_corrupt_transaction_tail_cannot_publish_partial_snapshot[1]"),
    ("capture state disposal", "tastytrade_transport.py", "measure.down(fault or reason or \"DISCONNECTED\", clock())",
     "pass  # mutant leaves indexed state", "test_capture_terminal_greeks_are_labelled_history_and_closed_state_unavailable[complete]"),
    ("identity binding", "tastytrade_greeks.py", "if state.identity_digest != identity.identity_digest or state.identity_epoch != identity_epoch:",
     "if False:", "test_live_health_required_every_time[identity_change]"),
    ("identity failure memory", "tastytrade_quotes.py", "self._identity_epochs[symbol] = self._identity_epochs.get(symbol, 0) + 1",
     "pass  # mutant forgets failure", "test_same_identity_recovery_never_revives_pre_failure_greek[False]"),
]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    results = []
    for name, file, old, new, test in CASES:
        with tempfile.TemporaryDirectory(prefix="greek-state-mut-") as temp:
            target = Path(temp) / "src"
            shutil.copytree(repo / "src", target)
            path = target / "desk" / file
            source = path.read_text()
            if source.count(old) != 1:
                raise SystemExit("Mutation anchor missing or ambiguous: " + name)
            path.write_text(source.replace(old, new, 1))
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-W", "error",
                "tests/test_tastytrade_greeks.py::" + test], cwd=repo,
                env={**os.environ, "PYTHONPATH": str(target)}, capture_output=True, text=True)
            caught = result.returncode == 1 and "FAILED " in result.stdout and "ERROR collecting" not in result.stdout
            results.append(dict(protection=name, caught=caught, exit_code=result.returncode))
            print(name, "CAUGHT" if caught else "NOT CAUGHT", flush=True)
            if not caught:
                print(result.stdout[-2000:], result.stderr[-500:])
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2) + "\n")
    return 0 if all(r["caught"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
