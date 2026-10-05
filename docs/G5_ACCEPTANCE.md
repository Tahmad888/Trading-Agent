# G1–G4 acceptance matrix (G5)

2026-10-02. Prepared by Claude against Taz's full G5 prompt (17:02Z). **G5 acceptance
is PENDING.** Three audits of `6d21ddf` came back at 20:18Z (Astra, an outside Claude
session, DeepSeek on the iMac). Their findings are fixed in the audit-closure commit
or listed below as decisions for Taz. Astra's re-audit of that commit (`ee4dc90`,
relayed 21:02Z) found two remaining timing defects (P1a, P1b); they are fixed in
`5e24e02`, together with Taz's answers to the open questions (rows
`Re-audit` and `Decision` below). Astra's check of `5e24e02` (2026-10-03 01:07Z) closed
both timing findings and found one decision-2 gap (P2, a relaxed threshold clearing a
warning), fixed in the policy-snapshot commit. G5 stays pending until Astra checks
that commit, iMac evidence exists for the exact commit and Taz's remaining
investigations close. Nothing here is Astra's approval. Audit rows are prefixed `Audit` with the
finding's source (Astra A–C; Claude #n; DeepSeek).

Evidence types are kept apart. Each row's artifact is tagged:
**[S]** automated synthetic test (labelled deterministic fixtures, no provider),
**[R]** historical replay, **[P]** current provider observation (cloud, Webull
sandbox host), **[H]** actual iMac integration, **[U]** human-facing acceptance.
Test files are under `tests/`; `g5` means `test_g5_integration.py`. The bracket at
the start of each requirement names the prompt line it answers (index below):
`Plan Gn` is that row of the `GAP_REPAIR_PLAN.md` acceptance table; `G4.x` is the G4
prompt (01:51Z); `Astra An` is her audit (07:25Z); `G5.x` is the full G5 prompt (17:02Z).
Choices and their evidence labels are listed after the matrix.

