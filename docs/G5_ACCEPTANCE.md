# G1–G4 acceptance matrix (G5)

2026-10-02. Prepared by Claude against Taz's full G5 prompt (17:02Z). **G5 acceptance
is PENDING**: the actual-iMac evidence and the independent reviews are not in yet.
Nothing here is Astra's approval (she is unavailable until 2026-10-07); Taz arranged
an outside Claude review and a DeepSeek review of the iMac side in her place.

Evidence types are kept apart. Each row's artifact is tagged:
**[S]** automated synthetic test (labelled deterministic fixtures, no provider),
**[R]** historical replay, **[P]** current provider observation (cloud, Webull
sandbox host), **[H]** actual iMac integration, **[U]** human-facing acceptance.
Test files are under `tests/`; `g5` means `test_g5_integration.py`.

## G1 — earnings failures isolated from price data

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| Earnings config/cache/refresh/read failure leaves price-only checks and an unrelated breakout working | `earnings.scanner_source` → `UnavailableEarningsSource`; `scanner.close_scan`/`run` | [S] `test_earnings_isolation::test_cli_earnings_failure_preserves_price_scan_and_logs_safe_issue`, `::test_failure_masks_underlying_success_but_breakout_trigger_and_review_continue`; [S] `g5::test_combined_repairs_from_broken_earnings_to_single_use_ticket_and_revision` | Pass | [H] not run with a real earnings outage |
| Missing required earnings evidence still blocks EP and cup | `earnings.qualify`; `scanner.revalidate_signal` | [S] `test_earnings_isolation::test_failure_masks_underlying_success_but_breakout_trigger_and_review_continue` (EP, cup `PENDING_EVIDENCE`; cup event ineligible); [S] `g5` test 1 (EP) | Pass | Automatic earnings supply is Step 09 |
| Price and SPY/QQQ failures are not weakened by the wrapper | `scanner.close_scan` market gate | [S] `test_earnings_isolation::test_missing_benchmark_remains_visible_with_earnings_outage`, `::test_genuine_ticker_price_failure_is_not_converted_to_earnings_warning`; [S] `g5::test_bearish_market_is_an_acknowledged_warning_but_missing_spy_qqq_blocks` | Pass | — |
| Safe diagnostics persist, no secrets | `earnings` review log | [S] same G1 tests assert no `SECRET` text in the log | Pass | — |

## G2 — observed stops and independent risk terms

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| Breakout/EP stop is the session low known at the decision; no ADR substitute, no future low | `signal_state` event binding; `scanner` | [S] `test_gap_stop_risk::test_session_low_bound_at_observation_then_frozen_and_invalidated`, `::test_adr_is_width_cap_not_stop_placement`, `::test_future_low_cannot_enter_decision_and_session_low_never_rises`; [S] `g5` test 1 (stop 146.5) | Pass | [H] no live session observed through the scanner |
| Versioned event evidence resolved independently of the caller | `risk_terms.EventRiskSource`; `risk.evaluate` | [S] `test_gap_stop_risk::test_missing_independent_source_cannot_use_caller_loss`, `::test_fabricated_loss_stop_event_and_target_rejected`, `::test_real_scanner_event_to_risk_and_fresh_stop_invalidation` | Pass | — |
| Shares sized from the executable limit, the stop and costs; wrong-side, chased and short cases | `risk.evaluate` sizing | [S] `test_gap_stop_risk::test_actual_limit_drives_share_size_not_old_loss_claim`, `::test_wrong_side_or_chased_limit_cannot_pass_with_false_zero_chase`, `::test_exact_directional_stop_math`, `::test_short_share_execution_remains_unsupported` | Pass | Short execution deliberately unsupported |
| Option exposure disclosed separately; selection under user policy | `risk.evaluate` option paths | [S] `test_gap_stop_risk::test_unavailable_option_stop_estimate_does_not_hide_selected_exposure`, `::test_selected_quantity_over_stop_budget_is_warning_not_cap`, `::test_maximum_loss_budget_is_an_explicit_optional_choice` | Pass | Option valuation for stop estimates not built; shown as unavailable |

