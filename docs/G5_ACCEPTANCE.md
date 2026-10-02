# G1–G4 acceptance matrix (G5)

2026-10-02. Prepared by Claude against Taz's full G5 prompt (17:02Z). **G5 acceptance
is PENDING**: the actual-iMac evidence and the independent reviews are not in yet.
Nothing here is Astra's approval (she is unavailable until 2026-10-07); Taz arranged
an outside Claude review and a DeepSeek review of the iMac side in her place.

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
| [Plan G3; G5.B9; G5.A] Automatic price path; no manual daily action enrolment | `vendor_basis.VendorBasisSource` | [S] `test_vendor_basis::test_automatic_source_arms_and_triggers_breakout_without_manual_enrollment`; [S] `g5` test 1; [P] `vendor_check` NVDA/SPY/QQQ/AAPL **PASS** at bb75602 (2026-10-02 01:45Z, historical session) and at d529880 (17:41Z, current regular session) | Pass (price/history only) | [H] iMac probe pending |
| [Plan G3; G5.B5] Revised history invalidates the original signal; its approval cannot be consumed | `vendor_basis`, `data_basis.PriceHistoryChanged`/`DailyHistoryChanged`, `signal_state.invalidate_candidate` | [S] `test_vendor_basis::test_revision_invalidates_exact_event_and_new_detection_uses_fresh_history`, `::test_split_and_ordinary_or_special_dividend_revisions_cannot_validate_old_signal`; [S] `g5` tests 1–2 | Pass | — |
| [Plan G3; G5.B6] Rebuild cannot reuse crossings through the detection time | `revision_rebuild.rebuild_pending`; `signal_state` no-replay boundary | [S] `test_vendor_basis::test_rebuilt_candidate_cannot_replay_gap_since_last_observation`, `::test_revision_before_first_event_retires_candidate_and_prevents_replay`; [S] `test_revision_rebuild::test_revision_rebuilds_in_same_scan_and_only_future_crossing_triggers`; [S] `g5` test 1 | Pass | — |
| [Plan G3; G5.B4] Genuine gap with unchanged history is not a revision | `vendor_basis` overlap comparison | [S] `test_vendor_basis::test_genuine_overnight_gap_does_not_trigger_history_revision`; [S] `test_revision_rebuild::test_genuine_gap_does_not_queue_rebuild` | Pass | — |
| [Plan G3; G5.B7; G4.D3] Unchanged refresh keeps candidate and event identity | `vendor_basis` content dedup; G4 binding excludes receipt times | [S] `test_vendor_basis::test_unchanged_refresh_preserves_candidate_and_live_event_identity`, `::test_identical_history_is_content_deduplicated_and_survives_restart`; [S] `test_tickets::test_stale_or_revised_evidence_blocks_but_fresh_unchanged_evidence_passes` | Pass | — |
| [Plan G3; G5.B3] One malformed ticker does not discard healthy peers | `vendor_basis` per-symbol errors; Webull partial parser | [S] `test_vendor_basis::test_bad_ticker_is_isolated_without_relabeling_unknown_data`, `::test_real_webull_partial_parser_preserves_valid_peers`, `::test_scanner_keeps_healthy_ticker_and_specific_error`; [S] `test_batch_actions::test_bad_attributable_action_does_not_disable_healthy_ticker` | Pass | [H] not observed with a real malformed ticker |
| [G5.B8] Revision cannot revive an older volume attestation after restart | `data_basis.volume_basis`; reviewed ledger | [S] `test_vendor_basis::test_history_revision_retires_older_volume_review_across_restart` | Pass | — |
| [G5.A2; G5.A3] Volume stays separate; a price PASS is not verified volume | `vendor_check`, `data_basis.volume_basis` | [S] `test_vendor_basis::test_probe_reports_price_pass_and_volume_limit_separately`; [S] `test_batch_actions::test_volume_probe_requires_pair_and_cannot_confuse_price_pass_with_volume_pass`; [P] both cloud reports: `volume_window: UNAVAILABLE_SEPARATE_EVIDENCE_REQUIRED` | Volume **unverified** (as designed) | EP/VCP/cup volume needs the opt-in G3a policy plus a Massive key and host check |
| [Plan G3; G5.A2; G5.A4] No corporate-action completeness claim; reviewed files cannot override conflicting daily/minute prices | `vendor_basis` fallback (diagnostic only) | [S] `test_vendor_basis::test_reviewed_source_is_diagnostic_only_for_unresolved_price_pair`; [P] both cloud reports: `action_coverage: NOT_ATTESTED` | Pass | Completeness is not attested by any source |
| [G5.B10; G4.E4–E6] Provider failure, recovery, restart and repeats: no stale eligibility, no duplicate consumption | `vendor_basis`, `revision_rebuild`, `tickets.consume` | [S] `test_vendor_basis::test_failed_provider_batch_not_retried_or_exposed`, `::test_identity_change_stays_blocked_after_restart`; [S] `test_revision_rebuild::test_rebuild_outage_waits_and_recovers_without_replaying_gap`; [S] `g5::test_provider_outage_recovery_restart_and_repeats_never_stale_or_double_consume`, `::test_outage_across_a_scan_leaves_no_stale_eligibility_after_recovery_and_restart` | Pass | — |
| [Plan G3 "rebuild on revisions"] G3a: ordinary cash dividend or split explained by batch evidence; rebuild then a new crossing only | `batch_actions.BatchActions`, `revision_rebuild` | [S] `test_revision_rebuild::test_dividend_only_daily_adjustment_reconciles_rebuilds_and_triggers_future_crossing`; [S] `test_batch_actions` (rate limits, pagination, secrets); [S] `g5` test 1 | Pass (opt-in, off by default) | No Massive key; [P]/[H] probe in `G3_FOLLOWUP.md` not run |

