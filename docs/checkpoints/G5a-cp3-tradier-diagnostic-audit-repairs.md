# G5a CP3 — Tradier diagnostic audit repairs

2026-10-07. Taz authorized Astra to implement her audit of
`5b43e5de92ec183074a28ed0a1b10fa846945cc0`. Local changes were absent; the remote
dedicated branch was verified at that SHA before coding. Hierarchy remains
**G5 → G5a → parent Checkpoint 3 → six children**. This is the bounded Tradier
preflight Step 2 repair preparing Step 3, not Step 09 or trading activation.

## Requirements recorded before code

| Finding | Requirement / evidence | Producers and consumers |
|---|---|---|
| F1 | Timestamp advancement must not certify freshness. Astra reproduced PASS with 15-minute-old/future timestamps, missing prices and a final missing option. Report latest per-field prices/ages/failures under the existing 60-second quote policy separately from advancement; no new threshold. **Checked** against code and injected transport. | `tradier_option_check` normalization and final verdicts → diagnostic JSON only. Risk and quote-service policies unchanged. |
| F2 | Invalid calendar dates become structured field/identity failures. Astra reproduced uncaught errors with February 30 and an invalid OCC expiration. Valid peer data survives. **Checked**. | `option_conventions.provider_time`, diagnostic OCC/chain validation → Tradier report. |
| F3 | Select a single economic call/put pair and bind both captures to it. Different midpoint/trade references can select different strikes. Astra reproduced 780 vs 781 using the same chain. **Checked**. | Sanitized diagnostic selection record → Tradier and tastytrade diagnostics; current instrument metadata checks; iMac comparison script. No approved mapping/ContractBook is created. |
| D1 | `parse_float=Decimal` affects JSON floating numbers; integers/strings and some raw samples survive. Correct blanket recapture claims; recover only retained evidence, never manufacture missing session data. **Checked** against decoder/recorder. | Existing checkpoint/handoff/quote documentation. |
| D2 | Describe single-source multiplier metadata truthfully: the existing diagnostic accepts one positive metadata value when the other is absent and rejects conflicts. Record provenance; no risk change or universal 100. **Checked**. | Conventions docs and diagnostic exposure provenance. |

