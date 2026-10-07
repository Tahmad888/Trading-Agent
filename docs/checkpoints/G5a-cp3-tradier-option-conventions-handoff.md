# Handoff for Astra: Tradier option conventions (G5a CP3, preflight Step 2) and the focused comparison (Step 3)

2026-10-07, from Claude, for Astra's audit. Taz relays it.

**Original handoff, now audited:** Astra reviewed `5b43e5d` and found F1/F2/F3
plus D1/D2 documentation corrections. This document retains the original work record;
its provider scripts are superseded below. Read `G5a-cp3-tradier-diagnostic-audit-repairs.md`
for the implementation and verification of those repairs before host/live acceptance.

Nothing here activates trading or resumes Step 09.

## Commits and remote

| | |
|---|---|
| Branch | `codex/repair-step-01-baseline` |
| Base | `d539d97dcc1cae6c62568bef5dc650b6c175d227` (local, local branch and remote agreed; tree clean) |
| Final | `5b43e5de92ec183074a28ed0a1b10fa846945cc0` (one commit, fast-forward push; no merge, rebase or force) |
| Remote check | `git ls-remote origin codex/repair-step-01-baseline` returned `5b43e5de92ec183074a28ed0a1b10fa846945cc0` after the push |
| Record (written before code) | `docs/checkpoints/G5a-cp3-tradier-option-conventions.md` |
| Field table | `docs/OPTION_CONVENTIONS.md` |

This is the repository record of Claude's original handoff. Astra filled its base
implementation SHA after checking the dedicated remote branch; later repair commits
are separate. The project-files copy was originally saved at
`/mnt/project-files/research/ai-trading/g5/G5a-cp3-tradier-option-conventions-handoff.md`.

## Changed files

| File | Change |
|---|---|
| `src/desk/option_conventions.py` | **New.** Contains: <ul><li>field mappings with evidence</li><li>a provider- and field-specific time parser</li><li>Greek normalization with the raw values kept</li><li>exposure, size and IV views</li><li>a call/put pair check</li><li>an offline tastytrade Greeks normalizer CLI</li></ul> |
| `src/desk/tradier_option_check.py` | **New.** A read-only Tradier REST diagnostic. <ul><li>Routes: quotes, expirations and chains only.</li><li>At most 12 requests.</li><li>Token from `TRADIER_ACCESS_TOKEN`, or a hidden prompt.</li><li>Refuses to overwrite a report.</li></ul> |
| `src/desk/quote_measure.py` | <ul><li>Adds `TRADIER_ACCESS_TOKEN` to `CREDENTIAL_NAMES`.</li><li>`number_state` now accepts `Decimal`. This is a defect fix, described below.</li></ul> |
| `tests/test_option_conventions.py`, `tests/test_tradier_option_check.py` | **New.** Offline tests. |
| `tests/test_quote_measure.py` | <ul><li>A wire-decoded `Decimal` regression test.</li><li>The isolation-guard list now includes the two new diagnostics.</li></ul> |
| `docs/OPTION_CONVENTIONS.md` | **New.** The field-conventions table. |
| `docs/checkpoints/G5a-cp3-tradier-option-conventions.md` (+ `-handoff.md`) | **New.** The checkpoint record and this handoff. |
| `docs/G5_ACCEPTANCE.md`, `docs/TASTYTRADE_QUOTES.md` | The disposition rows and a numeric-decoding note. |

## Defect found while mapping the pipeline (please audit first)

At `d539d97`, `quote_measure.number_state` accepted only `int`, `float` and `str`. Correction after Astra's audit: `json.loads(raw, parse_float=Decimal)` decodes JSON floating numbers as Decimal; integers and strings survive. Floating Greek/BBO/dayVolume values were affected, not every numeric observation. Retained raw samples or wire frames may support a bounded offline recovery with original times and flags. The old string fixtures missed the defect.

