# G5a checkpoint 2 — volume consumers and partial discovery

2026-10-04. Implementer: Claude (cloud container). Auditor: Astra. Requested by Taz
("Implement G5a Checkpoint 2: volume consumers and partial discovery", 06:31Z, relayed).
Trader-day steps: **watch** (discovery, liquidity) and **analyze/plan** (setup volume
checks, signal and ticket prerequisites). Nothing here is Astra's approval. Checkpoint 3,
Step 09, runner activation and iMac changes are not started.

## Before-code record (written before any code change)

### Baseline

- `codex/repair-step-01-baseline` at `df994721db017728b4dd5e0d8fd5d512babb7281`
  (Astra closed F1–F3 there). `git fetch` shows no newer upstream commit; the working
  tree is clean; no local changes. Checked 2026-10-04.
- Read: `AGENTS.md`, `CLAUDE.md`, `docs/REPAIR_PLAN.md` (G-steps, Step 17 volume
  repairs), `docs/ALPACA_VOLUME.md`, `docs/G5_ACCEPTANCE.md`,
  `docs/checkpoints/G5a-alpaca-volume.md`, the 01:47Z handoff and this prompt.

### Requirement (User policy, Taz 06:31Z)

Separately evidenced Alpaca SIP volume feeds the existing volume calculations; valid
discovery candidates publish when other candidates fail; saved signals and tickets
cannot keep using unavailable or revised volume qualification. Every trading rule,
threshold, weight, limit, sizing and approval policy stays as it is.

### Inventory of volume reads (Checked: `rg -n -i "volume|vwap"` over `src/desk`)

| # | Consumer | What it reads today | Dependency window | Decision? |
| --- | --- | --- | --- | --- |
| 1 | `watchlist.leader_scan` liquidity | `volume_basis(df[-50:])`, mean of Webull `volume` ≥ 1,000,000 | latest 50 completed sessions | yes (discovery gate) |
| 2 | `indicators.daily_features` `rel_volume` | Webull `volume ÷ rolling(50).mean()` (denominator includes the bar itself) | 50 sessions per point | **no decision consumer** (only `screen_check` display, `data_acceptance` diagnostic) |
| 3 | `triggers.minervini_vcp` dry volume | `volume_basis(f[-50:])`; 10-day mean < 0.7 × 50-day mean | last 50 sessions ending at the signal bar | yes |
| 4 | `triggers.oneil_cup_with_handle` light handle | `volume_basis(f[min(r+1, n-50):])`; handle mean < 50-day mean | last 50 sessions (handle ≤ 25 bars per card) | yes |
| 5 | `triggers.episodic_pivot` + `scanner.episodic_pivots` | Webull M15 first two bars vs Webull daily 50 mean, `compatible_volume`, float compare | 50 sessions before entry + 09:30/09:45 RTH bars | yes |
| 6 | `triggers.luk_reclaim` anchored VWAP | `volume_basis(f.loc[anchor:])`, Webull typical price × Webull volume | from the 63-bar low anchor | yes (level selection) |
| 7 | `indicators.session_vwap` | Webull price × volume | session | no consumer |
| 8 | `bar_contract.developing_daily_from_m15` | sum of Webull RTH M15 volumes (labelled developing) | today | RSI(2) only; `rel_volume` masked |
| 9 | `watchlist.movers` | Webull ranking field `relative_volume_10d` ≥ 2 | provider list | candidate source only; EP check decides |
| 10 | `watchlist.universe` | Webull `most_active` VOLUME/TURNOVER lists | provider list | candidate source only |
| 11 | Darvas `breakout_volume` 1.5, cup `breakout_volume` 1.4, VCP "rising volume" | card text/params only | entry bar | **not implemented** in any entry path (Step 10/17); text is not a check |
| 12 | `vendor_check`, `data_acceptance`, `screen_check` | Webull volume diagnostics | — | diagnostics only |
| 13 | `signal_state`, `revision_rebuild`, `risk_terms`, `tickets` | no volume today | — | must carry and re-check volume qualification |

### Contracts to add

- **Identity** (`alpaca_assets.py`): one documented read-only GET
  `https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity` per
  run, sharing the run's request budget, TLS on, redirects refused, error bodies never
  read, no account/order/position route. Persisted, versioned mapping per desk symbol:
  desk symbol, Webull symbol and instrument ID, Alpaca symbol and asset ID (separate
  fields), asset class, exchange, names, status, method (`exact-symbol` or an explicit
  class-share alias such as BRK.B), asof policy (omitted: Alpaca's current entity
  mapping), receipt and digest. Unresolved, ambiguous, conflicting, reused or changed
  identity makes that ticker's volume unavailable with the reason kept. A changed
  identity gets a new mapping version and cache namespace; windows that reach back
  before a detected change are `UNSUPPORTED_HISTORICAL_MAPPING`. A first pin relies on
  Alpaca's entity mapping, corroborated only by the Webull instrument's own price
  history covering the window (Assumption: not independent proof).