## G3 / G3a — vendor history basis, revisions and rebuilds

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| Automatic price path; no manual daily action enrolment | `vendor_basis.VendorBasisSource` | [S] `test_vendor_basis::test_automatic_source_arms_and_triggers_breakout_without_manual_enrollment`; [S] `g5` test 1; [P] `vendor_check` NVDA/SPY/QQQ/AAPL **PASS** at bb75602 (2026-10-02 01:45Z, historical session) and at d529880 (17:41Z, current regular session) | Pass (price/history only) | [H] iMac probe pending |
| Revised history invalidates the original signal; its approval cannot be consumed | `vendor_basis`, `data_basis.PriceHistoryChanged`/`DailyHistoryChanged`, `signal_state.invalidate_candidate` | [S] `test_vendor_basis::test_revision_invalidates_exact_event_and_new_detection_uses_fresh_history`, `::test_split_and_ordinary_or_special_dividend_revisions_cannot_validate_old_signal`; [S] `g5` tests 1–2 | Pass | — |
| Rebuild cannot reuse crossings through the detection time | `revision_rebuild.rebuild_pending`; `signal_state` no-replay boundary | [S] `test_vendor_basis::test_rebuilt_candidate_cannot_replay_gap_since_last_observation`, `::test_revision_before_first_event_retires_candidate_and_prevents_replay`; [S] `test_revision_rebuild::test_revision_rebuilds_in_same_scan_and_only_future_crossing_triggers`; [S] `g5` test 1 | Pass | — |
| Genuine gap with unchanged history is not a revision | `vendor_basis` overlap comparison | [S] `test_vendor_basis::test_genuine_overnight_gap_does_not_trigger_history_revision`; [S] `test_revision_rebuild::test_genuine_gap_does_not_queue_rebuild` | Pass | — |
| Unchanged refresh keeps candidate and event identity | `vendor_basis` content dedup; G4 binding excludes receipt times | [S] `test_vendor_basis::test_unchanged_refresh_preserves_candidate_and_live_event_identity`, `::test_identical_history_is_content_deduplicated_and_survives_restart`; [S] `test_tickets::test_stale_or_revised_evidence_blocks_but_fresh_unchanged_evidence_passes` | Pass | — |
| One malformed ticker does not discard healthy peers | `vendor_basis` per-symbol errors; Webull partial parser | [S] `test_vendor_basis::test_bad_ticker_is_isolated_without_relabeling_unknown_data`, `::test_real_webull_partial_parser_preserves_valid_peers`, `::test_scanner_keeps_healthy_ticker_and_specific_error`; [S] `test_batch_actions::test_bad_attributable_action_does_not_disable_healthy_ticker` | Pass | [H] not observed with a real malformed ticker |
| Revision cannot revive an older volume attestation after restart | `data_basis.volume_basis`; reviewed ledger | [S] `test_vendor_basis::test_history_revision_retires_older_volume_review_across_restart` | Pass | — |
| Volume stays separate; a price PASS is not verified volume | `vendor_check`, `data_basis.volume_basis` | [S] `test_vendor_basis::test_probe_reports_price_pass_and_volume_limit_separately`; [S] `test_batch_actions::test_volume_probe_requires_pair_and_cannot_confuse_price_pass_with_volume_pass`; [P] both cloud reports: `volume_window: UNAVAILABLE_SEPARATE_EVIDENCE_REQUIRED` | Volume **unverified** (as designed) | EP/VCP/cup volume needs the opt-in G3a policy plus a Massive key and host check |
| No corporate-action completeness claim; reviewed files cannot override conflicting daily/minute prices | `vendor_basis` fallback (diagnostic only) | [S] `test_vendor_basis::test_reviewed_source_is_diagnostic_only_for_unresolved_price_pair`; [P] both cloud reports: `action_coverage: NOT_ATTESTED` | Pass | Completeness is not attested by any source |
| Provider failure, recovery, restart and repeats: no stale eligibility, no duplicate consumption | `vendor_basis`, `revision_rebuild`, `tickets.consume` | [S] `test_vendor_basis::test_failed_provider_batch_not_retried_or_exposed`, `::test_identity_change_stays_blocked_after_restart`; [S] `test_revision_rebuild::test_rebuild_outage_waits_and_recovers_without_replaying_gap`; [S] `g5::test_provider_outage_recovery_restart_and_repeats_never_stale_or_double_consume`, `::test_outage_across_a_scan_leaves_no_stale_eligibility_after_recovery_and_restart` | Pass | — |
| G3a: ordinary cash dividend or split explained by batch evidence; rebuild then a new crossing only | `batch_actions.BatchActions`, `revision_rebuild` | [S] `test_revision_rebuild::test_dividend_only_daily_adjustment_reconciles_rebuilds_and_triggers_future_crossing`; [S] `test_batch_actions` (rate limits, pagination, secrets); [S] `g5` test 1 | Pass (opt-in, off by default) | No Massive key; [P]/[H] probe in `G3_FOLLOWUP.md` not run |