The fix adds `Decimal` to the accepted value types. Booleans are still refused, NaN is still recorded as NAN, and Infinity as INFINITE.

Evidence comes from two read-only tastytrade `quote_check --measure` captures in the cloud. Each ran 15 s with 6 requests, on 2026-10-07 at about 04:17 ET, for SPY/QQQ/NVDA and the SPY 780 pair.

| Capture | State counts across `measurements` |
|---|---|
| Before the fix, code at `d539d97` | INVALID 91, VALUE 7, ZERO 32, NAN 4 |
| After the fix, `d539d97` plus the working-tree fix (the report's `commit` field shows the base) | VALUE 114, ZERO 32, NAN 4, INVALID 0 |

What this affects:
- Inspect each retained measurement before recapturing. Integer/string values survive; at most three raw samples per channel/type, or separately retained wire frames, may support limited offline recovery. INVALID records without values cannot be reconstructed. Saved observations do not establish current Greeks or prove one provider is best.
- The planned opening-window `quote_measure volume` capture would have recorded day volume as INVALID too.
- `webull_quote_check` also calls `number_state`. Its values arrive as strings or floats, so it is unchanged.
- No decision module reads `number_state`; the isolation test checks this.

## Requirement → implementation → test → evidence → limitation

| Req | Implementation | Test(s) | Evidence label | Limitation |
|---|---|---|---|---|
| R1 Tradier `greeks.updated_at` read as UTC, provider- and field-specific; raw, parsed and evidence kept; global parsers unchanged | `option_conventions.provider_time` with the `TIME_FIELDS` key `("tradier","greeks.updated_at")` set to `NAIVE_UTC_TEXT` | `test_tradier_naive_greek_time_is_utc_with_its_evidence_and_et_display`, `test_naive_text_is_not_accepted_for_other_fields_or_providers`, `test_explicit_offsets_are_respected_and_other_formats_refused`, `test_tradiers_published_quote_example_parses_under_the_conventions` | Inferred: a relayed 2024 support answer, consistent with Tradier's 2025 example | Not a current first-party schema (support question 2) |
| R2 Tradier side and trade times in epoch ms; seconds refused | The `EPOCH_MS` contract; values below 1e11 give `TIME_UNIT_UNSUPPORTED`, and 0 gives `TIME_UNAVAILABLE` | `test_epoch_milliseconds_and_refusals`, `test_seconds_magnitude_side_times_are_refused_not_rescaled` | Documented example, plus observed in Taz's run | The 2021 chain example used seconds; the encoding has changed once already |
| R3 Greek age from the Greek time; negative age kept; no threshold | `age_view` gives `FUTURE_AT_CHECK` and `threshold: NONE_DEFINED` | `test_future_greek_time_is_kept_as_an_anomaly_not_clamped`, `test_quote_and_greek_times_are_distinct_fields` | — | The hourly cadence is informational only |
| R4 ET display via `ZoneInfo` | `ET = ZoneInfo("America/New_York")` | Acceptance example 7 (EDT and EST) | — | — |
| R5/R6 Raw kept; normalized per field; exposure = raw × verified multiplier × signed contracts, once | `normalize_greeks`, `exposure`, `position_view`, `EXPOSURE_UNITS` | `test_delta_share_equivalents_follow_contract_sign`, `test_put_delta_is_not_converted_a_second_time`, `test_theta_dollars_per_day_for_the_documented_daily_convention`, `test_zero_contracts_or_bool_contracts_are_refused` | tastytrade: documented (dxFeed). Tradier: see the table | Not wired into tickets or risk |
| R7 No universal 100 | `multiplier_from` returns `VERIFIED_FROM_METADATA`, `MISSING`, `INVALID` or `CONFLICT`. One positive metadata source is accepted when the other is absent; if both are supplied they must agree. Both missing/invalid values or a nonstandard root give no multiplier; raw source provenance is disclosed | `test_non_100_multiplier_follows_metadata`, `test_unknown_or_conflicting_terms_are_never_replaced_by_100`, `test_conflicting_or_missing_contract_size_gives_no_exposure`, `test_nonstandard_root_is_not_given_a_verified_multiplier` | Documented (OIC) | Deliverable terms are not on this route |
| R8 Option sizes stay raw, `UNVERIFIED` / `CONTRACTS_PROVISIONAL`, with no arithmetic | `size_view`; arithmetic is always `EXCLUDED` | `test_option_size_stays_raw_and_provisional` | **Unresolved** | Support question 1 |
| R9 Stock sizes are a sample observation in shares; `lot_size` is never applied | `SIZES[("tradier","rest","stock")]` gives `OBSERVED_SAMPLE` | `test_stock_size_is_a_sample_observation_and_unknown_sources_are_unresolved` | Observed (8 sides) | Sample only |
| R10 Tradier rho and phi raw only; put delta never transformed | `GREEKS` entries `RAW_ONLY`; `pair_check` only reports | `test_tradier_rho_phi_stay_raw_and_theta_vega_gamma_are_provisional`, `test_pair_check_reports_shared_fields_without_transforming_the_put` | Observed, plus documented (ORATS) | — |
| R11 Tradier gamma, theta and vega exposure `PROVISIONAL` | `GreekField.status` is `INFERRED`, with exposure `PROVISIONAL` | Same tests | Inferred (ORATS one-minute definitions) | Support questions 3 and 4 |
| R12 IV is a decimal fraction by mapping, never by magnitude | `IV_FIELDS` and `iv_display` | `test_iv_decimal_displays_as_percent_and_does_not_scale_with_quantity` | Observed | — |
| R13 Field-level refusals; HTTP-200 `fault` or `errors` body is a failure | `value_of`; `TradierClient.get` returns `REST_ERROR` | `test_bad_fields_affect_only_themselves_and_zero_negative_are_values`, `test_http_200_fault_body_is_a_failure_and_stops_requests`, `test_one_bad_option_does_not_discard_its_peer_or_prices`, `test_option_identity_mismatch_isolates_that_symbol`, `test_unmatched_and_missing_symbols_are_failures_not_observations`, `test_a_later_failed_round_leaves_no_current_result` | — | — |
| R14 tastytrade Greeks are observations only | `tastytrade_greek_records`, labelled `RAW_OBSERVATION_NORMALIZED_NOT_CURRENT` | `test_tastytrade_records_are_normalized_as_observations_only`, `test_offline_cli_refuses_to_overwrite` | Documented (dxFeed units) | No IndexedEvent reducer (acceptance example 11 does not apply) |
| Credential safety | `TRADIER_ACCESS_TOKEN` added to `CREDENTIAL_NAMES`; the CLI passes the token into the guard's environment copy | `test_cli_report_guard_refuses_an_echoed_token`, `test_cli_without_token_makes_no_call_and_refuses_overwrite` | — | — |
| Bounded, read-only | `ROUTES` allowlist, the request budget, and the plan pre-check | `test_only_market_data_routes_are_allowed_and_budget_is_enforced`, `test_request_plan_over_budget_makes_no_call` | — | — |
| Isolation | — | `test_conventions_stay_out_of_decision_modules`, plus the updated `test_quote_measure` guard | — | — |

## Conventions (summary; full table in `docs/OPTION_CONVENTIONS.md`)

| Field | Status |
|---|---|
| Tradier option `bidsize`/`asksize` (REST) and `bidsz`/`asksz` (stream) | **Unresolved**: raw, `CONTRACTS_PROVISIONAL`, never used in arithmetic |
| Tradier stock sizes | Observed as shares in one 8-side sample |
| Tradier `bid_date`/`ask_date`/`trade_date` | Epoch ms; seconds refused |
| Tradier `greeks.updated_at` | Naive text read as UTC (inferred from a relayed support answer) |
| Tradier delta | Observed per share, signed |
| Tradier gamma, theta, vega | Inferred from ORATS's upstream definitions; exposure `PROVISIONAL` |
| Tradier rho, phi | Raw only (shared strike values) |
| Tradier IV fields | Decimal fraction (observed) |
| tastytrade DXLink Greeks | Documented units (dxFeed); observations only |
| Alpaca free options | `indicative`; not an OPRA reference |

## Where I disagree with the prompt, or did less than it asked

1. **Tradier stream and controlled reconnect: not built, NOT_RUN.** The new diagnostic is REST-only. Astra's earlier stream preflight lives outside the repository. Stream sizes are mapped as `UNVERIFIED`, but no in-repo code reads the stream.
2. **No DXLink Greek reducer (acceptance example 11).** Without one, tastytrade Greeks cannot be called "current". I labelled them observations rather than build a reducer outside this bounded scope.
3. **No cross-provider merge tool.** The Step 3 comparison is three captures started together (Part 2). They are reviewed by hand against each other's own timestamps. I chose this over writing a fourth tool with untested matching rules.
4. **Gamma is also provisional for Tradier**, not just theta and vega. Tradier documents no unit for any Greek. Only delta is observed (call 0.449 / put −0.551, from Taz's sample as Astra relayed it).
5. **Put theta and vega on Tradier are unresolved.** ORATS publishes one Greek set per strike. Black-Scholes gives equal gamma and vega for a call and put, but not equal theta. So `pair_check` reports identical call/put fields per pair and transforms nothing.
6. **On the six report corrections, I agree with all six.** v1's "per contract" meant "contract-specific", not exposure. That wording was mine and was wrong. The corrected report is `REPORT.md` v2.

## Test commands and results

| | |
|---|---|
| Strict suite | `python -m pytest -q -W error`, on Python 3.13.16 in the cloud: **1918 passed in 297.87 s (exit 0)** |
| Diff check | `git diff --check`: clean |
| Base suite at `d539d97` | 1872 passed (Claude's earlier review) |
| New tests | `test_option_conventions.py` and `test_tradier_option_check.py`, plus 1 in `test_quote_measure.py` |
| Regression test | The `Decimal` test fails on the base and passes after the fix |

## Artifacts (project files, `research/ai-trading/tradier-option-conventions-2026-10-07/`)

| File | sha256 |
|---|---|
| `REPORT.md` (v2) | `54d2e346be4dbf71108a6ce8277c86fb98b8e1bfa102306d3792e876cafc3716` |
| `REPORT-v1-superseded.md` | `70edfea44d39adc9c9bfb43e8be28ae91029e2e3e1f42337b502fbdd16d6e36d` |
| `cloud-check/1-before-fix-d539d97-quote-check-measure.json` | `8a4e98a72b11f0e1aeeff4e3b4dba0bda3af2b2056a966183d1c3dab0a05519d` |
| `cloud-check/1-before-fix-greeks-normalized.json` | `cf38fb68f012831499d6a9510c82698977c470992ff4749c01a0fa4af384d38f` |
| `cloud-check/2-after-fix-quote-check-measure.json` | `4995d04f277a28563f65555fa2cd275aba94a98d45ed5561e9967a9264eac7db` |
| `cloud-check/2-after-fix-greeks-normalized.json` | `6bc83c9ebfaa86811d77384f07b479a4c5f798cc1f8649131d7a113ba2b43101` |
| `raw/probe-log.json` (v1 probe) | `8bdefc2dd8164f759785457c5129378ae85c949f8da9ba2056d6dcf5b0de809b` |
| `probe/option_conventions_probe.py` | `5a5bafd911900d7f74bc5fc3e16c662c066bdee5d14cb45927c706448e951fa0` |

Provider calls made from the cloud this round:
- Two tastytrade `quote_check --measure` captures, each 15 s with 6 requests (one before the fix, one after).
- Nothing to Tradier, since there is no key in the cloud.
- No account, order, position or balance routes.

## Live stage: NOT_RUN

- **Market closed:** it was about 04:00 ET on 2026-10-07 during this work.
- **No credential:** there is no Tradier credential in the cloud environment.

Step 3 needs the committed comparison script below during a regular session, after the repaired commit's host verification.

## The three gaps

| Gap | Implementation | Provider verification | Host acceptance |
|---|---|---|---|
| Option quantity units | Done: raw kept, `UNVERIFIED`/`CONTRACTS_PROVISIONAL`, excluded from arithmetic | **Unresolved.** Tradier's doc says "in hundreds", while OPRA and ORATS use contracts. No Tradier option size has been matched to an OPRA reference | Not run (Part 2 records sizes; matching needs an OPRA-referenced source) |
| Greek scaling | Done: per-field status, raw kept, exposure formula, no default 100 | Delta observed; gamma, theta and vega inferred (ORATS upstream); rho and phi raw; theta day type unresolved | Not run |
| Greek timestamp zone | Done: Tradier-specific UTC parser; global parsers unchanged | Inferred, from a relayed 2024 support answer and the 2025 example | Not run (Part 2 records `updated_at` beside the side times during RTH) |

## iMac comparison: superseded commands

The earlier inline scripts independently selected a strike at each provider and hid
process failures with `wait ... || true`. They are superseded by the bounded repair
record `G5a-cp3-tradier-diagnostic-audit-repairs.md` and committed script:

```bash
bash tools/g5_tradier_comparison.sh FULL_REVIEWED_COMMIT_SHA
```

Run from the iMac repository only after updating to that exact reviewed SHA and passing
its strict suite. Replace `FULL_REVIEWED_COMMIT_SHA` with the full accepted SHA. The
script requires a clean dedicated branch, a regular session with ten minutes left for
this diagnostic, and the existing credentials file. A missing Tradier token is prompted
with hidden input and not saved. No order/account route is used.

It selects one call/put pair, then passes the same report to both captures using
`--option-selection`. It records clock, Tradier, tastytrade, Webull and normalization
exit codes, preserves outputs in a new Desktop directory and reports missing/contradictory
pair evidence. Collection requires review; it is not G5 approval. It sets no clock
tolerance, Greek freshness threshold, size-unit conversion or new trading cutoff.

Send the strict-suite result and the complete report directory (including selection.json,
summary.json, clock.json, provider JSON, normalized Greeks and stdout files). Do not
repeat all provider calls to recover a missing file without checking saved evidence first.

## Rollback

`git revert 5b43e5d` restores the original base implementation, including its Decimal recorder defect for floating values. Later repair rollback is separate; see `G5a-cp3-tradier-diagnostic-audit-repairs.md`.

## Remaining G5 requirements (unchanged in status by this commit)

- **Child 4:** an iMac strict run at the reviewed commit (at the reviewed repair SHA). The iMac clock offset of +104 ms is still unresolved.
- **Child 5:** a regular-session quotes, freshness and recovery comparison (Part 2 covers REST); the Tradier stream and a controlled reconnect; live tastytrade evidence after the recorder fix, first recovering usable saved observations; the opening-window consolidated volume.
- **Child 6:** the actual Friday 16:40 build, and the overnight revision and dividend workflow on the host.
- Wiring the normalized option analytics into tickets and risk, which needs an open decision first.
- A DXLink Greek reducer, if tastytrade Greeks are to be "current".
- G5 and parent CP3 stay open. Step 09 stays paused.

## Questions for Tradier support (prepared, NOT sent)

1. Option `bidsize`/`asksize` and stream `bidsz`/`asksz`: are they contracts, or "hundreds"?
2. Is `greeks.updated_at` in UTC as the current schema?
3. On put rows, are theta, vega, rho and phi put-specific, or the strike's shared values?
4. Is theta per calendar day or per trading day? Are vega and rho per 1 percentage point?
