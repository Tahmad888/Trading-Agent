# G4 — local ticket preparation, exact approval and single-use consumption

2026-10-02. Base bb7560238cba1aaafcc1c730f65509649e14b55d (G3); the remote repair
branch matched before edits and had no later commits. Implementer: Claude (Taz's
2026-10-02 instruction: Claude implements, Astra/Codex audits). This record is the
implementer's own; it is not Astra's audit or approval. Serves **approve** (and the
plan -> approve hand-off). No orders, broker calls, scheduler activation, merges or
external messages. Webull remains data-only.

## Before-code record

### Requirement (User policy, Taz 2026-10-02 G4 specification)

Provide a documented local interface that prepares and displays a ticket, records
Taz's explicit approval or rejection, revokes an approval, shows state and audit
history, and validates then atomically consumes an approval through a local
non-ordering test interface. Shares default to `stop_budget`; `selected_quantity`
needs an explicit choice. Final approval requires a separate exact budget entry and
exact acknowledgement of every displayed warning for that ticket version. Approval
binds an immutable snapshot of the executable terms and evidence versions; any
term change needs a new version and a new approval. Preserved policies: no fixed
$100 cap, grades assign no budget, HALF does not halve the budget, account/market
conditions stay overridable warnings, the manual stop stays blocking, data/
arithmetic/identity/funding failures cannot be overridden, option exposure is shown
separately from a stop-loss estimate, and an unknown option-dollar stop is
unavailable, never zero. No setup threshold or EP timing change.

### Current behavior (Checked, code at bb75602)

- `risk.evaluate` returns `RiskDecision.approved` = local eligibility, with
  `order_authorized` fixed to false. `terms_sha256` hashes the whole proposal,
  including receipt times such as `quote_as_of`, so it changes on every fresh quote;
  it is not usable as an approval binding and nothing consumes it.
- `exposure_above_budget` is emitted whenever full strategy exposure plus costs
  exceeds the budget. For a long share position the full exposure is the position
  value, so every share ticket sized to a stop budget carries this warning
  (reproduced by the second-chair review on fcf7c72 and re-checked here).
- `stop_estimate_above_budget` is emitted for selected quantity when the conditional
  stop loss plus costs exceeds the budget (the $10 / 1,000 shares / $1,251 case).
- `sizing_mode` is a required proposal field with no default and no producer:
  nothing in `src/` builds a `TradeProposal`; only tests do.
- `RiskStateStore.review/acknowledge` persist a warning review and a codes-only
  acknowledgement (`set(codes) == expected codes`). There is no budget confirmation,
  no value binding, no approval state, revocation, expiry beyond the review deadline
  or consumption. Its docstring states it is not an execution approval.
- `contracts.ApprovalRecord` (decision approve/reject/reduce, `order_args_sha256`,
  `token_expires_at`) has no producer or consumer anywhere in `src/` or `tests/`.
- `EventRiskSource` resolves stop/target from the persisted event after scanner
  revalidation; G3 keeps candidate identity stable across identical history refreshes.
- There are no production producers of `MarketContext`, `ContractBook` or account
  snapshots for `RiskStateStore`; tests construct them. Live adapter wiring is
  therefore an outstanding dependency, not something G4 can claim.

### Affected producers, consumers, stored records and output