Primary basis: [Tradier market data](https://docs.tradier.com/docs/market-data)
separates real-time quotes from hourly ORATS Greeks;
[Tradier quote fields](https://docs.tradier.com/docs/quotes) distinguish side/trade
dates and contract size;
[tastytrade option metadata](https://developer.tastytrade.com/reference/instruments/getInstrumentsEquityOptionsSymbol/)
provides option terms, chain type and `shares-per-contract` (deliverable shares).
These do not establish Tradier option quote-size units or full adjusted deliverables.
No claim that every retail trader uses this exact diagnostic design.

## Acceptance examples

1. Advancing source times 15 minutes old or in the future never produce quote-freshness PASS.
2. A fresh trade does not freshen old/missing bid/ask fields; report trade separately.
3. Final missing/identity-refused option cannot reuse earlier PASS; healthy peers survive.
4. A valid positive uncrossed book with supported times within existing policy passes;
   an unchanged side within policy can pass without timestamp advancement.
5. Invalid naive day/month/hour and invalid OCC expiration produce safe failure codes;
   malformed Greek dates do not discard valid quotes or peer analytics.
6. Underlying crossing a strike midpoint cannot change a shared explicit pair.
   Missing or contradictory counterpart metadata cannot be silently substituted.
7. Quote age stays separate from Greek age; no Greek threshold or clock tolerance.
8. All tests use injected transports; no order/account/schedule/host changes.

## Bounded implementation plan

Repair F1, then F2, then F3; run focused checks after each. The shared selection
record is diagnostic input, not trusted provenance or approval. Both providers
re-check the selected option terms; current metadata limitations are visible.
An explicit tastytrade pair bypasses auto-selection without altering existing
non-pair behavior. No stream/valuation/approval architecture is added.

Run the final strict combined suite once stable on the existing Python runtime,
plus `git diff --check`, and record actual results. Commit and fast-forward push
verified changes to the dedicated branch; stop at this checkpoint for independent
review and subsequent iMac/live acceptance. Self-review is not independent review.

## Rollback and unresolved work

Revert this bounded repair commit to restore the prior diagnostic behavior; keep
the prior Decimal repair. G5/parent CP3 stay open. Tradier stream/reconnect,
live source comparison, actual-host verification, opening-window consolidated
volume, current Greek reduction where needed and the existing acceptance matrix
remain pending. Option quote-size units and several Greek definitions remain
unresolved/inferred. Step 09 stays paused.

## Results

Implemented F1/F2/F3 and corrected D1/D2. No provider call or host update was made.

| Finding | Change / checked result |
|---|---|
| F1 | `price_time_view`/`latest_policy_view` report bid, ask and trade independently at completion using `RiskLimits.max_quote_age` (60 s). Future times at receipt or completion fail; positive uncrossed prices are required for the two-sided field check. Final missing data cannot inherit an earlier PASS. Timestamp advancement remains a separate observation. |
| F2 | The field-specific naive date parser and shared OCC parser translate invalid calendar dates to safe failure codes. Invalid extra chain rows are listed by index/reason; valid peer quotes and analytics survive. |
| F3 | `diagnostic_pair` creates an explicit current-day diagnostic selection. `tradier_option_check --select-only` produces it once; both CLIs accept `--option-selection`. Tradier quote terms and tastytrade's fresh Standard chain/instrument terms are checked. Contradicted tastytrade options never enter the capture service; peers remain available. Missing size/classification yields PARTIAL, never a guessed default. Invalid selection files stop before provider calls. |
| D1 | Decoder/raw-sample claims corrected in the original checkpoint/handoff, quotes guide and acceptance matrix. Decimal acceptance from the base commit is retained; no missing numeric value is reconstructed. |
| D2 | Exposure illustrations keep the base's single-source metadata behavior, disclose both supplied sources and reject conflicts. The documentation no longer claims both sources are always mandatory. |

The tastytrade instrument capture adds only `option-chain-type` and
`shares-per-contract` supporting metadata; its identity digest and trading eligibility
do not change. Comparing Tradier `contract_size` with deliverable shares is labelled
`MATCHED_REPORTED_FIELDS`, not complete deliverable or premium-unit attestation.

`tools/g5_tradier_comparison.sh` is a committed Bash command: require the full reviewed
SHA and clean dedicated branch; require RTH with enough time to finish; select once;
start bounded overlapping Tradier/tastytrade/Webull captures using that same selection;
record every command exit, report failure and pair binding; normalize retained Greeks
offline. Tradier retains the earlier overall ceiling of 12 (selection cap 3, capture
cap 9; the normal four-round pipeline uses 7); tastytrade cap 12 and Webull cap 5 are
unchanged. The ten-minute margin is a diagnostic duration allowance, not a trade rule.
No `wait ... || true` masks failures. Collection is always labelled requiring review.

### Verification (offline only)

- Focused strict checks: **318 passed** on local Python **3.14.6**, including earlier
  quote repair/re-audit/terminal tests and all new repair suites. Command: `PYTHONPATH=src
  python -m pytest -q -W error` with `test_comparison_command`, `test_diagnostic_pair`,
  `test_tradier_diagnostic_repairs`, `test_quote_diagnostic`, `test_quote_repairs`,
  `test_quote_reaudit`, `test_quote_terminal`, `test_option_conventions`,
  `test_tradier_option_check`, `test_quote_measure`, `test_tastytrade_quotes`.
- The committed Bash command was executed against synthetic provider CLIs: the shared
  selection precedes both captures; each provider/clock/normalizer failure retains its
  exit; different pair IDs prevent complete collection; wrong branch stops before keys
  or provider commands. No market-data service was contacted by these tests.
- Original audit probes re-run with only source/output paths and implementation label
  changed: all five false F1 PASS cases now fail or give PARTIAL for the missing peer;
  both F2 uncaught exceptions are gone. Standalone nearest selection remains unchanged;
  the shared-pair and orchestration regressions verify the F3 comparison fix.
- Full combined strict suite: `PYTHONPATH=src python -m pytest -q -W error`, local
  Python **3.14.6**: **1,973 passed in 219.69 s (exit 0)**. Only this runtime was
  tested for the repair; earlier Claude multi-version counts do not verify it.
- `bash -n tools/g5_tradier_comparison.sh` and `git diff --check`: clean.

Before/after probes and JSON are saved outside the repository at
`REPAIR_TRADIER_5b43e5d_2026-10-07/` in the Codex workspace, alongside the original audit.
Self-review found two integration errors before completion (invalid unvalidated pair
used during failure reporting, and contradicted options remaining registered); both
were corrected and have regressions. This is not a claim of independent sign-off.

### Next checkpoint

Independent review of this bounded repair; iMac update and strict suite at the accepted
SHA; then the committed regular-session comparison. Share the complete new report
directory, including selection and summary. Tradier REST freshness does not test its
stream/reconnect, and this pipeline does not close immediate RTH30 volume or G5.
Option quantity units and provisional/inferred Greek conventions retain their existing
qualifications. Step 09 stays paused.

### Comparison-command follow-up, 2026-10-07 — requirements before coding

Taz's iMac suite passed 1,973 tests in 305.70 s at `92b7897`. Its subsequent
comparison preserved both providers' shared-pair reports but Webull exited 1.
**Checked in code:** the wrapper supplies a 60-second Webull interval, while the
real diagnostic permits 0–30 seconds and refuses larger intervals before requests.
The synthetic CLI orchestration tests did not exercise that real validator.

Requirement: use a supported 30-second interval in the wrapper and the external
Claude cloud prompt. Preserve the diagnostic limit, request cap, other providers'
collection settings and all trading policies. Producer: comparison shell command;
consumer: `desk.webull_quote_check.main` and its existing validation/normalization.
Acceptance: execute the wrapper's actual Webull arguments through the real CLI
against a synthetic HTTP transport, observing two rounds, six symbol observations
and five requests. The original 60-second command must fail before requests;
provider/clock/normalizer failures must still preserve other reports and exit codes.
Run the focused and combined strict suites; no real provider calls for this repair.
Rollback: revert this bounded follow-up. Preserve the original iMac reports; a new
Webull capture has its own timestamps and cannot be labelled simultaneous with them.
The failure reason in the actual saved Webull report still needs confirmation.
G5 remains open; source freshness, field conventions and host acceptance require
review of actual reports. This change grants no data eligibility or activation.

Verification: the new real-CLI regression failed on the original wrapper (exit 1,
Webull unavailable) before its interval changed. After repair,
`PYTHONPATH=src python -m pytest -q -W error tests/test_comparison_command.py
tests/test_webull_quote_check.py` passed **51 tests in 13.92 s**. The negative
wrapper regression preserves the original unsupported interval, proves
`INVALID_PROBE_ARGUMENTS` with zero requests, and retains both peers' reports.
The full strict suite passed **1,975 tests in 184.35 s**, exit 0, local Python
**3.14.6**. Bash syntax and `git diff --check` are clean. The synthetic real CLI
uses fake HTTP and injected clock/sleep; no live provider or iMac check was run.
Changed files: comparison wrapper, its orchestration regressions, this checkpoint
and the G5 acceptance row. The external Claude cloud prompt also uses 30 seconds.
