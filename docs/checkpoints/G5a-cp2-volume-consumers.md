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

## Audit repair F1/F2 — contract written before code (2026-10-04)

Astra's audit of `b5dab2c` (verbatim with Taz's repair prompt:
`/mnt/project-files/research/ai-trading/g5/audits/g5a-checkpoint2-audit-and-repair-prompt-2026-10-04-verbatim.md`)
did not sign off. Her probe script and JSON were not attached to the relayed message, so
the interleavings are rebuilt from the audit text. Baseline: `b5dab2c`, clean tree, no
newer upstream commit (checked before editing).

**F1 — persisted identity health (`alpaca_assets.IdentityStore`).**
- New append-only table `identity_health(sequence, scope, state OK|FAILED, reason,
  version, mapping_digest, asset_list_digest, received_at)` in the same volume/identity
  file. `mappings` stays the version history; health is current eligibility. Nothing is
  deleted.
- Scopes: `symbol:<desk symbol>` for a ticker's resolution outcome; one shared source
  scope `source:alpaca-assets` for the asset-list request.
- Every resolution attempt writes an outcome in the same transaction as any mapping
  write: OK (with the pinned version and digest) or FAILED (`ASSET_NOT_FOUND`,
  `ASSET_AMBIGUOUS`, `ASSET_UNSUPPORTED`, `ASSET_CONFLICT`,
  `ASSET_REUSED_BY_ANOTHER_SYMBOL`, `WEBULL_IDENTITY_MISSING`,
  `WEBULL_ALIAS_ID_MISMATCH`, `CLASS_SHARE_ALIAS_REQUIRED`). A failed asset-list request
  that was actually attempted (HTTP, transport, invalid list, unsafe reply, 401/403/429)
  writes a source FAILED; 401/403/429 also keep the existing cache STOP. Budget
  exhaustion and an already-stopped run send no request and record nothing new (the
  STOP is already persisted). If the identity store itself cannot be written, the run's
  identities are unavailable; a saved signal checked in that run is suspended in the
  signal store, and the cache-only gate's own read fails closed.
- Current eligibility (`IdentityStore.current`, one read): the latest mapping, the latest
  symbol-scope event, and the latest source FAILED. Eligible only when the symbol's
  latest event is OK for exactly the latest mapping version and digest, and no source
  FAILED has a higher sequence than that OK. No symbol event at all (a mapping written by
  `b5dab2c`) is `IDENTITY_HEALTH_NOT_RECORDED`: legacy pins never gain eligibility until a
  fresh successful resolution records OK.
- Recovery order: only a successful resolution against a fetched list recovers, by
  appending OK after the failure. A list whose receipt is not newer than an already
  recorded failure for that symbol or the source (a stale in-memory list) writes no OK
  and returns `IDENTITY_STALE_ASSET_LIST`. FAILED is always appended (fail closed). A
  changed identity keeps the new-version/namespace path and the gate still returns
  CHANGED, so the signal is retired and rebuilt; consumed or revoked tickets and retired
  events are never revived.
- The cache-only gate reads `current` and sends no request. Ticker failures affect that
  ticker; a source failure affects every identity-dependent volume input until each
  ticker is re-resolved; price-only setups are not consulted.

**F2 — volume/identity store held through ticket commit.**
- `AlpacaVolumeProvider.held()` opens the volume/identity SQLite file and takes
  `BEGIN IMMEDIATE` (the single writer reservation, in rollback-journal and WAL modes),
  then forbids every network path of that provider until release (a request or an
  identity refresh inside the guard returns `NETWORK_FORBIDDEN_IN_FINAL_GUARD`).
- `EventRiskSource.held_event` takes the signal-store lock, then `held()`, and its
  `status(at)` runs the cache-only gate while both are held. The ticket's `_final_tx`
  holds both through the ticket COMMIT, so a STOP, FAILED event, revision, mapping
  revision or identity-health write either committed before (and is observed) or
  waits and lands after the ticket commit. Reads made after the reservation see the last
  committed state, which no other connection can change until release.
- Lock order (one order everywhere): ticket store (EXCLUSIVE) → signal store
  (IMMEDIATE) → volume/identity store (IMMEDIATE) → account store (IMMEDIATE, last). No
  other path holds a volume transaction while waiting for a signal or account lock:
  volume/identity writers are short single-file transactions; signal and account writers
  never touch the volume file. All waits happen before the final clock sample.