## G1 — earnings failures isolated from price data

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| [Plan G1; G5.B1] Earnings config/cache/refresh/read failure leaves price-only checks and an unrelated breakout working | `earnings.scanner_source` → `UnavailableEarningsSource`; `scanner.close_scan`/`run` | [S] `test_earnings_isolation::test_cli_earnings_failure_preserves_price_scan_and_logs_safe_issue`, `::test_failure_masks_underlying_success_but_breakout_trigger_and_review_continue`; [S] `g5::test_combined_repairs_from_broken_earnings_to_single_use_ticket_and_revision` | Pass | [H] not run with a real earnings outage |
| [Plan G1; G5.B2] Missing required earnings evidence still blocks EP and cup | `earnings.qualify`; `scanner.revalidate_signal` | [S] `test_earnings_isolation::test_failure_masks_underlying_success_but_breakout_trigger_and_review_continue` (EP, cup `PENDING_EVIDENCE`; cup event ineligible); [S] `g5` test 1 (EP) | Pass | Automatic earnings supply is Step 09 |
| [Plan G1; G5.B11] Price and SPY/QQQ failures are not weakened by the wrapper | `scanner.close_scan` market gate | [S] `test_earnings_isolation::test_missing_benchmark_remains_visible_with_earnings_outage`, `::test_genuine_ticker_price_failure_is_not_converted_to_earnings_warning`; [S] `g5::test_bearish_market_is_an_acknowledged_warning_but_missing_spy_qqq_blocks` | Pass | — |
| [Plan G1] Safe diagnostics persist, no secrets | `earnings` review log | [S] same G1 tests assert no `SECRET` text in the log | Pass | — |
| [Plan G1; Audit Claude #12] A returned (not raised) refresh outcome such as `POLICY_EXPIRED` is reported in the scan record | `earnings.RefreshReportingSource`; `scanner.run` discovery | [S] `test_earnings_isolation::test_returned_refresh_outcome_is_reported_without_stopping_prices[4 statuses]`, `::test_ready_or_cached_refresh_adds_no_issue` | Pass | Reporting only; EP/cup were already pending |

## G2 — observed stops and independent risk terms

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| [Plan G2] Breakout/EP stop is the session low known at the decision; no ADR substitute, no future low | `signal_state` event binding; `scanner` | [S] `test_gap_stop_risk::test_session_low_bound_at_observation_then_frozen_and_invalidated`, `::test_adr_is_width_cap_not_stop_placement`, `::test_future_low_cannot_enter_decision_and_session_low_never_rises`; [S] `g5` test 1 (stop 146.5) | Pass | [H] no live session observed through the scanner |
| [Plan G2] Versioned event evidence resolved independently of the caller | `risk_terms.EventRiskSource`; `risk.evaluate` | [S] `test_gap_stop_risk::test_missing_independent_source_cannot_use_caller_loss`, `::test_fabricated_loss_stop_event_and_target_rejected`, `::test_real_scanner_event_to_risk_and_fresh_stop_invalidation` | Pass | — |
| [Plan G2] Shares sized from the executable limit, the stop and costs; wrong-side, chased and short cases | `risk.evaluate` sizing | [S] `test_gap_stop_risk::test_actual_limit_drives_share_size_not_old_loss_claim`, `::test_wrong_side_or_chased_limit_cannot_pass_with_false_zero_chase`, `::test_exact_directional_stop_math`, `::test_short_share_execution_remains_unsupported` | Pass | Short execution deliberately unsupported |
| [Plan G2; G4.2] Option exposure disclosed separately; selection under user policy | `risk.evaluate` option paths | [S] `test_gap_stop_risk::test_unavailable_option_stop_estimate_does_not_hide_selected_exposure`, `::test_selected_quantity_over_stop_budget_is_warning_not_cap`, `::test_maximum_loss_budget_is_an_explicit_optional_choice` | Pass | Option valuation for stop estimates not built; shown as unavailable |

## G3 / G3a — vendor history basis, revisions and rebuilds

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| [Plan G3; G5.B9; G5.A] Automatic price path; no manual daily action enrolment | `vendor_basis.VendorBasisSource` | [S] `test_vendor_basis::test_automatic_source_arms_and_triggers_breakout_without_manual_enrollment`; [S] `g5` test 1; [P] `vendor_check` NVDA/SPY/QQQ/AAPL **PASS** at bb75602 (2026-10-02 01:45Z, historical session) and at d529880 (17:41Z, current regular session); [H] iMac `vendor_check` at `6d21ddf`, 2026-10-02 15:22–15:23 EDT: PASS ×4, current regular session, host `api.sandbox.webull.com` (as reported by DeepSeek's audit) | Pass (price/history only) | Volume and action coverage still unverified on every host |
| [Plan G3; G5.B5] Revised history invalidates the original signal; its approval cannot be consumed | `vendor_basis`, `data_basis.PriceHistoryChanged`/`DailyHistoryChanged`, `signal_state.invalidate_candidate` | [S] `test_vendor_basis::test_revision_invalidates_exact_event_and_new_detection_uses_fresh_history`, `::test_split_and_ordinary_or_special_dividend_revisions_cannot_validate_old_signal`; [S] `g5` tests 1–2 | Pass | — |
| [Plan G3; G5.B6] Rebuild cannot reuse crossings through the detection time | `revision_rebuild.rebuild_pending`; `signal_state` no-replay boundary | [S] `test_vendor_basis::test_rebuilt_candidate_cannot_replay_gap_since_last_observation`, `::test_revision_before_first_event_retires_candidate_and_prevents_replay`; [S] `test_revision_rebuild::test_revision_rebuilds_in_same_scan_and_only_future_crossing_triggers`; [S] `g5` test 1 | Pass | — |
| [Plan G3; G5.B4] Genuine gap with unchanged history is not a revision | `vendor_basis` overlap comparison | [S] `test_vendor_basis::test_gap_day_inside_the_compared_history_is_not_a_revision` (gap day inside the overlap over three sessions, with a REVISED control; Audit Claude #16); [S] `::test_genuine_overnight_gap_does_not_trigger_history_revision`, `test_revision_rebuild::test_genuine_gap_does_not_queue_rebuild` (today's gap only) | Pass | — |
| [Plan G3; G5.B7; G4.D3] Unchanged refresh keeps candidate and event identity | `vendor_basis` content dedup; G4 binding excludes receipt times | [S] `test_vendor_basis::test_unchanged_refresh_preserves_candidate_and_live_event_identity`, `::test_identical_history_is_content_deduplicated_and_survives_restart`; [S] `test_tickets::test_stale_or_revised_evidence_blocks_but_fresh_unchanged_evidence_passes` | Pass | — |
| [Plan G3; G5.B3] One malformed ticker does not discard healthy peers | `vendor_basis` per-symbol errors; Webull partial parser | [S] `test_vendor_basis::test_bad_ticker_is_isolated_without_relabeling_unknown_data`, `::test_real_webull_partial_parser_preserves_valid_peers`, `::test_scanner_keeps_healthy_ticker_and_specific_error`; [S] `test_batch_actions::test_bad_attributable_action_does_not_disable_healthy_ticker`; [S] `test_vendor_basis::test_one_malformed_ticker_does_not_take_down_a_healthy_peer[7 faults × D/M15]` (Audit Claude #13: four variants escaped before) | Pass | [H] not observed with a real malformed ticker |
| [G5.B8] Revision cannot revive an older volume attestation after restart | `data_basis.volume_basis`; reviewed ledger | [S] `test_vendor_basis::test_history_revision_retires_older_volume_review_across_restart` | Pass | — |
| [G5.A2; G5.A3] Volume stays separate; a price PASS is not verified volume | `vendor_check`, `data_basis.volume_basis` | [S] `test_vendor_basis::test_probe_reports_price_pass_and_volume_limit_separately`; [S] `test_batch_actions::test_volume_probe_requires_pair_and_cannot_confuse_price_pass_with_volume_pass`; [P] both cloud reports: `volume_window: UNAVAILABLE_SEPARATE_EVIDENCE_REQUIRED` | Volume **unverified** (as designed) | EP/VCP/cup volume needs the opt-in G3a policy plus a Massive key and host check |
| [Plan G3; G5.A2; G5.A4] No corporate-action completeness claim; reviewed files cannot override conflicting daily/minute prices | `vendor_basis` fallback (diagnostic only) | [S] `test_vendor_basis::test_reviewed_source_is_diagnostic_only_for_unresolved_price_pair`; [P] both cloud reports: `action_coverage: NOT_ATTESTED` | Pass | Completeness is not attested by any source |
| [G5.B10; G4.E4–E6] Provider failure, recovery, restart and repeats: no stale eligibility, no duplicate consumption | `vendor_basis`, `revision_rebuild`, `tickets.consume` | [S] `test_vendor_basis::test_failed_provider_batch_not_retried_or_exposed`, `::test_identity_change_stays_blocked_after_restart`; [S] `test_revision_rebuild::test_rebuild_outage_waits_and_recovers_without_replaying_gap`; [S] `g5::test_provider_outage_recovery_restart_and_repeats_never_stale_or_double_consume`, `::test_outage_across_a_scan_leaves_no_stale_eligibility_after_recovery_and_restart` | Pass | — |
| [Plan G3; Audit Astra lower (F4, F5)] A declined rebuild reports its outcome; a request from an earlier session is closed as `EXPIRED` | `revision_rebuild.rebuild_pending`; `signal_state.close_stale_rebuilds`/`rebuild_request` | [S] `test_revision_rebuild::test_declined_rebuild_is_reported_not_silent`, `::test_rebuild_request_from_an_earlier_session_gets_a_terminal_outcome` | Pass | — |
| [Plan G3 "rebuild on revisions"] G3a: ordinary cash dividend or split explained by batch evidence; rebuild then a new crossing only | `batch_actions.BatchActions`, `revision_rebuild` | [S] `test_revision_rebuild::test_dividend_only_daily_adjustment_reconciles_rebuilds_and_triggers_future_crossing`; [S] `test_batch_actions` (rate limits, pagination, secrets); [S] `g5` test 1 | Pass (opt-in, off by default) | No Massive key; [P]/[H] probe in `G3_FOLLOWUP.md` not run |

## G4 — ticket approval bound to exact terms

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| [Plan G4; G4.D2] Budget, quantity, entry and stop edits invalidate approval | `tickets.TicketStore.revise`/binding | [S] `test_tickets::test_every_bound_request_edit_needs_a_new_version_and_approval`, `::test_option_leg_edits_need_a_new_version`, `::test_changed_independent_terms_after_approval_block_consumption` | Pass | — |
| [Plan G4; G4.2; G4.A] Exposure and stop estimate distinct; no invented dollar cap | `risk.evaluate`; `tickets` display | [S] `test_tickets::test_option_exposure_and_unavailable_stop_estimate_stay_distinct`, `::test_share_position_value_without_noisy_exposure_warning`, `::test_exact_budget_confirmation_for_25000` | Pass | — |
| [Plan G4; G4.2; G4.C; G5.B11] Warning acknowledgement cannot validate corrupt data; missing SPY/QQQ is not a bearish warning | `risk.evaluate` blocking checks; `tickets.approve` | [S] `test_tickets::test_acknowledgement_cannot_override_a_blocking_check`; [S] `test_risk_warnings::test_unknown_or_stale_market_is_data_failure_not_overridable_warning`; [S] `g5::test_bearish_market_is_an_acknowledged_warning_but_missing_spy_qqq_blocks` | Pass | No production SPY/QQQ market adapter yet (fixture adapter uses the scan's own gate) |
| [G4.B; G4.C; G4.E] Exact budget re-entry, exact per-warning codes, single use, revocation, expiry | `tickets.approve`/`consume`/`revoke` | [S] `test_tickets::test_ten_dollar_budget_with_1000_selected_shares_needs_exact_acknowledgement`, `::test_warning_acknowledgements_are_exact_per_version_and_value`, `::test_rejection_revocation_and_expiry`, `::test_concurrent_and_repeated_consumption_yield_one_permission`, `::test_restart_preserves_state_and_history` | Pass | Local SQLite is not a signed ledger; actor names are labels |
| [G5.0; Astra A1–A5] Astra's five defects (manual stop race, double terms read, stale final clock, time stop, display precision) | `tickets` (`d529880`; A1's mechanism replaced in the audit-closure commit) | [S] `test_g4_fixes` (12 test functions, 17 collected cases; record in `checkpoints/G4-ticket-approval.md`) | Pass; Astra's second audit found A1/A3 incomplete (rows below) | — |
| [G4.E1; G4.E2; Audit Astra A, Claude #6] Every age limit (account, quote, market, signal terms and event deadline) is judged at the final clock in approve and consume | `tickets.final_evaluation` under the final locks | [S] `test_audit_closure::test_evidence_expiring_during_consumption_is_refused[4]`, `::test_evidence_expiring_during_approval_is_refused[4]`, `::test_shorter_current_event_deadline_expiring_during_consumption_is_refused`, `::test_boundary_ages_still_pass_and_record_the_final_time` | Pass (all failed on `6d21ddf`) | — |
| [G4.E2; G4.E3; Audit Astra B, Claude #5] A signal invalidated, suspended, withdrawn or revised during the check cannot be approved or consumed | `SignalStore.held_event`, `EventRiskSource.held_event`; `tickets._final_tx` | [S] `test_audit_closure::test_real_signal_change_after_resolve_blocks_consumption[3]`, `::test_real_signal_invalidated_during_approval_is_refused`, `::test_changed_event_generation_at_commit_is_refused`, `::test_signal_source_without_a_fence_cannot_consume`, `::test_held_event_blocks_signal_writers_until_released`, `::test_unchanged_real_signal_passes_the_fence` | Pass | — |
| [G4.2; Audit Astra C, Claude #2, #7, #11, #20] Manual stop: fenced in rollback and WAL modes after reopen; never stuck behind a waiting consumer; never refused by a routine snapshot; a routine snapshot no longer refuses tickets | `RiskStateStore.held_account`, `set_manual_halt`; lock order in `tickets._final_tx` | [S] `test_audit_closure::test_account_guard_blocks_writers_in_both_journal_modes_after_reopen[2]`, `::test_manual_stop_mid_check_blocks_in_both_journal_modes[2]`, `::test_manual_stop_engages_while_a_consumer_waits_for_the_ticket_lock`, `::test_manual_stop_issued_while_the_guard_is_held_waits_and_then_lands`, `::test_routine_unchanged_snapshot_mid_check_does_not_refuse`, `::test_manual_stop_is_never_refused_by_a_newer_snapshot`, `::test_busy_store_reports_that_the_manual_stop_was_not_recorded` | Pass | — |
| [G4 "Record Taz's explicit approval", "Do not simulate Taz's approval…"; Audit Claude #1] CLI approval needs a typed `--actor` and an interactive terminal; no default name | `tickets.main`, `_interactive` | [S] `test_tickets::test_cli_approval_refuses_piped_input_and_needs_a_typed_name`, `::test_terminal_interface_round_trip` | Pass | Still a label, not authentication; out-of-band approval is Step 13 |
| [G4.E5; G4.F migration; Audit Claude #8, #21] A consumed approval cannot be reopened by editing rows; v1 files migrate; the permission names `binding_sha256` | `consumptions` table (insert-only), audit cross-check, schema `desk-tickets-v2` | [S] `test_audit_closure::test_editing_two_rows_cannot_reopen_a_consumed_approval`, `::test_audit_trail_alone_still_marks_an_approval_used`, `::test_v1_database_migrates_consumptions_into_the_insert_only_table`, `::test_consumption_permission_names_the_bound_terms` | Pass | Not a signed ledger: someone with file write access can still forge rows |
| [G4.2; Re-audit Astra P1a] A manual stop that waited behind a routine snapshot still engages, even when that snapshot's as-of time is later than the stop request; request and commit times are audited separately; resume keeps the revision and chronology checks | `RiskStateStore.set_manual_halt` | [S] `test_reaudit_closure::test_manual_stop_waiting_on_a_routine_snapshot_still_engages[2]` (two real stores, a writer paused on its lock; the newer case failed on `ee4dc90` with "Control time predates the account snapshot"), `::test_resume_keeps_the_revision_and_chronology_checks`, `::test_manual_control_timestamps_must_be_timezone_aware` | Pass | — |
| [G4.E1; G4.E2; Re-audit Astra P1b] No reader can delay the ticket commit after the final freshness check; account, quote, market and signal-terms ages and the approval and event deadlines are judged after any reader wait, in rollback and WAL modes | `tickets._final_tx` (`BEGIN EXCLUSIVE` before the signal/account locks and the clock) | [S] `test_reaudit_closure::test_reader_wait_happens_before_the_final_clock_in_rollback_mode[12]` (all 12 failed with `BEGIN IMMEDIATE`), `::test_wal_readers_never_delay_the_final_commit[12]`, `::test_commit_follows_the_recorded_final_time_promptly[4]` | Pass | Readers of the ticket store wait during one final computation (busy timeout 10 s) |
| [Decision 1, Taz 2026-10-02] Invalid price increments refused at prepare, never rounded: shares Rule 612; single-leg options by class; multi-leg options whole cents; unknown option class blocks | `TicketRequest._choices`, `tickets.increment`, `option_increment_problems`; `OptionContract.price_increment` | [S] `test_taz_decisions` (decision 1 tests, 26 cases) | Pass | No live contract adapter supplies the class yet (Steps 11/13); fixture classes are synthetic |
| [Decision 2, Taz 2026-10-02; G4.C as amended] Loss/drawdown warnings re-asked only when new, re-thresholded or worse by 10% of threshold from the acknowledged value; improvements shown | `tickets.binding_change`, `BANDED_WARNINGS`; audit `warning_values`; display | [S] `test_taz_decisions` (decision 2 tests, 12 cases) | Pass | — |
| [Decision 2; Re-audit Astra P2 of 5e24e02] A relaxed threshold that clears a loss/drawdown warning, or any change to the warning policy, re-asks; real improvements still pass; tickets without a policy snapshot are refused | `tickets.warning_policy` bound in the binding; `binding_change` | [S] `test_taz_decisions::test_relaxing_a_threshold_that_clears_the_warning_is_re_asked[6]` (all 6 failed on `5e24e02`), `::test_a_warning_cleared_by_real_improvement_still_consumes[3]`, `::test_policy_change_refuses_even_a_ticket_without_warnings`, `::test_ticket_without_a_policy_snapshot_is_not_assumed_unchanged`, `::test_ticket_shows_the_warning_levels_it_was_checked_against` | Pass | — |
| [Decision 3, Taz 2026-10-02] EP chase measured from the frozen opening-range high with the executable limit, in risk and in scanner revalidation; timeframes and stops unchanged | `risk_terms.chase_reference`; `EventRiskSource.resolve`; `scanner.revalidate_signal` | [S] `test_taz_decisions` (decision 3 tests, 7 cases; the reference and revalidation cases failed on `ee4dc90`) | Pass | Adapter-supplied `already_moved_pct` must use the same reference when a live adapter exists |
| [Standing constraint "Fail closed"; Audit DeepSeek] An unset `WEBULL_HOST` is refused instead of reaching production | `webull.WebullData.from_env`; `action_source._host` | [S] `test_webull::test_unset_host_does_not_fall_back_to_production` | Pass | — |
| [G5.B9; G4.E7] A valid signal reaches local approval without manual enrolment; no broker action | `tickets` + `EventRiskSource` + vendor path | [S] `test_tickets::test_complete_positive_path_from_persisted_signal_to_single_use`; [S] `g5` test 1 | Pass (`order_submitted: false`) | Live account, quote and contract adapters: Steps 11/13/15/20 |
| [Plan G4; G4.F] Repo and external blueprint wording status recorded | `CLAUDE.md` rule 5 note; `GAP_REPAIR_PLAN.md` | Docs | Repo done; external blueprint **not updated** | Owner: "Independent check of v2.3" thread, if Taz asks |

## G5a — Alpaca SIP volume producer (checkpoint 1, 2026-10-04)

Taz's handoff "G5 volume and discovery follow-up" (01:47Z), checkpoint 1 only. Producer
and probe exist; no consumer uses them (scanner integration is checkpoint 2). Details:
`checkpoints/G5a-alpaca-volume.md`, contract `ALPACA_VOLUME.md`. Astra's audit of `2d97ea6`
found F1–F3 (rows marked `Audit Astra`); repaired in `df99472` and **closed by Astra**
(1212 strict on 3.14.6, 70 targeted on 3.12.14, relayed 2026-10-04 06:31Z).

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| [G5a producer] Read-only SIP client: documented headers, one host/route, TLS on, explicit `feed=sip`, no fallback | `alpaca_volume.AlpacaVolumeClient`, `BarRequest` | [S] `test_alpaca_volume` request/header/feed tests; [P] probe 2026-10-04 01:57Z | Pass | Historical only; real-time entitlement unproven |
| [G5a producer] Auth/entitlement/rate limit stop the run; six-request cap; remaining pages are incomplete | `RequestBudget`, `fetch` | [S] 401/403/429, truncation, loop and six-request tests | Pass | — |
| [G5a producer] Typed per-ticker evidence; one bad ticker never erases others | `VolumeObservation`, `BatchResult` | [S] missing-symbol and 11 malformed-row variants | Pass | Identity is symbol-level only |
| [G5a producer] `split` and `raw` never share a basis; volumes retained as returned | `share_basis_id`, `ep_volume_component` | [S] raw/split tests; offline NVDA 2024-06-10 split fixture | Pass | No real Alpaca split-window check run |
| [G5a producer] Cache by feed/adjustment/identity/timeframe/bounds; same content reused, changed content revised and invalidates an earlier result | `VolumeCache`, `revalidate` | [S] reuse, revision, identity-change, clock and failure-store tests | **Failed audit at `2d97ea6`** (F1, F2, F3 below); Pass after the repair | Consumers do not yet check it (checkpoint 2) |
| [Audit Astra F1] An active auth/entitlement/rate-limit stop also blocks cached returns; a recorded stop survives restart until a successful refresh | `AlpacaVolumeClient.fetch`, `VolumeCache.record_stop`/`eligible` | [S] `test_alpaca_volume_audit::test_f1_*` (401, 403, 429; daily and RTH) | Pass (fails on `2d97ea6`) | — |
| [Audit Astra F2] A failed latest refresh (ticker or request level, identity change) stays unavailable across restart; healthy tickers stay usable; a successful refresh, even unchanged, restores it; order by sequence | `VolumeCache` events log, `fetch(reuse=True)` cache-only | [S] `test_alpaca_volume_audit::test_f2_*` | Pass (fails on `2d97ea6`) | A `2d97ea6` cache has no events, so nothing in it is eligible |
| [Audit Astra F3] The 0.5 threshold comes from the EP card at calculation and revalidation; no caller override; altered stored results and card changes require requalification | `approved_rule`, `EPVolumeComponent` validator, `revalidate` | [S] `test_alpaca_volume_audit::test_f3_*` | Pass (fails on `2d97ea6`) | Rule version is the whole EP card fingerprint, so any card edit requalifies |
| [G5a calc] 50 prior exchange sessions, entry excluded, 09:30+09:45 RTH bars, Decimal, exact 0.5 boundary, DST/holidays/short session | `ep_volume_component`, `prior_sessions` | [S] 49 vs 50, gap, entry exclusion, stale, DST, boundary tests; [P] NVDA/SPY/QQQ/AAPL replay for 2026-10-02 (all available; ratios 0.246/0.120/0.130/0.079) | Pass | Volume component only; not setup qualification |

## G5a checkpoint 2 — volume consumers and partial discovery (2026-10-04)

Taz's authorization (06:31Z, relayed). Implemented by Claude at `14aeb35`; **pending
Astra's audit**. Details, consumer matrix and section-10 test map:
`checkpoints/G5a-cp2-volume-consumers.md`. [S] = fixture test, [P] = provider observation.

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| [CP2 §5] Webull↔Alpaca identity from the paper asset list; explicit BRK.B alias; ambiguous, changed, reused or unsupported identities isolated; changed identity versioned; older windows rejected | `alpaca_assets.IdentityStore`, `data_basis.decision_window` | [S] `test_alpaca_identity` (12), historical-mapping test; [P] preview: 283/283 exact-symbol, version 1 | Pass (implementer) | Alias and change paths fixture-only; Alpaca's current mapping is not independent historical proof (Assumption) |
| [CP2 §5–6] Separate decision volume; Webull OHLC/`volume` unchanged; one source per comparison; no IEX/raw/mixed/fallback | `AlpacaDecisionVolume`, `decision_window` | [S] `test_volume_consumers` 10.2–10.3 | Pass (implementer) | — |
| [CP2 §6] Liquidity, VCP, cup, EP and `rel_volume` on Alpaca with exact Decimal boundaries; EP via the approved-card component | `watchlist._liquid`, `triggers`, `scanner.ep_volume_context` | [S] boundary, window, DST, holiday, entry-exclusion tests; [P] preview liquidity for the whole universe | Pass (implementer) | Live EP RTH SIP at 10:15 not observed; Luk VWAP stays Webull (unavailable without accepted Webull volume); three card volume texts have no entry check |
| [CP2 §7] Evidence on signals; cache gate before observation, refresh at revalidation, gate at ticket prepare/approve/consume/final fence; suspend vs invalidate+rebuild; no revival | `scanner.volume_status`/`check_volume`, `signal_state.candidate_id`, `risk_terms.held_event` | [S] `test_volume_lifecycle` (500/401/403/429, malformed, restart, revisions, identity, source, share basis, rule, revoked/consumed) | Pass (implementer) | Fixture-only; no real ticket exists yet |
| [CP2 §7] Truthful timing: native daily final at next ET midnight; EP from 10:15; receipts after the decision clock never qualify it | `complete_through`, `settle`, pending re-prep | [S] timing tests in `test_partial_discovery`, `test_volume_consumers` | Pass (implementer) | Finality is a checkpoint-1 Assumption; decisions 1–3 for Taz in the checkpoint |
| [CP2 §8] READY/PARTIAL/EMPTY publish atomically by generation; FAILED/INCOMPLETE retain; SPY at the latest completed session; picks and core across builds | `scanner._leader_scan_job`, `ScanLog` bundle | [S] `test_partial_discovery` (16); [R] replay of the saved 365 names: INCOMPLETE (no Alpaca data saved); [P] preview: PARTIAL generation 1, 45 leaders of 213 ranked, 96 disclosed source failures | Pass (implementer) | Scheduled Friday build and iMac not run |
| [CP2 §9] Shared budget, pagination, stop, cache reuse | `RequestBudget`, `AlpacaVolumeProvider` | [S] 10.10 tests; [P] preview 3 of 6 calls, 2 pages exhausted | Pass (implementer) | — |
| [CP2 audit F1] Persisted, ordered identity health separate from mapping history; every resolution outcome recorded (asset, metadata, conflict, reuse, attempted asset-list failure, store failure); cache-only gates read it with zero requests; newer failure suspends across restart; recovery only from a later successful list; stale list cannot overwrite; legacy pins ineligible | `alpaca_assets.IdentityStore.current`/`resolve`/`record_source_failure`, `alpaca_source.identities`/`gate` | [S] `test_volume_audit_cp2` F1 (16 cases); mutation checks: reverting `current` to "latest pin" fails 20, removing the stale-list check fails the ordering case | **Failed audit at `b5dab2c`**; Pass (implementer) after the repair, pending Astra's re-audit | Fixture-only; no provider call made for the repair |
| [CP2 audit F2] Final approve/consume guard holds the volume/identity file (`BEGIN IMMEDIATE`) from before the final clock to the ticket COMMIT; lock order ticket → signal → volume/identity → account; no network inside; guard failure refuses unspent | `alpaca_source.AlpacaVolumeProvider.held`, `risk_terms.EventRiskSource.held_event`, client `network_blocked` | [S] `test_volume_audit_cp2` F2 (21 cases: before-guard ×5 on approve and consume, thread+subprocess writer race ×4 in rollback-journal and WAL, WAL read-snapshot control, zero-network, rollback, lock timeout, concurrency, manual stop, unrelated revision); mutation: removing the reservation fails 10 | **Failed audit at `b5dab2c`**; Pass (implementer) after the repair, pending Astra's re-audit | Astra's own probe files were not attached; interleavings rebuilt from her audit text |
| [CP2 re-audit R1] An identity outcome that cannot be persisted never leaves an older pin eligible: attempt committed before any request (no request if it cannot be); outcome and closure in one transaction; open attempt unavailable across restart; this run withholds at once; stale list cannot clear newer open/failed attempts | `IdentityStore.register_attempt`/`resolve`/`current`, `AlpacaVolumeProvider._fetch_assets`/`identities`/`gate` | [S] `test_volume_audit_r1r2` R1 (16 cases, real write locks); Astra's probe with updated expectations (R1 registration and completion variants); mutation checks (open-attempt rule, run flag, pre-registration, stale rule) each fail tests | **Failed re-audit at `0d8838c`**; Pass (implementer), pending Astra | An open attempt withholds every identity-dependent input until a later successful refresh (fail closed by design) |
| [CP2 re-audit R2] The final volume/identity reservation follows the stored event's Alpaca dependency; price-only events never wait on an unrelated volume writer | `alpaca_source.needs_volume_guard`, `SignalStore.held_event` view `persisted`, `EventRiskSource.held_event` | [S] `test_volume_audit_r1r2` R2 (18 cases: price-only prepare/approve/consume with the file locked, both journal modes; VCP still refuses; tampered, mismatched, legacy and source-changed rows); F2 race tests unchanged; mutation checks (always/never guard) fail tests | **Failed re-audit at `0d8838c`**; Pass (implementer), pending Astra | — |

## G5a checkpoint 3 — scoped discovery history (2026-10-04)

Taz's prompt (22:29Z, relayed). Implemented by Claude at `9e43eeb` (parent `ba78ba0`,
which Astra accepted for CP2). Astra's audit of `33256b3` found F1 (a clipped reply became a
healthy EMPTY), F2 (boolean OHLCV read as 1.0, predating CP3) and D1 (excluded duplicates
not stored); repaired in `1dea7d4`. Her re-audit kept F1, F2 and D1 and found that a
reply's row count was still trusted as listing origin; repaired in the commit after
`1dea7d4`, **pending her re-audit**. Details,
dependency inventory and section-9 test map: `checkpoints/G5a-cp3-scoped-history.md`.

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| [CP3] Only the Friday leader build is scoped; every other consumer keeps 1000 strict rows | `scanner._leader_scan_job`, `fetch_scoped`; `DAILY_BARS` unchanged | [S] dependency inventory; `test_unsupported_adapter…`; [P] preview D count 265, M15 40 | Pass (implementer) | — |
| [CP3] Typed scope (consumer, policy, instrument, timeframe, cutoff, required start/end); cannot be shortened | `history_scope.HistoryScope`, `ScopeWindow` | [S] `test_friday_prices_end_friday…`; mutation (validator off) caught | Pass (implementer) | — |
| [CP3] Pre-parse classification; unreadable/ambiguous time, identity, shape, delay, receipt never waived; unsupported adapter uses the strict path | `classify_daily`, `WebullData.bars_scoped` | [S] 10 unwaivable variants, stale receipt, excess rows; mutations caught | Pass (implementer) | Excess-row path fixture-only (Webull honoured `start_time` live) |
| [CP3] Finite discovery features only; scoped frames refused by `daily_features` and `triggers.scan` | `indicators.discovery_features` | [S] full vs scoped equality incl. flat tie; mutations caught | Pass (implementer) | SMA floating-point Assumption (1.9e-14) |
| [CP3] Strict required window: gap ≠ short history; truncated and incomplete named; raw M15 anchor kept | `vendor_basis._daily_scoped`, `discovery_bars` | [S] missing/suspension/head/short/truncated/anchor tests; [P] 9 live `MISSING_REQUIRED_SESSIONS`, 6 anchor | Pass (implementer) | — |
| [CP3] Full-history diagnostics keep row, session, field and reason; defects stored sanitized and bounded | `bars.BarRowError`, `VendorHistoryStore.record_defects` | [S] case 2; [P] 21 live `DAILY_ROW_INVALID … at session <date>` (was `DAILY_PROVIDER_UNAVAILABLE`), 27 named gaps | Pass (implementer) | — |
| [CP3] Discovery evidence never overwrites, clears or revalidates full history; revisions stay visible; `compatible_prices` rule kept | `scoped_current`, `latest(scope)`, `latest_revision`, `webull-discovery-v1` | [S] interleave/restart, revision, cannot-arm tests; [P] live `current` 0 rows | Pass (implementer) | — |
| [CP3] Old-defect names recover; in-window defects still reject | build path | [P] 48 earlier failures: 29 outside-only, 19 inside, 0 mismatches; preview 227 ranked / 48 leaders / 59 bar failures (was 213 / 45 / 82) | Pass (implementer) | Cloud sandbox, Sunday run; not iMac or a scheduled build |
| [CP3 audit F1] A clipped reply cannot become a healthy short history: coverage checked against every accepted full and discovery capture of the same instrument (durable, restart-safe); late start without uncapped full-history evidence is unverified; all-failed build is FAILED, list retained | `VendorHistoryStore.coverage`, `coverage_starts`, `VendorBasisSource._short_history`, `scanner._leader_scan_job` | [S] `test_scoped_history_audit` F1 (7 tests: same process, restart, repeat, stale, recovery, window roll, PARTIAL peer, INCOMPLETE control, unverified first-time, capped vs uncapped); mutation checks A2–A5, A7 | **Failed audit at `33256b3`**; repaired in `1dea7d4`, kept by Astra's re-audit except the origin part (next row) | Uncapped-capture origin superseded by the next row |
| [CP3 coverage origin] Row count never creates origin evidence; a first-time short reply stays unverified; count-derived `coverage_starts` rows kept as audit history and ignored; earlier accepted coverage across scopes and restart still gives `SCOPED_HISTORY_INCOMPLETE`; complete 260-session replies need no proof | `VendorHistoryStore.record`/`coverage` (`established` always None, `ignored_count_markers`), `VendorBasisSource._short_history` | [S] `test_scoped_history_audit` coverage-origin (4 tests: truncated first observation, same-shape young vs cut, persisted marker after upgrade, earlier coverage after restart) and the rewritten short-history test; 5 of 57 fail on `1dea7d4`; mutations B1–B5 caught | **Failed re-audit at `1dea7d4`** (healthy EMPTY from a capped reply); Pass (implementer), pending Astra's re-audit | No supported origin source: young listings are individually unavailable on the scoped path (under-260 criterion only on the unchanged full fallback); no provider call, purchase or enrollment added |
| [CP3 audit F2] Boolean OHLCV refused before conversion in the shared parser | `bars._number`, `bars_from_webull`, `row_defect` | [S] 10 raw-parser cases, numeric/zero-volume control, false-leader control (SLOW became a leader at `33256b3`), outside-scope boolean recorded; mutation A1 | **Failed audit at `33256b3`** (predates CP3); Pass (implementer), pending Astra | — |
| [CP3 audit D1] Excluded duplicates detected and stored with both row references | `history_scope.classify_daily` | [S] old duplicate stored as `EXCLUDED_OUTSIDE_SCOPE` and ticker ranks; required duplicate rejects; mutation A6 | **Failed audit at `33256b3`**; Pass (implementer), pending Astra | — |

## Prompt line index

G4 prompt (Taz, 2026-10-02 01:51Z):
- **G4.2** "Daily-loss, weekly-loss, drawdown and bearish-market conditions remain
  explicitly overridable warnings … The separate manual stop remains blocking. Invalid
  data, unsupported calculations, stale required evidence, instrument mismatches and
  failed funding checks cannot be overridden as ordinary warnings." "Show option
  exposure separately from estimated loss at a stop."
- **G4.A** "Show: … Entry limit, structural stop and target, or an explicit absence of
  a target."
- **G4.B** "require confirmation of the exact budget through an explicit amount-entry step".
- **G4.C** "A warning acknowledgement does not override blocking checks."
- **G4.D2** "Changes to budget, quantity, account, instrument, legs, price terms, stop,
  target, costs, exits or warning terms require a new ticket version and approval."
- **G4.D3** "Fresh evidence with unchanged terms must be revalidated without
  manufacturing new executable terms solely because its receipt timestamp changed."
- **G4.E** lifecycle: "Expiry cannot extend beyond the relevant evidence-validity
  boundaries." (E1) "Recheck signal state, current required evidence, risk conditions
  and manual stop before consumption." (E2) "A newly failed prerequisite prevents
  consumption." (E3) "Two concurrent consumers cannot both obtain a successful first
  consumption." (E4) "Retrying the same consumption cannot create a second
  permission." (E5) "Restarting the process preserves state and history." (E6) "The
  consumption interface in G4 performs no broker action." (E7)
- **G4.F** "Do not claim the external Claude blueprint was updated unless it was
  actually updated."

Astra's audit (relayed 07:25Z): A1 manual stop bypass; A2 terms read twice; A3
consumption after expiry; A4 passed time stop; A5 displayed price precision.

G5 prompt (Taz, 2026-10-02 17:02Z):
- **G5.0** "First resolve Astra's G4 findings. Record each finding, its resolution,
  test evidence and commit."
- **G5.A** "Follow docs/VENDOR_BASIS.md using the current checked-out commit." A1 "If
  you cannot access the iMac, give Taz one copyable command block. Do not substitute a
  cloud result and label it iMac verification." A2 "Keep these outcomes separate:
  Price/history checks. Volume evidence. Corporate-action completeness.
  Current-session versus historical-session checks." A3 "A price PASS does not turn
  unknown volume into verified volume." A4 "Do not re-enable the removed fallback …"
  A5 "Do not retry repeatedly after a provider error or request-limit response."
- **G5.B1–B10** the ten "Verify:" scenarios in their listed order; **G5.B11** "Test the
  approved warning policy without removing existing shared market-data prerequisites.
  Missing SPY/QQQ data is not equivalent to an available bearish-market warning."
- **G5.C** matrix, five evidence types, final strict suites and `git diff --check`;
  "Use labelled deterministic fixtures when the market provides no qualifying signal";
  "If required host evidence is absent, mark G5 acceptance pending."
- **G5.D** commit, push, verify the remote SHA, hand off; Step 09 and later listed as
  dependencies only; nothing activated.

## Choices made in G4/G5 and their evidence labels

Labels as defined in the G4 prompt: **User policy** (explicit decision from Taz),
**Checked** (code, a reproducible test or a named provider observation), **Sourced**
(linked primary source), **Assumption** (engineering choice without external
evidence). Software mechanics are engineering decisions, not trading research.

| Choice | Label | Basis | Prompt line |
| --- | --- | --- | --- |
| Bearish market, loss limits and drawdown are acknowledgeable warnings | User policy | `REPAIR_PLAN.md` D05 (loss and drawdown, 2026-09-30); G4 prompt for the bearish market | G4.2 |
| Missing or stale SPY/QQQ market data blocks and offers no acknowledgement | User policy | G4 prompt (stale required evidence cannot be overridden) | G4.2, G5.B11 |
| Manual stop blocks, including one switched on mid-check | User policy; Checked | G4 prompt; Astra's reproduction; `test_g4_fixes`, `test_audit_closure` | G4.2, A1, Audit Astra C |
| Share default sizing = (budget − reserved costs) ÷ (limit − event stop), floored | User policy | G4 prompt "default share tickets to stop_budget"; arithmetic in `RISK_TERMS.md` | G4.A |
| Breakout/EP stop at the low of the day, no wider than 1× ADR (EP up to 1.5×) | Sourced | Kullamägi: "Stop is always at the lows of the day"; "no more than 1x, or maximum 1.5x the average daily range" — https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/ ; breakouts: https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/ (cited by the outside review; EP page re-read 2026-10-02) | Plan G2 |
| Desk adaptations of that stop: the low as of the completed 15-minute decision bar, frozen at the event, ADR measured against the entry price | Assumption | Engineering choices in G2; not stated on the source pages | Plan G2 |
| EP entry at the completed opening-range high (15- or 60-minute) | Sourced (entry); User policy (either range through day one) | "I enter once the opening range highs break … I will buy the 5-minute highs or 60-minute highs" (EP page above); Taz 2026-09-30 | Plan G2 |
| 3% `max_already_moved_pct` chase limit | Assumption | No source found in the blueprint or the cards; Taz kept it as the desk's assumption (2026-10-02) | Plan G2 |
| EP chase measured from the selected, frozen opening-range high (other setups: the candidate trigger) | User policy | Taz 2026-10-02, decision 3; Kullamägi buys the opening-range high break (EP page above); no open-anchored chase rule found there | Decision 3 |
| Prices shown exactly as accepted (no rounding on display) | Checked | Astra A5 reproduction; `test_g4_fixes::test_price_formatting_is_exact` | A5, G4.A |
| A ticket whose time stop has passed cannot be prepared, approved or consumed | Checked; Assumption (logic) | Astra A4 reproduction; the time stop is a bound exit instruction (G4.D). Not presented as trading research | A4, G4.E3 |
| Final transaction: lock order ticket (`BEGIN EXCLUSIVE`) → signal → account (`BEGIN IMMEDIATE` fences), every wait before the clock, full risk rerun at that clock, final time recorded | Assumption (engineering) | Locking/timestamp mechanics; Astra A–C, P1b and Claude #2/#5/#6 reproductions; SQLite locking: https://www.sqlite.org/lang_transaction.html, https://www.sqlite.org/lockingv3.html | G4.E1, G4.E2 |
| Manual stop switch-on has no precondition (no revision or snapshot-time veto); request and commit times audited; 30 s busy wait, then a loud "NOT recorded" error | Assumption (engineering) | Claude #2/#20; Astra P1a | G4.2 |
| CLI approval requires an interactive terminal and a typed `--actor` (no default) | Assumption (engineering) | Claude #1; not authentication | G4 "Record Taz's explicit approval" |
| Share limits: whole cents at $1.00 and above, $0.0001 below; refused at prepare, never rounded; half-cent tier not assumed | User policy; Sourced | Taz 2026-10-02, decision 1; SEC Rule 612 https://www.law.cornell.edu/cfr/text/17/242.612; compliance delay to Nov 2027 https://www.sec.gov/files/rules/exorders/2026/34-105656.pdf | Decision 1 |
| Option limits: single leg by class schedule ($0.01/$0.05 Penny Program, $0.01 all prices QQQ/SPY/IWM, $0.05/$0.10 otherwise); multi-leg whole cents; unknown class blocks | User policy; Sourced; Assumption (routing) | Taz decision 1; Cboe Rule 5.4(a) (https://www.govinfo.gov/content/pkg/FR-2025-12-22/html/2025-23533.htm); Cboe Rule 5.33(f)(1) (https://www.sec.gov/files/rules/sro/c2/2022/34-95342.pdf). Assumes a multi-leg ticket is later sent as one complex order | Decision 1 |
| Whole account-warning policy bound into each ticket; any change re-asks, including on a ticket with no warning; legacy tickets refused | Assumption (engineering) | Astra P2 (re-audit of 5e24e02); fail closed. Slightly stricter than "its threshold changes": a policy change on a no-warning ticket costs one new version | Decision 2 |
| Account warning band: 10% of threshold, cumulative from the acknowledged value, improvements displayed | User policy | Taz 2026-10-02, decision 2: "my suggested starting policy, not a researched trading rule" | Decision 2, G4.C |
| Approval lifetime 120 s default (1–3600 s configurable) | Assumption (engineering) | Recorded in G4; not trading research | G4.E |
| An approval ends at the signal event's validity | User policy (prompt) | G4.E1 | G4.E1 |
| That validity is 15 minutes after the trigger bar, extended by each scan that observes the event holding | Assumption | Existing Step 07 rule in `SIGNAL_LIFECYCLE.md`; the number has no external source | G4.E1 |
| Test fixtures place buy limits on whole cents | Sourced | SEC Rule 612 (17 CFR 242.612) minimum increments: $0.01, or $0.005 for tight-spread stocks, for NMS stocks ≥ $1.00 — https://www.law.cornell.edu/cfr/text/17/242.612 | G5.C (labelled fixtures) |
| Fixture market adapter derives "available" from the scan's own SPY/QQQ gate | Assumption (fixture) | No production market adapter exists yet (later steps) | G5.B11 |
| Ordinary cash dividend / split reconciliation (G3a) | Astra's G3a labels (Sourced/Checked/inference) | `checkpoints/G3a-targeted-rebuild.md` | Plan G3 |

### Open questions for Taz

Taz answered questions 1, 3, 4, 5 and 7 on 2026-10-02 (21:02Z):

- **1 (sub-penny limits): yes.** Implemented (decision 1 rows above).
- **3 (warning values): yes, with a band.** Implemented (decision 2).
- **4 (EP chase): yes.** Implemented (decision 3).
- **5 (SPY/QQQ ex-dividend days): set up the free Massive plan and verify SPY/QQQ
  dividend reconciliation on the iMac.** Unexplained price mismatches stay blocking
  until that passes. Pending on the iMac; a configured key alone proves nothing.
- **7 (`SSL_CERT_FILE`): investigate first.** Read-only iMac checks are listed in
  `research/ai-trading/g5/imac-commands.md`; nothing is changed until they are seen.

Question 2 is open and 6 still needs the two host runs. The original wording follows.

1. **Sub-penny limit prices.** The ticket accepted a $250.0049 limit (Astra A5). SEC
   Rule 612 bars brokers and exchanges from accepting orders in NMS stocks priced at
   or above $1.00 in increments below $0.01 (or $0.005 for designated tight-spread
   stocks): https://www.law.cornell.edu/cfr/text/17/242.612. Such a ticket could never
   become a valid order. Rejecting off-increment limits at preparation is a candidate
   fix (Sourced), but it changes accepted input, so it waits for Taz's decision. The
   current fix only displays the exact value.
2. **Approval lifetime of 120 s** is an Assumption; the journal or Taz's use should
   confirm it.
3. **Loss/drawdown warning values in the binding (Audit Claude #3).** P&L includes
   unrealized P&L, so with a position open any one-cent change between prepare and
   approve, or approve and consume, refuses the ticket. That follows G4.C ("a changed
   warning value must not authorize the ticket") literally but makes these warnings
   act like blocks. Options: keep as is; or bind the warning code and threshold and
   re-ask only when the warning set changes or the value worsens past a band Taz picks.
   Needs Taz's decision because it changes a prompt line's behaviour.
4. **EP chase reference (Audit Claude #4).** Risk measures the 3% chase from the
   candidate trigger, which for EP is the day's open, while the EP entry is the
   opening-range high. An EP whose opening-range high is more than 3% above the open
   can never pass. Kullamägi's EP page buys the 1-, 5- or 60-minute high with the stop
   at the day's low and no open-anchored chase rule (link above). Candidate fix: for EP,
   measure the chase from the entry level. This changes a setup rule (CLAUDE.md rule 5),
   so it waits for Taz.
5. **SPY/QQQ ex-dividend days (Audit Claude #10).** On an ETF's ex-dividend date the
   adjusted daily close differs from the raw minute close, so the default vendor path
   reports `DAILY_RAW_CLOSE_MISMATCH` for it unless the opt-in G3a dividend evidence
   (Massive key) is configured; a market gate built on that path would block that
   day. About 8 sessions a year (SPY and QQQ quarterly). Inferred, not host-checked.
6. **Volume-only revisions (Audit Claude #14, suspected).** Overnight volume
   finalisation, if Webull does it, would retire every armed price-only signal. Needs
   two host `vendor_check` runs (16:10 and 09:35 next day) compared before any change.
7. **`SSL_CERT_FILE` on the iMac (Audit DeepSeek).** Set in the desk's env file; Taz
   to confirm it is intentional.

## Evidence by type

- **[S] Automated synthetic tests:** the full strict suite (counts in
  `checkpoints/G5-combined-verification.md`). Fixtures are labelled; approvals use
  `fixture:automated-test`, never Taz.
- **[R] Historical replay:** none for G1–G4. The NVDA/AAPL/MSFT iMac replays are Step 09
  earnings evidence and are not counted here.
- **[P] Current provider observations (cloud, Webull sandbox host, read-only):**
  `research/ai-trading/astra-review/g3-vendor-check-bb75602-cloud-2026-10-02T0145Z.json`
  (historical session) and `research/ai-trading/g5/g5-vendor-check-cloud-d529880-2026-10-02T1741Z.json`
  (current regular session, 16 M15 rows). Price/history PASS ×4; volume unavailable;
  actions not attested. These are **not** iMac verification.
- **[P] G5a Alpaca SIP historical replay (cloud, 2026-10-04 01:57Z):**
  `research/ai-trading/alpaca-volume-checkpoint1-2026-10-04/` (two requests, both 200,
  complete; four tickers available). Historical access only; not an iMac check and
  not a current-session or entitlement check.
- **[R]/[P] G5a checkpoint 2 replay and live preview (cloud, 2026-10-04 07:25Z, `14aeb35`):**
  `research/ai-trading/alpaca-volume-checkpoint2-2026-10-04/REPORT.md`. Replay of the
  saved 365-name artifacts (no network) and one bounded discovery build through
  production components (3 Alpaca calls, sandbox Webull, scratch data). Not an iMac
  check, not a scheduled build and not a live EP or entitlement check.
- **[P] G5a checkpoint 3 classification and after-preview (cloud, 2026-10-04, `9e43eeb`):**
  `research/ai-trading/g5a-cp3-scoped-history-2026-10-04/REPORT.md`. 10 Webull requests for
  the 48 earlier daily-history failures, then one bounded discovery build (54 Webull, 3
  Alpaca), scratch data. Not an iMac check, not a scheduled build, not a regular session.
- **[H] Actual iMac integration (for `6d21ddf`), as reported in DeepSeek's audit of
  2026-10-02 (the implementer has not seen the raw transcript):** HEAD `6d21ddf`, tree
  clean, env file mode 0600; strict suite 997 passed on Python 3.14.7 (`.venv`) and on
  3.13 in a throwaway venv; `vendor_check` result.json (15:22–15:23 EDT) PASS for NVDA,
  SPY, QQQ and AAPL, current regular session, `volume_window:
  UNAVAILABLE_SEPARATE_EVIDENCE_REQUIRED`, `action_coverage: NOT_ATTESTED`, host
  `api.sandbox.webull.com`, no secrets; no launchd/cron/scanner process; the `DESK_*`
  opt-ins unset; Webull order tools denied. Other platforms (not the iMac): Astra
  997 passed on 3.12.14 and 3.14.6; outside Claude 997 passed on 3.12.13 and 3.14.6
  (macOS arm64). The audit-closure commit has **not** yet run on the iMac; the command
  block in `research/ai-trading/g5/imac-commands.md` is updated for it.
- **[U] Human-facing acceptance:** **PENDING.** Taz has seen the G4 CLI demo transcripts
  built from synthetic adapters (`research/ai-trading/g4/`), not a real ticket. A real
  ticket needs the live adapters (later steps). No setup was fabricated and no
  broker fill is claimed.

No scheduled runner, paper orders or live orders are activated by G4/G5, and no
operational trading-day count starts from these development tests.

## Current reconciliation — G5a CP3 six child steps (2026-10-04)

This section supersedes earlier "not yet on iMac" statements only for the exact
evidence listed here. Historical audit rows retain their chronology. **G5 and parent
Checkpoint 3 remain pending.** Step 09 remains paused. No trading activation.

| Check | Current evidence and scope | Status |
| --- | --- | --- |
| Child 1 saved tastytrade/SIP first30 arithmetic and helper repair | [R/S] Astra independently recomputed 96 supplied rows; 11 standalone repair checks passed. Values unchanged; Tasty volume 42.8–55.8% below reported SIP totals. Raw FEED_DATA not retained; vendor cause unknown. User-relayed artifacts are outside this repo. | Accepted within offline scope; do not reopen |
| CP3 iMac baseline/scoped preview | [H] Taz supplied b747904, Python 3.14.7, 1410 strict tests (200.15s); `/Users/taz/Desktop/g5a-cp3-imac-4nghj27p`: 379/379 accounted, 227 ranked, 48 leaders, PARTIAL, 81 disclosed source failures; Webull 54/70, Alpaca 3/6; 298 symbols, two native-daily SIP pages exhausted. Astra has not opened those iMac files. | Accepted for b747904 and historical Friday cutoff; new quote commit host check still pending |
| Existing host TLS/CA | [H] Taz's 2026-10-03 report: SSL_CERT_FILE set/existing/matches certifi, certificate and hostname required, Webull sandbox and Massive TLS PASS. | Accepted unaffected baseline evidence; new tastytrade HTTPS/WSS host check pending |
| Massive dividend/split observations | [H] Taz's 2026-10-04 00:28–00:41Z reports in `/Users/taz/Desktop/g5-volume-urzfS2`: SPY/QQQ two dividends each; NFLX 2025-11-17 10:1 event, 50 post-split sessions PASS and crossing window EXPECTED_REFUSAL. One unrelated AXIAp identity issue isolated. SPY ex-date daily/minute volume disagreement was measured, not explained or waived. | Accepted scoped observations; no universal action completeness/volume claim |
| Child 2 quote implementation | [S] Astra's `78da588`: `tastytrade_quotes`, `tastytrade_transport`, `quote_risk`, `quote_check`; provider identity/times, caller-source check, per-symbol failures, health/price final fence, closed-market evidence limit; no Candle volume. Child 3 review found F1–F10 (below). | Implemented by Astra; defects repaired in child 3 |
| Child 3 code review and repairs | [S] Claude's independent review of `78da588` found F1–F10; Claude repaired them in `12705aa`. Astra's re-audit of `12705aa` (1566 strict on Python 3.12.14) withheld sign-off for R1–R4 (mapping writer outside the final fence, classification not checked against current identities, halt forgotten after a Profile fault, earlier-connection prices in the recovery summary); Claude repaired R1–R4 in `88391ab`/`318b877`. Astra's audit of `318b877` (1,613 strict on Python 3.12.14; R1–R4 probes all repaired) withheld sign-off for H1 (a committed Webull identity failure did not reach final approval/consumption) and D1 (the diagnostic summary missed changes after the last data message); Claude repaired both in `0ad7a1d`. Astra's audit of `0ad7a1d` (1,655 strict on Python 3.12.14) accepted H1/D1 and found O1 (an older identity check finishing after a newer failure cleared it); Claude repaired it in the follow-up commit (`checkpoints/G5a-cp3-child3-quote-repairs.md`). | Implemented by Claude; **independent sign-off pending Astra's audit** |
| Child 4 current-commit iMac | Pull reviewed quote commit, install `.[dev,quotes]`, strict suite, private credentials and read-only diagnostic; see quote handoff. | Pending |
| Child 5 actual regular-session quotes/recovery | SPY/QQQ/NVDA and real SPY call/put metadata; source times versus simultaneous independent observations; quote side freshness, updates, missing fields, disconnect/recovery. | Pending; closed-session data cannot close it |
| Child 6 actual Friday 16:40 build | Latest Friday price session; prior Thursday finalized SIP liquidity. Existing deterministic calendar/cutoff tests are reused. | Deterministic tests only; actual scheduled-time read-only build pending |
| Child 6 overnight revisions/dividend integrated workflow | Existing production-component revision-versus-gap, dividend rebuild, restart/provider failure and final approval races reused in strict suite. | Automated evidence retained; required actual-host overnight comparison not supplied |
| Human-facing actual ticket/full inputs | G4 fixtures are not a real ticket. Account/market context, full option ContractBook/open interest/valuation where required and planner dependencies are not fabricated by the quote adapter. | Pending; no real approval/order claim |
| Immediate consolidated intraday volume | Existing historical SIP delivery remains delayed; tastytrade Candle volume excluded. Existing EP guards/50 sessions/0.5 unchanged. | Unresolved; quote support does not resolve it |

### Child 3 dispositions (F1–F10)

Code-closed = production code changed and a regression detects removal of the fix.
Nothing below is provider, iMac, regular-session or operational evidence: zero provider
calls were made. Details and test names: `checkpoints/G5a-cp3-child3-quote-repairs.md`.

| Item | Disposition | Still pending |
| --- | --- | --- |
| F1 lazy FEED_CONFIG deadlock | Code-closed: subscribe on channel open, per-type maps, bounded schema deadline | Live tastytrade ordering (child 5) |
| F2 repeated/changed FEED_CONFIG fatal | Code-closed: identical config harmless, changed order adopted and withholds old values, invalid core map withholds then stops | Live config behaviour (child 5) |
| F3 future source time | Diagnostic-closed: still ineligible (no tolerance adopted), lead and clock-uncertainty explanation reported | iMac `sntp` offset (child 4); any tolerance is an open decision |
| F4 old side-change time | Diagnostic-closed: labelled "exceeds quote policy, not delay evidence"; 60 s policy unchanged | Option quote freshness policy before option tickets (policy) |
| F5 mixed quote ordering | Code-closed: `QUOTE_ORDER_AMBIGUOUS` withholds, watermark recovery | Whether dxFeed composite quotes do this live (unverified) |
| F6 reserved label without provenance | Code-closed: blocking `quote_source_matches`, visible warning, approve/consume refused | — |
| F7 identity not verified before signal mutation | Code-closed: reviewed mapping verified before revalidation and in the final fence | **No operational mapping exists**; each watchlist name needs Taz's review |
| F8 quote session per CLI command | Diagnostic-closed: actionable "quote session changed" refusal; generation binding kept | Long-lived shared quote service (future design, not built) |
| F9 halts | Code-closed for known HALTED via optional Profile on a separate channel; UNDEFINED/missing is unknown | Profile delivery for this account (child 5); final `security_tradable` wiring |
| F10 heartbeat recovery | Code-closed: bounded recovery with fresh token/generation/events; denial/schema/429/expiry stop | Live reconnect (child 5); token renewal for an all-day service |

### Child 3 re-audit items (R1–R4)

| Item | Disposition | Still pending |
| --- | --- | --- |
| R1 mapping writer outside the final fence | Code-closed: shared sidecar flock held from before the final clock to COMMIT; writers (review command) take it exclusively; thread and separate-process races wait or refuse | — |
| R2 classification not checked against current identities | Code-closed: tastytrade `is-etf` in the instrument identity and the mapping digest; pinned Webull sub-category compared; missing/corrupt is unavailable; v1 mappings refused and preserved | Webull reclassification seen at the next vendor fetch (no network in the fence); no operational mapping |
| R3 halt forgotten after a Profile fault | Code-closed: per-symbol halt latch cleared only by ACTIVE; halted prices refused | Profile delivery for this account (child 5); latch is in-process only |
| R4 earlier connection's prices in the recovery summary | Diagnostic-closed: status/coverage/lag from the final generation; history labelled; recovery by symbol and component | Live reconnect (child 5) |

### Child 3 audit items of `318b877` (H1, D1)

| Item | Disposition | Still pending |
| --- | --- | --- |
| H1 committed Webull identity failure ignored at final approval/consumption | Code-closed: append-only identity health in the vendor store (check committed before the metadata request; `VERIFIED`/`FAILED`/`NOT_OBSERVED`); verified state required before revalidation and in the final fence; vendor-store write reservation held to COMMIT (thread and separate-process writers wait); legacy pins `UNVERIFIED` until the next normal refresh | Legacy stores on the iMac re-verify on their next scan (child 4); no operational mapping |
| D1 diagnostic summary from the last data message | Diagnostic-closed: per-attempt terminal view; received versus usable at the end; `recovered` only when usable; `FINAL_ATTEMPT_FAILED_AFTER_OBSERVATIONS` | Live reconnect and real schema/status behaviour (child 5) |
| O1 an older identity check finishing late cleared a newer failure (Astra's audit of `0ad7a1d`) | Code-closed: outcomes ordered by check opening; a failure clears only by a check opened after it; unchecked outcomes never clear one | — |
| iMac handoff guards printed STOP and continued | Handoff-closed: the checks run as scripts with `set -e` and an `exit 1` per guard; the reviewed code is pulled and verified before the credential tool (absent on `b747904`) | Taz's run on the iMac (child 4) |

The referenced iMac/cloud artifacts are not available in this implementation
checkout. That is an access limitation, not a failed provider check or a claim that
Taz supplied no evidence. New adapter live timing has made **zero provider calls**
during implementation; every new automated provider/account/signal fixture is
labelled synthetic. No actual Friday schedule or market-session result is inferred.