- **Decision volume** (`data_basis.AlpacaDecisionVolume`, new policy ids, never the
  Webull `webull-rth30/native-daily50-v1` label): attached to a Webull frame as a
  separate `attrs["decision_volume"]`; the Webull `volume` column and `volume_basis`
  stay untouched. Daily joined by NY session date; M15 by exact interval start.
- **Consumer view** (`data_basis.decision_window`): one source per comparison. With
  Alpaca configured the Alpaca view is the only volume source for rows 1–5 (no Webull,
  IEX or native fallback); without it, the existing Webull path is unchanged.
- **Signal evidence**: `Signal.volume_evidence` holds the used values, sessions or
  intervals, definitions, share basis, both identities and mapping version, request
  keys, snapshot digests, receipt, rule fingerprint, result and a dependency digest.
  Candidate identity hashes only the dependency (not receipts or snapshot digests).
- **Eligibility gate**: before trigger observation (cache only), signal revalidation
  (fresh refetch of the same keys), and the ticket's final fence (cache only, local).
  Unavailable → suspend; changed volume/source/share basis/identity/rule → invalidate
  and queue the existing rebuild.
- **Partial discovery**: `leader_scan_job` classifies each candidate's terminal outcome
  (selected, criterion, source failure, limit) and publishes READY / PARTIAL / EMPTY;
  FAILED (universe or SPY) and INCOMPLETE (failures, no leaders) retain the previous
  list. One atomic bundle (generation, digest, list, status) is the commit point; the
  derived `watchlist.json` and `watchlist-status.json` are repaired from it on read.
  SPY must reach the latest completed session at the real clock.

### Timing consequences (truthful availability; not chosen silently)

- Native-daily SIP includes extended hours; Checkpoint 1's Assumption treats it as
  final at the following ET midnight. At the 16:10 close scan today's native daily is
  not final, so VCP/cup volume is `DAILY_NOT_COMPLETED_AT_RECEIPT`; those names are
  re-prepared at the next session's first slot (existing preparation path), with the
  same price terms. Price-only setups arm at 16:10 as before.
- The Friday 16:40 build's liquidity window ends at the latest session whose native
  daily is final (Thursday at 16:40; Friday on a weekend preview). Disclosed per build.
- Free historical SIP needs a 15-minute-old end, so EP's 09:30/09:45 bars are usable
  from 10:15 ET. The 10:00 mover EP check reports them unavailable; user EP picks retry
  at later slots through the existing pending path. No zero-delay entitlement is assumed.

### Acceptance cases

The prompt's ten regression groups (section 10), mapped to tests in the final section,
plus a deterministic replay of the saved 365-name artifacts and one bounded read-only
preview (at most six Alpaca HTTP calls, scratch cache).

### Rollback

Unset `DESK_ALPACA_VOLUME_CACHE` (default): no provider, every consumer takes the
unchanged Webull path. Code rollback: revert this checkpoint's commit; the new tables
and files (`watchlist-build.json`, identity tables) are additive and ignored by the
previous code, which keeps reading `watchlist.json`.

## Result (after code)

Code commit `14aeb35` (parent `df99472`); this document, the contract and the G5 matrix
follow in the docs commit. Implemented by Claude; **not audited**. Labels: Checked
(observed here), Assumption (unverified), User policy (Taz).

### Consumer matrix

All rows read Alpaca only when a provider is configured (`DESK_ALPACA_VOLUME_CACHE`);
without one, rows 1–5 run the unchanged Webull path. "Gate" is `scanner.volume_status`
(cache before observation, fresh refresh at revalidation, cache at ticket prepare,
approve, consume and final fence).