- The cache-only reuse path becomes read-only: with an active run stop it returns the
  stop code without appending FAILED rows (the STOP is already persisted), so a gate
  inside the guard never needs the writer lock it holds.
- Acquisition failure (busy timeout) raises `sqlite3.Error`; approve/consume already
  refuse "Final check unavailable … nothing was approved/consumed", roll back, and
  release every guard.

**Migration.** Additive table only. Old caches open unchanged; their pins are history and
are ineligible until refreshed. The previous code ignores the new table.

**Regression cases.** Section 10.6/10.7 production-pipeline cases for ASSET_NOT_FOUND,
ASSET_AMBIGUOUS, inactive ASSET_UNSUPPORTED and asset-list 500 (no trigger, no
prepare/approve/consume, final fence rejects; repeated with a fresh provider and zero
requests); ticker failure next to a healthy ticker; asset 401/403/429 keeping STOP;
recovery and changed mapping; stale instance vs newer failure vs later recovery; legacy
migration. F2: disqualifying state committed before the guard on approve and consume
(STOP, per-key FAILED, revised snapshot, mapping change, identity-health failure);
competing writers in another connection and another process during the gap, in
rollback-journal and WAL modes, with a read-only-guard control that must let the writer
through; healthy unchanged refresh, unrelated revision, rollback, lock timeout,
concurrent consumption, manual stop and zero network inside the guard.

## Audit repair F1/F2 — results (2026-10-04)

Implemented as contracted above; no provider call, no Checkpoint 3, no Step 09 work.
Tests: `tests/test_volume_audit_cp2.py` (37 cases, production pipeline: `Desk` fixture,
`scanner.fetch`, `TicketStore.prepare/approve/consume`, `risk_terms` fence).

| Finding (audit line) | Implementation | Regression |
| --- | --- | --- |
| F1 health must persist, ordered, apart from mapping history | `identity_health` table; `IdentityStore._health`, `record_source_failure`, `record_failures` | every F1 test reads the rows back |
| F1 every attempt records an outcome (not found, ambiguous, inactive, unsupported, conflict, missing metadata, failed list, store unavailable) | `resolve` appends FAILED for each pre-transaction failure, conflict and reuse; `identities` records attempted asset-list failures; store errors return `IDENTITY_STORE_UNAVAILABLE` | `..._suspends_a_saved_signal_before_trigger[not_found/ambiguous/inactive/asset_list_500]`, `..._missing_webull_metadata_and_store_failure_are_recorded` |
| F1 cache-only gates inspect health, zero requests; same instance and after restart | `gate` → `IdentityStore.current` | `..._suspends_a_saved_signal_before_trigger[4 kinds]` (same instance and a new provider, no trigger, zero requests); `..._closes_the_cached_final_fence_and_the_ticket[4 kinds]` (after a restart with reopened stores: fence rejects with zero requests, prepare yields a blocked ticket, approve and consume refused, approval unspent) |
| F1 isolation: ticker vs shared source | symbol scope vs `source:alpaca-assets` | `..._a_ticker_failure_leaves_a_healthy_ticker_usable`; `..._asset_list_stop_keeps_the_persisted_stop_and_records_source_health[401/403/429]` |
| F1 recovery only from a later successful list; unchanged pin recovers without new terms; change requalifies | append OK after FAILED; digest-equal pin confirmed; changed digest → version n+1 → CHANGED | `..._recovery_confirms_the_unchanged_pin_without_new_terms_and_a_change_requalifies` |
| F1 stale in-memory list cannot overwrite a newer failure | receipt-time check against newest FAILED per scope → `IDENTITY_STALE_ASSET_LIST`, no write | `..._stale_in_memory_list_cannot_overwrite_a_newer_failure` (stale instance t1, failure t2, stale write refused, later list t3 recovers) |
| F1 legacy pins / migration | additive table; no symbol event → `IDENTITY_HEALTH_NOT_RECORDED` | `..._legacy_pins_without_health_are_ineligible_until_refreshed` (table dropped, reopened, ineligible, then a refresh records OK) |
| F2 guard holds the volume/identity store to the ticket COMMIT, cross-process | `AlpacaVolumeProvider.held` (`BEGIN IMMEDIATE`), `EventRiskSource.held_event` | `test_f2_a_writer_racing_the_gap_serializes_after_the_ticket_commit[{delete,wal} × {stop,key_failed,mapping,identity_health}]` |
| F2 disqualifier committed before the guard is observed and refused (approve and consume) | final `status(at)` reads after the reservation | `test_f2_{approve,consume}_refuses_a_disqualifier_committed_before_the_guard[stop,key_failed,revised,mapping,identity_health]` |
| F2 a WAL read snapshot is insufficient | control replaces `held` with a WAL read transaction | `test_f2_control_a_wal_read_snapshot_guard_lets_the_writer_in` (writer commits inside the gap) |
| F2 no network inside the guard | client `network_blocked`; refresh gate returns `NETWORK_FORBIDDEN_IN_FINAL_GUARD` | `test_f2_no_request_inside_the_guard_and_refresh_is_refused_there` (request count equal on entry and exit; `fetch_assets` raises) |
| F2 failure refuses unspent and releases; concurrency; manual stop; unrelated revision | existing refusal path; `finally` rolls back every guard | `test_f2_unrelated_revision_rollback_lock_timeout_concurrency_and_manual_stop` |