## G4 — ticket approval bound to exact terms

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| Budget, quantity, entry and stop edits invalidate approval | `tickets.TicketStore.revise`/binding | [S] `test_tickets::test_every_bound_request_edit_needs_a_new_version_and_approval`, `::test_option_leg_edits_need_a_new_version`, `::test_changed_independent_terms_after_approval_block_consumption` | Pass | — |
| Exposure and stop estimate distinct; no invented dollar cap | `risk.evaluate`; `tickets` display | [S] `test_tickets::test_option_exposure_and_unavailable_stop_estimate_stay_distinct`, `::test_share_position_value_without_noisy_exposure_warning`, `::test_exact_budget_confirmation_for_25000` | Pass | — |
| Warning acknowledgement cannot validate corrupt data; missing SPY/QQQ is not a bearish warning | `risk.evaluate` blocking checks; `tickets.approve` | [S] `test_tickets::test_acknowledgement_cannot_override_a_blocking_check`; [S] `test_risk_warnings::test_unknown_or_stale_market_is_data_failure_not_overridable_warning`; [S] `g5::test_bearish_market_is_an_acknowledged_warning_but_missing_spy_qqq_blocks` | Pass | No production SPY/QQQ market adapter yet (fixture adapter uses the scan's own gate) |
| Exact budget re-entry, exact per-warning codes, single use, revocation, expiry | `tickets.approve`/`consume`/`revoke` | [S] `test_tickets::test_ten_dollar_budget_with_1000_selected_shares_needs_exact_acknowledgement`, `::test_warning_acknowledgements_are_exact_per_version_and_value`, `::test_rejection_revocation_and_expiry`, `::test_concurrent_and_repeated_consumption_yield_one_permission`, `::test_restart_preserves_state_and_history` | Pass | Local SQLite is not a signed ledger; actor names are labels |
| Astra's five defects (manual stop race, double terms read, stale final clock, time stop, display precision) | `tickets`, `risk_state.held_revision` (`d529880`) | [S] `test_g4_fixes` (13 tests; record in `checkpoints/G4-ticket-approval.md`) | Pass; **re-audit pending** | — |
| A valid signal reaches local approval without manual enrolment; no broker action | `tickets` + `EventRiskSource` + vendor path | [S] `test_tickets::test_complete_positive_path_from_persisted_signal_to_single_use`; [S] `g5` test 1 | Pass (`order_submitted: false`) | Live account, quote and contract adapters: Steps 11/13/15/20 |
| Repo and external blueprint wording status recorded | `CLAUDE.md` rule 5 note; `GAP_REPAIR_PLAN.md` | Docs | Repo done; external blueprint **not updated** | Owner: "Independent check of v2.3" thread, if Taz asks |

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
- **[H] Actual iMac integration:** **PENDING.** Taz runs the command block in
  `research/ai-trading/g5/imac-commands.md`; the result is recorded here when pasted back.
- **[U] Human-facing acceptance:** **PENDING.** Taz has seen the G4 CLI demo transcripts
  built from synthetic adapters (`research/ai-trading/g4/`), not a real ticket. A real
  ticket needs the live adapters (later steps). No setup was fabricated and no
  broker fill is claimed.

No scheduled runner, paper orders or live orders are activated by G4/G5, and no
operational trading-day count starts from these development tests.