| # | Consumer | Required bars | Source / definition | Identity | Availability check | Persisted dependency | Regression tests |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Discovery liquidity (`watchlist._liquid`) | latest 50 **final** native-daily sessions (through `complete_through`) | Alpaca SIP split native daily, `alpaca-sip-split:native-daily-v1` | pinned record, version > old window rejected | `decision_window(final_only)`: short, missing, misaligned, unfinished, historical mapping → `source_failure:liquidity` | `liquidity_evidence` per leader in `watchlist-build.json`; digest on the leader row | `test_discovery_liquidity_uses_final_alpaca_sessions_with_an_exact_boundary`, `test_discovery_liquidity_unavailable_is_a_source_failure_not_a_fallback`, `test_healthy_and_failed_candidates_publish_partial_with_every_exclusion` |
| 2 | `rel_volume` feature | 50 sessions per point | same | same | NaN for any point without a full final same-identity window | none (display only, no decision consumer) | `test_attached_volume_changes_only_rel_volume_never_webull_columns`, `test_window_reaching_before_a_changed_identity_is_unsupported_historical_mapping` |
| 3 | VCP dry volume | 50 sessions ending at the signal bar | same | same | `decision_window`; exact `s10×50 < 0.7×s50×10` | `Signal.volume_evidence` (dependency digest in `candidate_id`) | `test_alpaca_volume_drives_vcp_and_cup_while_webull_frames_stay_unchanged`, `test_vcp_dry_volume_boundary_is_exact_and_strict`, `test_full_short_missing_and_unfinished_daily_windows`, `test_holiday_and_early_close_follow_the_exchange_calendar` |
| 4 | Cup handle volume | 50 sessions ending at the signal bar (handle ≤ 25 inside it) | same | same | same; exact handle mean < 50-day mean | same | `test_alpaca_volume_drives_vcp_and_cup_while_webull_frames_stay_unchanged` |
| 5 | EP early volume | 50 native-daily sessions before the entry session + RTH M15 09:30 and 09:45 | `alpaca-sip-split:rth30/native-daily50-v1` via `ep_volume_component` (card threshold 0.5) | same record for both channels | `alpaca_ep_inputs`; 15-minute-old end (10:15 ET), missing intervals, DST, short prior windows | `Signal.volume_evidence` + `Context.ep_volume` | `test_ep_uses_the_approved_card_component_with_an_exact_boundary`, `test_ep_excludes_the_entry_day_and_rejects_missing_intervals`, `test_ep_before_1015_is_unavailable_without_a_request_and_retries`, `test_ep_rth_intervals_follow_daylight_saving`, `test_holiday_shortened_prior_window_for_ep`, `test_mover_ep_unavailable_at_1000_retries_at_1015` |
| 6 | Luk anchored VWAP | from the 63-bar low anchor | **unchanged** Webull price × Webull volume, gated by `volume_basis` | Webull only | raises (setup unavailable) without an accepted Webull volume basis; never Alpaca weights | none | `test_luk_anchored_vwap_never_reads_alpaca_weights` |
| 7 | `session_vwap`, `developing_daily_from_m15` volume | — | Webull | — | no decision consumer | none | unchanged suites |
| 8 | Movers / most-active lists | provider list | Webull ranking fields | — | candidate source only; EP decides | none | unchanged `test_discovery` |
| 9 | Card text with no entry check (VCP rising entry volume, cup 1.4×, Darvas 1.5× breakout) | — | — | — | registered in `NOT_IMPLEMENTED`, never satisfied | none | `test_every_volume_consumer_and_unimplemented_card_condition_is_registered` |
| 10 | Signals, revision rebuild, risk terms, tickets | the signal's evidence | as captured | version compared | gate: UNAVAILABLE suspends, CHANGED invalidates + rebuild | evidence on the signal and event terms | `tests/test_volume_lifecycle.py` (11 tests) |

### Section 10 test map (Checked: all pass)