**Interleavings (Checked, fixtures).** In the race test, the writer thread starts inside
the final transaction after `final_problem` has read the fence and before COMMIT:
- after a bounded 0.5 s join it is still waiting;
- a fresh reader still sees `OK` (last committed state);
- a separate Python process running `BEGIN IMMEDIATE` with `timeout=0` prints `locked`;
- once the ticket commits, the writer commits and its own next read of the ticket shows
  `consumed`, so it serialized after the ticket COMMIT;
- the change is then visible (the cached gate is no longer OK).

This holds in rollback-journal and WAL modes for STOP, per-key FAILED, mapping change and
identity-health FAILED. With the reservation swapped for a WAL read snapshot, the STOP
commits inside the gap (the control). Mutation checks (reverted after): making `held()`
a no-op fails 10 F2 tests; making `current()` return the latest pin fails 20 tests;
disabling the stale-list check fails the ordering test.

**Strict suite.** `python -m pytest -q -W error`: 1319 passed on Python 3.12.3 (112.6 s)
and 1319 passed on Python 3.13.14 (114.1 s), Claude cloud container. `git diff --check`
clean.

**Remaining limits.** Fixture-only: Astra's own probe files were not attached, so her
exact injection points are not replayed. No provider was called for the repair. The
acceptance limits listed above (live EP RTH, real-time entitlement, BRK.B live, iMac,
scheduled build, Checkpoint 3, SPY 2026-09-18, Massive and `SSL_CERT_FILE`) are
unchanged. G5 and Step 09 are **not** complete.

## Re-audit repair R1/R2 — contract written before code (2026-10-04)

Astra's re-audit of `0d8838c` (Taz's prompt and her probe saved verbatim:
`/mnt/project-files/research/ai-trading/g5/audits/g5a-cp2-repair-reaudit-R1R2-prompt-2026-10-04-verbatim.md`)
keeps the F1/F2 repairs and finds two uncovered cases. Her separate report file and
probe JSON did not arrive; her probe script did, and at `0d8838c` it reproduces both
(Checked: R1 same-instance and restart gates `OK`, saved VCP triggered; R2 price-only
consume refused with the unrelated writer held, in both journal modes).

**R1 — identity refresh attempts are durable before any request.**
- New table `identity_attempts(attempt INTEGER PRIMARY KEY AUTOINCREMENT, symbols TEXT
  (JSON list of desk symbols the attempt resolves), registered_at TEXT, completed_at
  TEXT, outcome TEXT)`; `identity_health` gains a nullable `attempt` column (the
  attempt whose asset list produced the row; NULL for rows written before this change
  and for direct `resolve` calls without an attempt).
- Order of one refresh: (1) register the attempt in its own short `BEGIN IMMEDIATE`
  transaction and commit; if that fails, **no request is sent** and the run's
  identities are `IDENTITY_STORE_UNAVAILABLE`. (2) Release every lock, then send the
  asset-list request (no lock is held over HTTP). (3) Record the outcome and close the
  attempt in **one** transaction: a failed request appends source FAILED and closes the
  attempt `SOURCE_FAILED:<code>`; a list is resolved and its per-ticker OK/FAILED rows,
  source OK and the attempt's `RESOLVED` closure commit together. A request never sent
  (budget, stop, final guard) closes it `NOT_SENT:<code>` and records no health.