## G4 — ticket approval bound to exact terms

| Requirement | Code path | Test or host artifact | Result | Remaining limitation |
| --- | --- | --- | --- | --- |
| [Plan G4; G4.D2] Budget, quantity, entry and stop edits invalidate approval | `tickets.TicketStore.revise`/binding | [S] `test_tickets::test_every_bound_request_edit_needs_a_new_version_and_approval`, `::test_option_leg_edits_need_a_new_version`, `::test_changed_independent_terms_after_approval_block_consumption` | Pass | — |
| [Plan G4; G4.2; G4.A] Exposure and stop estimate distinct; no invented dollar cap | `risk.evaluate`; `tickets` display | [S] `test_tickets::test_option_exposure_and_unavailable_stop_estimate_stay_distinct`, `::test_share_position_value_without_noisy_exposure_warning`, `::test_exact_budget_confirmation_for_25000` | Pass | — |
| [Plan G4; G4.2; G4.C; G5.B11] Warning acknowledgement cannot validate corrupt data; missing SPY/QQQ is not a bearish warning | `risk.evaluate` blocking checks; `tickets.approve` | [S] `test_tickets::test_acknowledgement_cannot_override_a_blocking_check`; [S] `test_risk_warnings::test_unknown_or_stale_market_is_data_failure_not_overridable_warning`; [S] `g5::test_bearish_market_is_an_acknowledged_warning_but_missing_spy_qqq_blocks` | Pass | No production SPY/QQQ market adapter yet (fixture adapter uses the scan's own gate) |
| [G4.B; G4.C; G4.E] Exact budget re-entry, exact per-warning codes, single use, revocation, expiry | `tickets.approve`/`consume`/`revoke` | [S] `test_tickets::test_ten_dollar_budget_with_1000_selected_shares_needs_exact_acknowledgement`, `::test_warning_acknowledgements_are_exact_per_version_and_value`, `::test_rejection_revocation_and_expiry`, `::test_concurrent_and_repeated_consumption_yield_one_permission`, `::test_restart_preserves_state_and_history` | Pass | Local SQLite is not a signed ledger; actor names are labels |
| [G5.0; Astra A1–A5] Astra's five defects (manual stop race, double terms read, stale final clock, time stop, display precision) | `tickets`, `risk_state.held_revision` (`d529880`) | [S] `test_g4_fixes` (13 tests; record in `checkpoints/G4-ticket-approval.md`) | Pass; **re-audit pending** | — |
| [G5.B9; G4.E7] A valid signal reaches local approval without manual enrolment; no broker action | `tickets` + `EventRiskSource` + vendor path | [S] `test_tickets::test_complete_positive_path_from_persisted_signal_to_single_use`; [S] `g5` test 1 | Pass (`order_submitted: false`) | Live account, quote and contract adapters: Steps 11/13/15/20 |
| [Plan G4; G4.F] Repo and external blueprint wording status recorded | `CLAUDE.md` rule 5 note; `GAP_REPAIR_PLAN.md` | Docs | Repo done; external blueprint **not updated** | Owner: "Independent check of v2.3" thread, if Taz asks |

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
| Manual stop blocks, including one switched on mid-check | User policy; Checked | G4 prompt; Astra's reproduction; `test_g4_fixes` | G4.2, A1 |
| Share default sizing = (budget − reserved costs) ÷ (limit − event stop), floored | User policy | G4 prompt "default share tickets to stop_budget"; arithmetic in `RISK_TERMS.md` | G4.A |
| Breakout/EP stop at the observed day low | Sourced (G2) | Kullamägi cards, per `checkpoints/G2-stop-risk.md` | Plan G2 |
| Prices shown exactly as accepted (no rounding on display) | Checked | Astra A5 reproduction; `test_g4_fixes::test_price_formatting_is_exact` | A5, G4.A |
| A ticket whose time stop has passed cannot be prepared, approved or consumed | Checked; Assumption (logic) | Astra A4 reproduction; the time stop is a bound exit instruction (G4.D). Not presented as trading research | A4, G4.E3 |
| Fresh clock and held account revision in the final transaction | Assumption (engineering) | Locking/timestamp mechanics; Astra A1/A3 reproductions | A1, A3, G4.E2 |
| Approval lifetime 120 s default (1–3600 s configurable) | Assumption (engineering) | Recorded in G4; not trading research | G4.E |
| An approval ends at the signal event's validity (15 min after the trigger bar, extended by each scan that observes it holding) | User policy (prompt) + existing Step 07 rule | G4.E1; window defined in `SIGNAL_LIFECYCLE.md`, unchanged here | G4.E1 |
| Test fixtures place buy limits on whole cents | Sourced | SEC Rule 612 (17 CFR 242.612) minimum increments: $0.01, or $0.005 for tight-spread stocks, for NMS stocks ≥ $1.00 — https://www.law.cornell.edu/cfr/text/17/242.612 | G5.C (labelled fixtures) |
| Fixture market adapter derives "available" from the scan's own SPY/QQQ gate | Assumption (fixture) | No production market adapter exists yet (later steps) | G5.B11 |
| Ordinary cash dividend / split reconciliation (G3a) | Astra's G3a labels (Sourced/Checked/inference) | `checkpoints/G3a-targeted-rebuild.md` | Plan G3 |

### Open questions for Taz (not implemented; the prompts did not ask)

1. **Sub-penny limit prices.** The ticket accepted a $250.0049 limit (Astra A5). SEC
   Rule 612 bars brokers and exchanges from accepting orders in NMS stocks priced at
   or above $1.00 in increments below $0.01 (or $0.005 for designated tight-spread
   stocks): https://www.law.cornell.edu/cfr/text/17/242.612. Such a ticket could never
   become a valid order. Rejecting off-increment limits at preparation is a candidate
   fix (Sourced), but it changes accepted input, so it waits for Taz's decision. The
   current fix only displays the exact value.
2. **Approval lifetime of 120 s** is an Assumption; the journal or Taz's use should
   confirm it.

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
