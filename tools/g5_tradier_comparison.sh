#!/bin/bash
# Run explicitly with bash and the full reviewed commit SHA. Market-data only.
set +x
set -euo pipefail
if [ "${1:-}" = "--help" ]; then
  echo "Usage: bash tools/g5_tradier_comparison.sh FULL_REVIEWED_COMMIT_SHA"
  exit 0
fi
if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
  echo "STOP: supply the full reviewed commit SHA."
  exit 1
fi
EXPECTED="$1"
cd "$(dirname "$0")/.."
if [ "$(git rev-parse --abbrev-ref HEAD)" != "codex/repair-step-01-baseline" ] ||
   [ "$(git rev-parse HEAD)" != "$EXPECTED" ] ||
   [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "STOP: expected clean dedicated branch at the reviewed commit."
  exit 1
fi
source .venv/bin/activate
# This ten-minute margin is a diagnostic duration allowance, never a trading cutoff.
python - <<'PY'
from datetime import datetime, timedelta, timezone
from desk.calendar import clock, session
from desk.tastytrade_quotes import session_label
now = datetime.now(timezone.utc)
if session_label(now) != "RTH":
    raise SystemExit("STOP: run during a regular trading session.")
if session(clock(now).date())[1] - clock(now) < timedelta(minutes=10):
    raise SystemExit("STOP: less than ten minutes remain for this bounded diagnostic.")
PY
source "$HOME/.config/trading-desk/env"
if [ -z "${TRADIER_ACCESS_TOKEN:-}" ]; then
  read -rs -p "Tradier production token (hidden): " TRADIER_ACCESS_TOKEN
  echo
fi
export TRADIER_ACCESS_TOKEN
python - <<'PY'
import os
required = ("TRADIER_ACCESS_TOKEN", "TASTYTRADE_CLIENT_SECRET", "TASTYTRADE_REFRESH_TOKEN",
            "WEBULL_APP_KEY", "WEBULL_APP_SECRET")
missing = [name for name in required if not os.environ.get(name)]
if missing:
    raise SystemExit("STOP: missing credentials: " + ", ".join(missing))
PY
OUT="$(mktemp -d "$HOME/Desktop/g5-step3-comparison-XXXXXX")"
echo "Report directory: $OUT"
# VALID, MISSING, MALFORMED (or UNAVAILABLE for a clock) for an expected file; never an exit status.
evidence() {
  python - "$1" "$2" <<'PY'
import sys
from desk.evidence_files import state
print(state(sys.argv[2], sys.argv[1]))
PY
}
# Select once. Both captures will re-check this exact expiry, strike, call and put.
if python -m desk.tradier_option_check --environment production --symbols SPY --option-underlying SPY \
    --select-only --max-requests 3 --output "$OUT/selection.json" > "$OUT/selection.stdout" 2>&1; then
  selection_exit=0
else
  selection_exit=$?
fi
selection_state="$(evidence selection "$OUT/selection.json")"
if [ "$selection_exit" -ne 0 ] || [ "$selection_state" != "VALID" ]; then
  echo "STOP: selection failed (exit $selection_exit, selection.json $selection_state); inspect selection.json and selection.stdout in the report directory."
  exit 1
fi
echo "Option pair selected (exit 0, selection.json VALID)."
clock_exit=0
if python -m desk.quote_measure clock --output "$OUT/clock.json" > "$OUT/clock.stdout" 2>&1; then
  clock_exit=0
else
  clock_exit=$?
fi
# Only a valid measured host-clock file is attached; anything else is named in the summary.
HC=()
if [ "$(evidence clock "$OUT/clock.json")" = "VALID" ]; then
  HC=(--host-clock "$OUT/clock.json")
else
  echo "Host clock evidence is not valid; captures run without --host-clock (see summary.json)."
fi
python -m desk.tradier_option_check --environment production --symbols SPY QQQ NVDA \
  --option-selection "$OUT/selection.json" --rounds 4 --interval-seconds 60 --max-requests 9 \
  --output "$OUT/tradier.json" > "$OUT/tradier.stdout" 2>&1 &
P1=$!
python -m desk.quote_check --environment production --symbols SPY QQQ NVDA \
  --option-selection "$OUT/selection.json" --seconds 180 --reconnects 1 --max-requests 12 \
  --measure ${HC[@]+"${HC[@]}"} --output "$OUT/tastytrade.json" > "$OUT/tastytrade.stdout" 2>&1 &
P2=$!
python -m desk.webull_quote_check --symbols SPY QQQ NVDA --rounds 2 --interval-seconds 30 \
  --max-requests 5 ${HC[@]+"${HC[@]}"} --output "$OUT/webull.json" > "$OUT/webull.stdout" 2>&1 &
P3=$!
if wait "$P1"; then tradier_exit=0; else tradier_exit=$?; fi
if wait "$P2"; then tastytrade_exit=0; else tastytrade_exit=$?; fi
if wait "$P3"; then webull_exit=0; else webull_exit=$?; fi
greeks_exit=0
if python -m desk.option_conventions tastytrade-greeks --capture "$OUT/tastytrade.json" \
    --output "$OUT/tastytrade-greeks-normalized.json" > "$OUT/greeks.stdout" 2>&1; then
  greeks_exit=0
else
  greeks_exit=$?
fi
# Preserve command exits and shared-pair binding; recording data is not a live-data PASS.
python - "$OUT" "$EXPECTED" "${#HC[@]}" "$selection_exit" "$clock_exit" "$tradier_exit" "$tastytrade_exit" \
  "$webull_exit" "$greeks_exit" <<'PY'
import json
from pathlib import Path
import sys
from desk.evidence_files import state
from desk.quote_measure import write_report
out = Path(sys.argv[1])
names = ("selection", "clock", "tradier", "tastytrade", "webull", "greeks")
exits = dict(zip(names, map(int, sys.argv[4:]), strict=True))
# Every expected file, checked apart from its command's exit status.
files = {"selection": ("selection.json", "selection"), "clock": ("clock.json", "clock"),
         "tradier": ("tradier.json", "report"), "tastytrade": ("tastytrade.json", "report"),
         "webull": ("webull.json", "report"), "greeks": ("tastytrade-greeks-normalized.json", "report")}
states = {name: state(out / file, kind) for name, (file, kind) in files.items()}
reports = {name: json.loads((out / files[name][0]).read_text()) for name, s in states.items()
           if s == "VALID" and name != "clock"}
errors = {name: "REPORT_" + s for name, s in states.items() if s != "VALID"}
selection_id = reports.get("selection", {}).get("option_pair", {}).get("selection_id")
binding = {name: bool(selection_id and isinstance(reports.get(name, {}).get("option_pair_comparison"), dict)
                     and reports[name]["option_pair_comparison"].get("selection_id") == selection_id)
           for name in ("tradier", "tastytrade")}
summary = dict(purpose="read-only comparison collection; no decision eligibility", commit=sys.argv[2],
               status="RECORDED_REVIEW_REQUIRED" if not errors and not any(exits.values()) and all(binding.values())
                      else "PARTIAL_OR_FAILED_REVIEW_REQUIRED",
               command_exit_codes=exits, evidence_files=states, report_errors=errors,
               host_clock_passed_to_captures=sys.argv[3] != "0", shared_pair_binding=binding,
               report_statuses={name: report.get("status") for name, report in reports.items()},
               report_directory=str(out), live_data_approval="NOT_ATTESTED", G5="OPEN")
summary = write_report(summary, out / "summary.json")
print(json.dumps(summary, indent=2))
raise SystemExit(0 if summary.get("status") == "RECORDED_REVIEW_REQUIRED" else 1)
PY