- If step 3 cannot commit (busy, crash, exception), the attempt stays open. An open
  attempt is an unknown shared-source outcome: every identity-dependent input stays
  `IDENTITY_REFRESH_UNRESOLVED` in every process until a ticker's OK row comes from a
  **later** attempt. Price-only paths never read identity health.
- Same instance: any identity persistence failure sets the provider's
  `_unrecorded` code for the rest of that run; the cache-only gate returns it before
  reading the store. Restart relies only on the durable open attempt, not on this flag.
- Eligibility (`current`, one read) adds one rule to F1's: no open attempt numbered
  above the attempt of the ticker's latest OK row (legacy NULL counts as 0).
- Ordering: a list from attempt *k* is stale once any attempt numbered above *k* is
  registered (open or closed); it writes no OK or mapping and closes *k* as `STALE`
  (`IDENTITY_STALE_ASSET_LIST`). F1's receipt-time rule against newer FAILED rows stays.
  So an old in-memory list or a late completion cannot clear a newer open or failed
  attempt, and cannot pin an older identity over a newer one.
- Recovery: a later attempt that resolves the unchanged identity confirms the same
  version (no new terms or candidate; obsolete approvals stay spent or revoked). A
  changed identity keeps the version n+1 / namespace / CHANGED / requalify path.
- 401/403/429 keep the cache STOP; the final guard still sends nothing and registers
  nothing (pre-checks for network block, stop and exhausted budget come before
  registration).
