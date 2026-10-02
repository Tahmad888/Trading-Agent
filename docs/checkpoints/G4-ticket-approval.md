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
| 1 | Manual stop switched on after the account read still allowed consumption; the final transaction did not recheck the account revision. | The final approve/consume transaction holds the account store's revision (`RiskStateStore.held_revision`) and refuses if it differs from the revision the check read; later controls wait for the write. | `test_manual_stop_during_consumption_check_blocks`, `test_manual_stop_during_approval_check_blocks`, `test_held_revision_makes_a_manual_stop_wait_for_the_final_write` | "Recheck signal state, current required evidence, risk conditions and manual stop before consumption"; "The separate manual stop remains blocking." Checked (reproduction); locking is an engineering decision |
| 2 | Terms were read twice; risk could validate a newer revision while the approval stayed bound to the older one. | One `RiskTerms` snapshot per check; `risk.evaluate` validates the same snapshot the binding records. | `test_risk_and_binding_use_one_terms_snapshot`, `test_newer_terms_revision_blocks_consumption` | "Approval issuance must load the stored ticket and independently rerun required checks"; changed terms "require a new ticket version and approval." Checked |
| 3 | A slow check crossed the approval deadline yet consumption succeeded, because the final check reused the start time. | The final transaction takes a fresh clock (`RiskInputs.clock`, else UTC wall clock, never earlier than the start) for expiry. | `test_slow_check_crossing_expiry_blocks_consumption`, `test_final_clock_never_runs_backwards` | "Expiry cannot extend beyond the relevant evidence-validity boundaries." Checked; clock source is an engineering decision |
| 4 | A passed time stop did not block entry (consumed 4 s after the required exit). | Time stop blocks prepare, approve and consume once passed; approvals never outlive it; rechecked with the fresh clock. | `test_passed_time_stop_blocks_consumption`, `test_time_stop_crossed_during_check_blocks_consumption`, `test_ticket_prepared_after_its_time_stop_is_blocked` | Bound "declared exit instructions"; "A newly failed prerequisite prevents consumption." Checked; blocking entry after the exit time is logic, not trading research |
| 5 | Displayed prices lost precision ($250.0049 shown as $250.00). | Limit, stop and target are shown exactly as accepted. | `test_display_shows_exact_limit_and_stop`, `test_price_formatting_is_exact` (parametrized) | "Show: … Entry limit, structural stop and target" exactly. Checked. Open for Taz: SEC Rule 612 bars sub-penny orders for stocks ≥ $1 (https://www.law.cornell.edu/cfr/text/17/242.612); rejecting such limits is not implemented |

Evidence (cloud container): 994 strict tests passed on Python 3.12.3 and 3.13.14 at
`d529880`; see `G5-combined-verification.md` for the final counts at the G5 commit.
Status: **fixed, awaiting re-audit.** Astra is unavailable until 2026-10-07; Taz
arranged an independent Claude review and a DeepSeek review of the iMac side in her
place. Neither is Astra's approval. Python 3.14 results come only from the iMac run.