| § | Tests |
| --- | --- |
| 10.1 | `tests/test_alpaca_identity.py` (12) and `test_window_reaching_before_a_changed_identity_is_unsupported_historical_mapping` |
| 10.2 | `test_alpaca_volume_drives_vcp_and_cup_while_webull_frames_stay_unchanged`, `test_attached_volume_changes_only_rel_volume_never_webull_columns` |
| 10.3 | `test_unchanged_webull_path_without_configuration`, `test_missing_keys_or_bad_config_isolate_volume_and_keep_price_checks` (parametrized), `test_decision_volume_refuses_iex_raw_and_mixed_sources` |
| 10.4 | `test_full_short_missing_and_unfinished_daily_windows`, `test_holiday_and_early_close_follow_the_exchange_calendar`, the five EP tests in row 5 |
| 10.5 | `test_every_volume_consumer_and_unimplemented_card_condition_is_registered`, `test_luk_anchored_vwap_never_reads_alpaca_weights` and the row tests above |
| 10.6 | `test_alpaca_qualified_vcp_reaches_a_single_use_ticket`, `test_failed_refresh_blocks_prepare_approve_consume_and_survives_restart` (500/401/403/429), `test_stop_inside_a_run_blocks_every_later_volume_request`, `test_malformed_ticker_is_isolated_while_healthy_tickers_qualify` |
| 10.7 | `test_unchanged_refresh_and_unrelated_changes_keep_terms`, `test_revised_used_volume_invalidates_queues_rebuild_and_never_revives_the_approval`, `test_changed_identity_requalifies`, `test_source_change_requalifies_and_price_only_signals_are_untouched`, `test_share_basis_and_rule_changes_requalify`, `test_revoked_or_consumed_tickets_stay_dead_after_an_unchanged_refresh` |
| 10.8 | `tests/test_partial_discovery.py`: partial, zero leaders/EMPTY, INCOMPLETE, benchmark/universe FAILED (parametrized), no previous list, crash after/before the commit point, tampered bundle, restart and recovery, legacy generation 0, SPY latest completed session |
| 10.9 | `test_user_picks_and_core_exemptions_across_partial_and_failed_builds`, `test_publication_records_attempt_and_stage_counts` |
| 10.10 | `test_pagination_completes_and_exhausted_budget_stays_incomplete`, `test_cache_reuse_makes_zero_calls_and_a_stop_blocks_later_requests`, `test_receipts_after_the_decision_clock_cannot_qualify_it`, `test_complete_through_is_the_latest_final_native_daily_session`, `test_dependency_terms_exclude_receipts`, `test_cache_gate_runs_before_trigger_observation_without_requests` |

Existing tests changed (behaviour the spec changes): `test_discovery` (a failed ticker
now gives PARTIAL, not a vetoed build; new INCOMPLETE case), `test_scanner` (stale SPY
reports "no valid completed SPY bars"), `test_alpaca_volume` (module users now include
the source, data_basis and scanner).

### Verification (Checked, 2026-10-04, Claude cloud container)

- `python -m pytest -q -W error` with the docs commit's tree: **1282 passed** on Python
  3.12.3 (97.2 s) and **1282 passed** on Python 3.13.14 (99.2 s). Baseline `df99472` had
  1212, so 70 tests were added. No other Python is available here; 3.14 not tested by
  Claude.
- `git diff --check`: clean.
- Deterministic replay of the saved 365-name artifacts and one bounded live preview:
  `/mnt/project-files/research/ai-trading/alpaca-volume-checkpoint2-2026-10-04/REPORT.md`.
  Preview: PARTIAL, 3 of 6 Alpaca calls, 283/283 identities and 50-session windows
  complete, 213 ranked, 45 leaders, 96 disclosed source failures, generation 1.

### Implemented vs fixture-tested vs provider-tested

- **Provider-tested (once, cloud, weekend):** asset-list identity for 283 exact-symbol
  names; native-daily SIP liquidity for the whole current universe through
  `leader_scan_job`; PARTIAL publication and status files.
- **Fixture-tested only:** BRK.B alias, ambiguous/changed/reused identity, EP RTH M15
  path, VCP/cup consumers, every signal/ticket gate, crash/restart, stop codes.
- **Not tested:** live EP at 10:15 ET, real-time entitlement, the scheduled Friday build,
  iMac.

### Decisions for Taz (implemented as described; change on request)

1. The Friday 16:40 build's liquidity ends Thursday, because Friday's native daily
   (extended hours) is not final until midnight (checkpoint 1 Assumption).
2. VCP/cup volume cannot qualify at the 16:10 close scan; those names are re-prepared at
   the next session's first slot with the same price terms.
3. EP volume is available from 10:15 ET (free SIP history needs a 15-minute-old end); the
   10:00 mover check retries at the next slot.
4. Card volume conditions with no entry check (row 9) are listed, not invented.
5. Luk's anchored VWAP stays on the Webull path; with Webull volume unaccepted it is
   unavailable. An Alpaca-native VWAP would be a separate change.
6. A failed universe ranking list makes the build FAILED (shared input), like SPY.

### Migration

None required. First use creates the identity tables in the volume cache. A data
directory with only `watchlist.json` is read as generation 0 until the first bundle.

### Plan B

Unset `DESK_ALPACA_VOLUME_CACHE`: unchanged Webull path. A provider fault isolates only
the affected tickers' volume; price-only setups continue.

### Unresolved acceptance

Astra's audit of this checkpoint; live EP RTH evidence at 10:15 ET on a session day;
the iMac run; Checkpoint 3 (scoped history validation); the Webull/Alpaca SPY
2026-09-18 disagreement (no vendor declared correct); G5's Massive dividend evidence
and `SSL_CERT_FILE` items. G5 and Step 09 are **not** complete.