- Migration: additive (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN attempt`
  when absent). Nothing is deleted; mapping and snapshot history are unchanged.

**R2 — the volume guard follows the event's persisted dependencies.**
- Under the signal-store lock, `SignalStore.held_event`'s view exposes the persisted
  candidate row (`candidates.setup_id`, candidate payload, event terms). The volume
  writer reservation is required when a provider is configured and any of: the
  persisted setup is a volume setup (`VOLUME_SETUPS`), the candidate or event terms
  carry a `volume_evidence` key with any non-null value, or the row cannot be read
  consistently (payload setup differs from the column, unparsable payload). Nothing
  from the ticket request decides it.
- Not required: a configured price-only setup whose persisted candidate and terms carry
  no volume evidence. Its `status` still runs `volume_status` (which returns OK without
  touching the provider) and keeps the signal lock, account/manual stop, freshness,
  acknowledgement binding and single-use consumption.
- Required guards keep ticket → signal → volume/identity → account, held through the
  ticket COMMIT, acquired before the final clock; failure refuses unspent. No guard is
  taken lazily after the account lock and no provider call happens under final locks.
- A volume setup saved without evidence, evidence with Alpaca unconfigured, or malformed
  evidence keep their CHANGED/UNAVAILABLE refusals.

**Regression cases.** R1: attempt registration blocked by a real writer lock (zero
requests); 500 and resolution failure with completion blocked by a real lock (same
instance and restart unavailable, signal not triggered); healthy list whose
confirmation cannot commit; crash after registration and after the response; stale
list after a newer open or failed attempt; later unchanged recovery without new terms
and without reviving a consumed ticket; changed mapping requalifies; healthy ticker
isolated; 401/403/429 STOP; zero-network final guard. R2: price-only prepare, approve
and consume with an unrelated writer holding the file (both journal modes); the
volume-dependent equivalent refuses on acquisition failure and still serializes racing
writers after COMMIT; volume setup without evidence, malformed evidence and evidence
with the provider removed still refuse.

## Re-audit repair R1/R2 — results (2026-10-04)

Implemented as contracted above; no provider call, no Checkpoint 3, no Step 09 work.
Tests: `tests/test_volume_audit_r1r2.py` (34 cases; real SQLite write locks from
independent connections, busy waits shortened to 0.05 s by patching only the wait
constants). Astra's probe ran unchanged against the repair and fails at its R1
assertion, as intended: that assertion encoded the defect. A copy with only its
expectations updated, plus two added variants (lock after registration; an
Alpaca-dependent R2 control), passes on Python 3.12.3 and 3.13.14. With the real 5 s
store timeout, her original registration-blocked scenario now sends **0** asset
requests.

| Finding (prompt line) | Implementation | Regression |
| --- | --- | --- |
| R1 inv. 1: this run withholds at once | `_unrecorded` set on any identity persistence failure; cache gate returns it first | `test_r1_a_lock_preventing_registration_sends_nothing_and_withholds_this_run`; `..._outcome_that_cannot_be_committed...` (same-instance gate) |
| R1 test: lock prevents registration → no request | `_fetch_assets` registers before `fetch_assets`; failure returns without sending | same test: request count unchanged, the queued 500 never consumed, no attempt row; a new process reads the earlier confirmation (nothing was observed) |
| R1 inv. 2 / test: 500, resolution failure or healthy list whose outcome cannot commit stays unavailable after restart | outcome + closure in one transaction; open attempt → `IDENTITY_REFRESH_UNRESOLVED` | `test_r1_an_outcome_that_cannot_be_committed_stays_unavailable_after_restart[asset_500, not_found, healthy_list]` (request sent once, attempt open, restart unavailable, signal not triggered, zero requests in cache checks) |
| R1 test: crash between registration, response and completion | open attempt is durable | `test_r1_a_crash_..._leaves_it_unresolved[after_registration, after_response]` (restart gate and final fence refuse, zero requests, approval unspent) |
| R1 inv. 5 / test: stale list after a newer attempt or failure | fetch-numbered staleness; closure `STALE`, no OK/mapping | `test_r1_a_stale_list_cannot_clear_a_newer_open_or_failed_attempt`; `test_r1_a_stale_list_cannot_pin_an_older_identity_over_a_newer_one` |
| R1 inv. 3, 5 / test: later unchanged recovery, no invented terms, no revival | later attempt confirms the same pin (`last_confirmed_at` only) | `test_r1_later_unchanged_recovery_keeps_terms_and_never_revives_a_spent_approval` (history length 1, same terms digest and candidate id, consumes once, then "already consumed") |
| R1 inv. 6: changed mapping requalifies | unchanged path | `test_r1_a_changed_identity_after_an_unresolved_attempt_still_requalifies` |
| R1 inv. 4: scope | per-ticker rows vs shared open attempt; price-only never reads identity | `test_r1_an_open_attempt_spares_recorded_healthy_tickers_scope_and_price_only_setups` |
| R1: 401/403/429 STOP, zero-network final guard | pre-checks before registration | `test_r1_stop_replies_close_the_attempt_and_keep_the_stop[401/403/429]`; `test_r1_the_final_guard_and_a_spent_budget_register_nothing_and_send_nothing` |
| R1 migration | `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN attempt` | `test_r1_migration_adds_the_attempt_column_and_keeps_legacy_rows` |
| R2 req. 1–3: decide from the persisted row under the signal lock | `SignalStore.held_event` view `.persisted()`; `needs_volume_guard` | `test_r2_the_dependency_rule_reads_only_the_stored_row_and_fails_toward_the_guard` (11 rows: price-only with/without null evidence, evidence on candidate or terms, VCP/EP without evidence, setup mismatch, unknown setup, bad JSON, missing payload, missing column) |
| R2: price-only prepare, approve, consume with an unrelated writer holding the file | no reservation for price-only rows | `test_r2_price_only_prepare_approve_and_consume_while_an_unrelated_writer_holds_the_file[delete, wal]` (also manual stop refuses unspent, single use) |
| R2 req. 4: dependent events still refuse on acquisition failure and serialize racing writers | reservation unchanged for them | `test_r2_a_volume_dependent_event_still_refuses_when_the_reservation_is_busy[delete, wal]`; the F2 race tests in `test_volume_audit_cp2` (STOP, per-key FAILED, mapping, identity health × both modes) pass unchanged |
| R2: malformed dependency evidence and source changes | guard taken; `volume_status` decides | `test_r2_a_price_only_row_with_dependency_evidence_or_inconsistency_takes_the_guard[evidence_added, setup_mismatch]`; `test_r2_source_changes_and_legacy_volume_rows_still_refuse` |

**Mutation checks (Checked, reverted after; 71 tests of both audit files):** dropping the
open-attempt rule fails 8; dropping the run flag fails 4; registering after the request
fails 8; dropping the fetch-stale rule fails 2; always taking the guard fails 4; never
taking it fails 23.

**Strict suite.** `python -m pytest -q -W error`: 1353 passed on Python 3.12.3 (125.4 s)
and 1353 passed on Python 3.13.14 (123.0 s), Claude cloud container (1319 + 34).
`git diff --check` clean.

**Remaining limits.** Fixture-only; Astra's separate report and her JSON results did not
arrive. By design, an open attempt (from a crash or a busy store) withholds every
identity-dependent input until the next successful refresh. The provider acceptance
limits above are unchanged. G5 and Step 09 are **not** complete.
