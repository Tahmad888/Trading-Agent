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
# Select once. Both captures will re-check this exact expiry, strike, call and put.
if python -m desk.tradier_option_check --environment production --symbols SPY --option-underlying SPY \
    --select-only --max-requests 3 --output "$OUT/selection.json" > "$OUT/selection.stdout" 2>&1; then
  echo "Option pair selected."
else
  echo "STOP: selection failed; inspect selection.json and selection.stdout in the report directory."
  exit 1
fi
clock_exit=0
if python -m desk.quote_measure clock --output "$OUT/clock.json" > "$OUT/clock.stdout" 2>&1; then
  clock_exit=0
else
  clock_exit=$?
fi
HC=()
if [ -f "$OUT/clock.json" ]; then
  HC=(--host-clock "$OUT/clock.json")
fi
python -m desk.tradier_option_check --environment production --symbols SPY QQQ NVDA \
  --option-selection "$OUT/selection.json" --rounds 4 --interval-seconds 60 --max-requests 9 \
  --output "$OUT/tradier.json" > "$OUT/tradier.stdout" 2>&1 &
P1=$!
python -m desk.quote_check --environment production --symbols SPY QQQ NVDA \
  --option-selection "$OUT/selection.json" --seconds 180 --reconnects 1 --max-requests 12 \
  --measure ${HC[@]+"${HC[@]}"} --output "$OUT/tastytrade.json" > "$OUT/tastytrade.stdout" 2>&1 &
P2=$!
python -m desk.webull_quote_check --symbols SPY QQQ NVDA --rounds 2 --interval-seconds 60 \
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
python - "$OUT" "$EXPECTED" "$clock_exit" "$tradier_exit" "$tastytrade_exit" "$webull_exit" "$greeks_exit" <<'PY'
import json
from pathlib import Path
import sys
from desk.quote_measure import write_report
out = Path(sys.argv[1])
names = ("clock", "tradier", "tastytrade", "webull", "greeks")
exits = dict(zip(names, map(int, sys.argv[3:]), strict=True))
reports, errors = {}, {}
for name in ("selection", "tradier", "tastytrade", "webull"):
    try:
        data = json.loads((out / (name + ".json")).read_text())
        if not isinstance(data, dict):
            raise ValueError
        reports[name] = data
    except (OSError, ValueError):
        errors[name] = "REPORT_MISSING_OR_MALFORMED"
selection_id = reports.get("selection", {}).get("option_pair", {}).get("selection_id")
binding = {name: bool(selection_id and reports.get(name, {}).get("option_pair_comparison", {})
                     .get("selection_id") == selection_id) for name in ("tradier", "tastytrade")}
summary = dict(purpose="read-only comparison collection; no decision eligibility", commit=sys.argv[2],
               status="RECORDED_REVIEW_REQUIRED" if not errors and not any(exits.values()) and all(binding.values())
                      else "PARTIAL_OR_FAILED_REVIEW_REQUIRED",
               command_exit_codes=exits, report_errors=errors, shared_pair_binding=binding,
               report_statuses={name: report.get("status") for name, report in reports.items()},
               report_directory=str(out), live_data_approval="NOT_ATTESTED", G5="OPEN")
summary = write_report(summary, out / "summary.json")
print(json.dumps(summary, indent=2))
raise SystemExit(0 if summary.get("status") == "RECORDED_REVIEW_REQUIRED" else 1)
PY