- `risk.py`: share tickets stop emitting `exposure_above_budget`; options keep it.
  New additive decision fields: `position_value_usd` (shares) and
  `signal_event_valid_until` (the event's own validity, not receipt freshness).
  `maximum_loss_budget` arithmetic and buying-power/margin checks are unchanged.
- `contracts.py`: `ApprovalRecord` is replaced by a schema-2 immutable approval
  snapshot model; the old shape fails validation (no producer existed).
- New `desk/tickets.py`: ticket request model, proposal builder, plain-language
  display, SQLite `TicketStore` with versions, approvals, consumptions and an
  append-only audit table, plus the `python -m desk.tickets` terminal interface.
- Consumers: the future Step 13 planner/ticket UI and Step 15 paper adapter consume
  a successful local consumption result. G4's consumption performs no broker action.
- Stored records: new `tickets.sqlite` (default `data/tickets.sqlite`,
  `DESK_TICKET_DB`). Existing `risk-state` and `signals.sqlite` schemas are unchanged.
- User-facing output: the ticket text and CLI messages.

### Design (Assumption: engineering choices, not trading research)

- Ticket version = stored request + resolved event terms + independent risk decision
  + binding snapshot. `binding_sha256` hashes executable terms and evidence versions
  but excludes receipt timestamps (quote, account, market and check times), so fresh
  evidence with unchanged terms revalidates to the same binding (G3 stable identity).
- Bound fields: ticket/version, account, environment (paper/live), event id and
  digest, setup id/version, instrument, structure, every leg's symbol, broker
  contract id (options), side, requested and final quantity and limit price, stop,
  target (or none), exit rules and time stop, budget, sizing mode, costs, stop-loss
  estimate and basis, exposure, premium, position value, funding estimate, every
  warning's code/value/threshold.
- Approval issuance reloads the stored ticket, recomputes its binding digest,
  rebuilds the proposal from stored terms plus a fresh trusted market observation,
  reruns `risk.evaluate` with the current stored account snapshot, and requires the
  rerun binding to equal the stored one. Client-supplied flags, hashes or risk results
  are not accepted by the interface.
- Budget confirmation: the approval step requires a separately typed amount parsed
  as exact cents; it must equal the ticket budget. No threshold, no cap.
- Warning acknowledgement token: `<code>:<10 hex>` where the hex is a SHA-256 over
  the ticket version's binding digest and that warning's code/value/threshold. The
  approver must supply exactly the set of tokens displayed. Copying from another
  ticket or a changed value gives a different token.
- States: pending, blocked (not locally eligible; cannot be approved), approved,
  rejected, revoked, expired, superseded (a newer version exists), consumed.
- Approval lifetime: 120 s by default (`DESK_APPROVAL_LIFETIME_SECONDS`, 1–3600).
  Rationale: consumption reruns every check anyway, so the lifetime limits stale
  human intent rather than data freshness; 120 s matches the earlier
  `ApprovalRecord` design note. Expiry is the earliest of the lifetime, the event's
  own validity and contract-metadata validity. Quote/account/market receipt
  freshness is rechecked at consumption instead of shortening expiry.
- Consumption: rerun all checks (signal revalidation via the terms source, current
  account incl. manual stop, market context, contract metadata, risk) and require an
  identical binding; then one `BEGIN IMMEDIATE` transaction moves approved ->
  consumed only if still approved and unexpired. A retry with the same request id
  returns the same stored result marked as a replay; a different request id fails.
- Actor: the terminal interface records the configured approver name and the OS
  login as audit labels. A local terminal is not authentication. Automated tests use
  channel `automated_fixture` and actor `fixture:automated-test`, never "Taz".

### Acceptance examples

The 17 cases in Taz's G4 specification, including: $10 budget with 1,000 selected
MSFT shares at $250 and event stop $248.75 gives $1,250 stop loss + $1 costs =
$1,251 and the `stop_estimate_above_budget` warning; approval fails until that exact
token is acknowledged. $25,000 displays as `$25,000.00`; confirmations `250`,
`2,500` and missing fail; exact passes; a revised budget invalidates the earlier
confirmation. A share ticket shows position value with no exposure warning.
A long call shows $450 premium exposure with the stop estimate unavailable.

### Migration and rollback

`tickets.sqlite` is new; nothing is migrated from existing stores. Legacy
`ApprovalRecord`-shaped rows or approval rows missing the schema-2 binding fields
fail validation and can never be consumed. Rollback: revert the G4 commit; the
ticket database is ignored by older code and can be kept for audit. No other
database schema changes.

### Outstanding dependencies (not G4)

- Live adapter wiring for account snapshots, market context, contract metadata and
  market observations (Steps 11, 13, 15, 20). G4 takes them as injected adapters.
- Next earnings date, timeframe narrative and option-choice rationale on the ticket
  (CLAUDE.md rule 4): need Step 09 evidence and Steps 11–13. Displayed as not
  supplied rather than invented.
- Broker idempotency, order submission, fills and reconciliation (Step 15/16).
- G3 points still open: ex-dividend revisions retire armed signals until the next
  scan; automatic volume evidence for EP/VCP/cup.
- External blueprint wording (hard loss stops, $100) still needs its owner's update.

## Implemented and verified (Claude, 2026-10-02, cloud container)

Status: **implemented and locally verified by the implementer; awaiting Astra's
audit.** Not reviewed by anyone else. G5 not started.

Changes:
- `src/desk/tickets.py` (new): `TicketRequest`, `MarketObservation`, `RiskInputs`,
  proposal builder, binding, acknowledgement tokens, `TicketStore`, plain-language
  `render` and the `python -m desk.tickets` terminal interface.
- `src/desk/risk.py`: no `exposure_above_budget` on share tickets; new
  `position_value_usd` and `signal_event_valid_until` decision fields.
- `src/desk/contracts.py`: schema-2 `ApprovalRecord`; the two new decision fields;
  `terms_sha256` comment corrected (not an approval binding).
- `tests/test_tickets.py` (40 cases) and `tests/ticket_support.py` (fixture adapters).
- Docs: `TICKETS.md` (new), `RISK_TERMS.md`, `GAP_REPAIR_PLAN.md` (active ownership),
  `REPAIR_PLAN.md`, `CLAUDE.md` (G4 pointer, superseded loss-halt wording), `README.md`.

Required acceptance tests (all in `tests/test_tickets.py`):
1 `test_share_ticket_defaults_to_stop_budget_and_shows_plain_terms`;
2 `test_selected_quantity_needs_an_explicit_choice`;
3 `test_ten_dollar_budget_with_1000_selected_shares_needs_exact_acknowledgement`;
4 `test_share_position_value_without_noisy_exposure_warning`;
5 `test_option_exposure_and_unavailable_stop_estimate_stay_distinct`;
6 `test_exact_budget_confirmation_for_25000`;
7 `test_every_bound_request_edit_needs_a_new_version_and_approval` (9 edits),
  `test_option_leg_edits_need_a_new_version` (3), `test_changed_independent_terms_after_approval_block_consumption` (4),
  `test_changed_option_contract_identity_blocks_consumption`, `test_tampered_stored_ticket_cannot_be_consumed` (3);
8 `test_warning_acknowledgements_are_exact_per_version_and_value`, `test_acknowledgement_cannot_override_a_blocking_check`;
9 `test_forged_unknown_and_legacy_approvals_fail`, `test_copied_risk_result_and_actor_claims_are_not_approval`;
10 `test_rejection_revocation_and_expiry`; 11 `test_restart_preserves_state_and_history`;
12 `test_concurrent_and_repeated_consumption_yield_one_permission`;
13 `test_stale_or_revised_evidence_blocks_but_fresh_unchanged_evidence_passes`;
14 `test_touched_stop_or_invalidated_event_blocks_consumption`, `test_explicitly_invalidated_event_blocks_consumption`;
15 `test_manual_stop_after_approval_blocks_consumption`; 16 `test_new_blocking_risk_failure_after_approval`;
17 `test_complete_positive_path_from_persisted_signal_to_single_use` (real scanner
event, `EventRiskSource` revalidation, risk, display, fixture approval, consumption);
plus `test_terminal_interface_round_trip` for the CLI.

Verification (cloud container, not the iMac or the Codex host):
- Python 3.12.3: `python -m pytest -q -W error tests/test_tickets.py` 40 passed;
  `python -m pytest -q -W error` **932 passed**.
- Python 3.13.14 (extra): same, 40 and **932 passed**.
- Python 3.14: only 3.14.0rc2 is obtainable here; with pydantic 2.13.5 it fails at
  conftest import (`typing._eval_type() got an unexpected keyword argument
  'prefer_fwd_module'`) on the unchanged bb75602 baseline too. **No 3.14 result is
  claimed.** Baseline before edits: 892 passed on 3.12.3 and 3.13.14.
- `git diff --check` passed. CLI demo transcripts with labelled synthetic adapters:
  `research/ai-trading/g4/` in the project folder (outside the repo).

Found and fixed during self-review: the CLI reused the real clock for show/revoke
while the demo adapters used the fixture clock; every command now uses the
adapters' clock when adapters are configured. `python -m desk.tickets` now runs
through the importable module so adapter factories share its classes.

Remaining (owners): live adapters for account snapshots, market context, contract
metadata and observations (Steps 11/13/15/20); earnings date, timeframe notes and
option rationale on the ticket (Step 09, 11–13); broker submission, idempotency,
fills and reconciliation (Steps 15/16); a signed or tamper-evident ledger beyond
local SQLite (not scheduled); G3 ex-dividend retirement and automatic volume
evidence (G5/Step 10 review); external blueprint wording (blueprint owner thread).

## Astra's audit of d1e4514 and resolution (2026-10-02)

Recorded per the G5 prompt line "First resolve Astra's G4 findings. Record each
finding, its resolution, test evidence and commit." Astra audited `d1e4514` with local fixtures (no source edits, no orders) and
reproduced five defects; Taz relayed them at 07:25Z. All five are fixed in
`d529880` ("G4 fixes: close five approval/consumption gaps Astra reproduced") with
regressions in `tests/test_g4_fixes.py`; each listed test failed on `5b63b8a`
(G4 code unchanged since `d1e4514`) before the fix.

| # | Finding (Astra) | Resolution in `d529880` | Regression tests | G4 prompt line it enforces; label |
| --- | --- | --- | --- | --- |
| 1 | Manual stop switched on after the account read still allowed consumption; the final transaction did not recheck the account revision. | The final approve/consume transaction holds the account store's revision (`RiskStateStore.held_revision`) and refuses if it differs from the revision the check read. *Superseded by the second audit round below: `held_revision` was WAL-unsafe and could make a manual stop fail; replaced by `held_account` and a final risk rerun.* | `test_manual_stop_during_consumption_check_blocks`, `test_manual_stop_during_approval_check_blocks`, `test_account_guard_refuses_a_competing_writer_with_a_short_timeout` (renamed; it shows exclusion, not waiting) | "Recheck signal state, current required evidence, risk conditions and manual stop before consumption"; "The separate manual stop remains blocking." Checked (reproduction); locking is an engineering decision |
| 2 | Terms were read twice; risk could validate a newer revision while the approval stayed bound to the older one. | One `RiskTerms` snapshot per check; `risk.evaluate` validates the same snapshot the binding records. | `test_risk_and_binding_use_one_terms_snapshot`, `test_newer_terms_revision_blocks_consumption` | "Approval issuance must load the stored ticket and independently rerun required checks"; changed terms "require a new ticket version and approval." Checked |
| 3 | A slow check crossed the approval deadline yet consumption succeeded, because the final check reused the start time. | The final transaction takes a fresh clock (`RiskInputs.clock`, else UTC wall clock, never earlier than the start) for expiry. | `test_slow_check_crossing_expiry_blocks_consumption`, `test_final_clock_never_runs_backwards` | "Expiry cannot extend beyond the relevant evidence-validity boundaries." Checked; clock source is an engineering decision |
| 4 | A passed time stop did not block entry (consumed 4 s after the required exit). | Time stop blocks prepare, approve and consume once passed; approvals never outlive it; rechecked with the fresh clock. | `test_passed_time_stop_blocks_consumption`, `test_time_stop_crossed_during_check_blocks_consumption`, `test_ticket_prepared_after_its_time_stop_is_blocked` | Bound "declared exit instructions"; "A newly failed prerequisite prevents consumption." Checked; blocking entry after the exit time is logic, not trading research |
| 5 | Displayed prices lost precision ($250.0049 shown as $250.00). | Limit, stop and target are shown exactly as accepted. | `test_display_shows_exact_limit_and_stop`, `test_price_formatting_is_exact` (parametrized) | "Show: … Entry limit, structural stop and target" exactly. Checked. Open for Taz: SEC Rule 612 bars sub-penny orders for stocks ≥ $1 (https://www.law.cornell.edu/cfr/text/17/242.612); rejecting such limits is not implemented |

Evidence (cloud container): 994 strict tests passed on Python 3.12.3 and 3.13.14 at
`d529880`; see `G5-combined-verification.md` for the final counts at the G5 commit.
Status: **fixed, awaiting re-audit.** Astra is unavailable until 2026-10-07; Taz
arranged an independent Claude review and a DeepSeek review of the iMac side in her
place. Neither is Astra's approval. Python 3.14 results come only from the iMac run.

## Second audit round: three audits of 6d21ddf (2026-10-02)

Taz relayed three audits at 20:18Z: Astra (code audit, 997 passed on Python 3.12.14
and 3.14.6), an outside Claude session (997 passed on 3.12.13 and 3.14.6, macOS) and
DeepSeek on the iMac (997 passed on 3.14.7 and 3.13; no critical findings). The full
text is kept in the project folder at
`research/ai-trading/g5/audits/three-audits-2026-10-02-verbatim.md`; the consolidated
list with outcomes is `audits/consolidated-findings-2026-10-02.md` next to it.

Every listed regression lives in `tests/test_audit_closure.py` unless named
otherwise. 30 of its first 31 tests fail on `6d21ddf` (checked by running them against
that commit); the one that passes there is the positive control
(`test_unchanged_real_signal_passes_the_fence`).

| # | Source | Finding | Resolution | Regression tests | Prompt line; label |
| --- | --- | --- | --- | --- | --- |
| R1 | Astra A (P1); Claude #6 | Account, quote, market and signal-terms ages were judged at the check's start, not at the final write (36 s account and 61 s quote consumed). | `final_evaluation` reruns `risk.evaluate` under the final locks at the final clock, on the same snapshots and the current account; existing boundaries unchanged; `decided_at`/`consumed_at` are the final time. | `test_evidence_expiring_during_consumption_is_refused[account,market,quote,terms]`, `…_during_approval_is_refused[…]`, `test_shorter_current_event_deadline_expiring_during_consumption_is_refused`, `test_boundary_ages_still_pass_and_record_the_final_time` | G4.E "Expiry cannot extend beyond the relevant evidence-validity boundaries"; "Recheck … current required evidence … before consumption". Checked |
| R2 | Astra B (P1); Claude #5 | A real event invalidated after terms resolution was still consumed. | Terms sources expose `held_event`; `SignalStore.held_event` holds the signal write lock through the ticket commit; the final step requires the event eligible with the same digest. A source without it cannot approve or consume. | `test_real_signal_change_after_resolve_blocks_consumption[invalidate,suspend,withdraw]`, `test_real_signal_invalidated_during_approval_is_refused`, `test_changed_event_generation_at_commit_is_refused`, `test_unchanged_real_signal_passes_the_fence`, `test_signal_source_without_a_fence_cannot_consume`, `test_held_event_blocks_signal_writers_until_released` | G4.E "Recheck signal state … before consumption"; "A newly failed prerequisite prevents consumption." Checked; locking is an engineering decision |
| R3 | Astra C (P2); Claude #11 | The account fence relied on rollback-journal mode; WAL let a manual stop commit inside it. | `RiskStateStore.held_account` uses `BEGIN IMMEDIATE` (single writer lock in both modes); the store does not rewrite the mode. | `test_account_guard_blocks_writers_in_both_journal_modes_after_reopen[delete,wal]`, `test_manual_stop_mid_check_blocks_in_both_journal_modes[delete,wal]` | G4.2 "The separate manual stop remains blocking." Checked; engineering decision |
| R4 | Claude #2 | A manual stop failed after 5 s while a consumer held the account read lock and waited for the ticket lock. | Fixed lock order (ticket, signal, account last); the account lock is never held while waiting for another lock; `set_manual_halt` waits up to 30 s and raises "Manual stop was NOT recorded" if the store stays busy. | `test_manual_stop_engages_while_a_consumer_waits_for_the_ticket_lock` (production timeouts), `test_manual_stop_issued_while_the_guard_is_held_waits_and_then_lands`, `test_busy_store_reports_that_the_manual_stop_was_not_recorded` | G4.2 manual stop. Checked; engineering decision |
| R5 | Claude #7 | Any routine account snapshot during a check refused approval/consumption. | The final rerun uses the current snapshot; only a change in checks or binding refuses. | `test_routine_unchanged_snapshot_mid_check_does_not_refuse` | G4.D3 "Fresh evidence with unchanged terms must be revalidated without manufacturing new executable terms". Checked |
| R6 | Claude #20 | `set_manual_halt` could be refused by a newer snapshot revision. | Switching the stop on has no revision precondition; a resume still needs the current revision. | `test_manual_stop_is_never_refused_by_a_newer_snapshot` | G4.2 manual stop. Engineering decision |
| R7 | Claude #1 | A piped script could approve, and the record defaulted to "Taz". | `--actor` is required (no default); CLI approval refuses non-interactive input. Still a label, not authentication (Step 13). | `test_tickets::test_cli_approval_refuses_piped_input_and_needs_a_typed_name`, `::test_terminal_interface_round_trip` | G4 "Record Taz's explicit approval"; "Do not simulate Taz's approval outside clearly labelled automated fixtures"; "Do not trust … actor claims". Engineering decision |
| R8 | Claude #8 | Two UPDATEs reopened a consumed approval. | Insert-only `consumptions` table (primary key `approval_id`, UPDATE/DELETE refused) plus an audit cross-check; `desk-tickets-v1` files migrate in place. A keyed signature is not built (needs a key outside the database). | `test_editing_two_rows_cannot_reopen_a_consumed_approval`, `test_audit_trail_alone_still_marks_an_approval_used`, `test_v1_database_migrates_consumptions_into_the_insert_only_table` | G4.E "Retrying the same consumption cannot create a second permission"; F "Preserve audit history during migration". Checked |
| R9 | Claude #21 | The permission did not name the bound terms. | The permission id hashes `binding_sha256`; the outcome and audit carry it. | `test_consumption_permission_names_the_bound_terms` | G4.D binding; Step 15 hand-off. Engineering decision |

Not changed here, waiting for Taz (details in the consolidated list): Claude #3
(P&L warning values in the binding conflict with practical approval while positions
are open, but G4.C says "a changed warning value must not authorize the ticket");
Claude #4 (EP chase measured from the open); Claude #9 (sub-penny limits, Rule 612);
Claude #10 (SPY/QQQ ex-dividend days); Claude #14 (volume-only revisions, needs host
evidence). Status: **fixed in the audit-closure commit, re-audited by Astra (next section).**

## Third round: Astra's re-audit of ee4dc90 and Taz's decisions (2026-10-02)

Taz relayed Astra's re-audit at 21:02Z. She confirmed 1053 passed on Python 3.12.14
and 3.14.6 and that the original probes pass, and found two remaining timing defects.
Taz answered the open questions in the same message and said the decisions do not
close G4/G5. Regressions are in `tests/test_reaudit_closure.py` (P1a, P1b) and
`tests/test_taz_decisions.py` (decisions 1–3).

| # | Source | Finding | Resolution | Regression tests | Prompt line; label |
| --- | --- | --- | --- | --- | --- |
| P1a | Astra re-audit | A manual stop that waited behind a routine snapshot was refused ("Control time predates the account snapshot") when that snapshot's as-of time was later than the stop request. | Switching the stop on no longer checks the snapshot time. The request time stays in the audit (`at`, `requested_at`); the commit time is read under the lock (`committed_at`) with `snapshot_as_of`. A resume keeps the revision check and the request-time chronology check; both timestamps must be timezone aware. | `test_manual_stop_waiting_on_a_routine_snapshot_still_engages[2]` (Astra's two-store scenario and her positive control; the newer case failed on `ee4dc90`), `test_resume_keeps_the_revision_and_chronology_checks`, `test_manual_control_timestamps_must_be_timezone_aware` | G4.2 manual stop. Engineering decision |
| P1b | Astra re-audit | In rollback-journal mode a reader on the ticket store delayed COMMIT after the freshness check (approved/consumed at about 31 s account age). | The final ticket transaction opens with `BEGIN EXCLUSIVE`, before the signal and account locks and before the clock is read, so reader waits come first. Lock order is unchanged. | `test_reader_wait_happens_before_the_final_clock_in_rollback_mode[12]` (account, quote, market, terms, approval and event deadlines × approve/consume; all 12 failed with `BEGIN IMMEDIATE`), `test_wal_readers_never_delay_the_final_commit[12]`, `test_commit_follows_the_recorded_final_time_promptly[4]` | G4.E1, E2. Engineering decision; SQLite https://www.sqlite.org/lang_transaction.html |
| D1 | Taz decision 1 | Sub-penny limits were accepted. | Refused at prepare, never rounded: shares Rule 612; options whole cents, single-leg class schedule (Cboe Rule 5.4(a)), multi-leg whole cents (Cboe Rule 5.33(f)(1)); an unknown option class blocks. | decision 1 tests (26 cases) | User policy; Sourced |
| D2 | Taz decision 2 | A one-cent P&L move refused the ticket. | `daily_loss`, `weekly_loss`, `account_drawdown` are re-asked only when new, re-thresholded or worse by 10% of threshold from the acknowledged value (cumulative); improvements are shown as the latest final check. | decision 2 tests (12 cases) | User policy (Taz's suggested starting policy) |
| D3 | Taz decision 3 | The EP chase was measured from the open. | Measured from the frozen opening-range high with the executable limit, in risk and in scanner revalidation; the 3% stays an Assumption. | decision 3 tests (7 cases) | User policy |

Decisions 4 (free Massive plan, SPY/QQQ dividend reconciliation on the iMac) and 5
(`SSL_CERT_FILE`, investigate first) need the iMac; read-only steps are in
`research/ai-trading/g5/imac-commands.md`. Status: **fixed in the re-audit closure
commit (`5e24e02`); Astra checked it (next section).**

## Fourth round: Astra's re-audit of 5e24e02 (2026-10-03)

Astra closed P1a and P1b (her original probes: 11 passed, 1 deliberately deselected,
on Python 3.12.14 and 3.14.6; 1130 passed on both) and reviewed decisions 1–3 with no
defect in 1 or 3. She found one gap in decision 2.

| # | Finding | Assessment | Resolution | Regression tests | Label |
| --- | --- | --- | --- | --- | --- |
| P2 | Relaxing a loss/drawdown threshold (e.g. daily $200 → $500) made the warning disappear, and the original ticket was approved or consumed without a new version. | Confirmed: all six of her cases fail on `5e24e02`. The cause is ambiguity: a warning missing from a later check can come from a real improvement or from a relaxed threshold, and the binding held thresholds only inside active warnings. Taz's decision says a threshold change re-asks. | The binding now carries `warning_policy` (the three thresholds, the 10% band and the measures). Any change re-asks, even on a ticket that showed no warning (fail closed; one extra version in that case). A ticket without the snapshot is refused, never assumed unchanged. The display shows the warning levels. | `test_taz_decisions::test_relaxing_a_threshold_that_clears_the_warning_is_re_asked[6]`, `::test_a_warning_cleared_by_real_improvement_still_consumes[3]`, `::test_policy_change_refuses_even_a_ticket_without_warnings`, `::test_ticket_without_a_policy_snapshot_is_not_assumed_unchanged`, `::test_ticket_shows_the_warning_levels_it_was_checked_against` | User policy (decision 2); engineering for the snapshot |

Status: **fixed in the policy-snapshot commit; awaiting Astra's check and iMac evidence
for that commit.**
