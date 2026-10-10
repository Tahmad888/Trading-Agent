# G5a CP3 — backlog findings repairs (R1–R5)

2026-10-10. Implementer: Claude, authorized by Taz forwarding
`CLAUDE_G5_BACKLOG_FINDINGS_REPAIR_PROMPT_2026-10-10.md`. Base
`c41142ee8b364f8a2c49c3aa0940dc70020bdb3a` on `codex/repair-step-01-baseline`
(origin had not advanced when work started). Hierarchy: G5 → G5a → parent CP3,
existing child 3. G5 stays open; Step 09, host deployment, provider calls,
option-size normalization and orders are out of scope. Inputs: Claude's
`G5_AUDIT_BACKLOG_REPORT.md` and Astra's `G5_BACKLOG_REPORT_ASTRA_REVIEW_2026-10-10.md`.

Each package below records its requirement, evidence label, producers and
consumers, and acceptance examples before its implementation. No trading rule,
risk budget, quote-age threshold, clock tolerance, volume threshold or earnings
policy changes in this round.

## R1 — publication chronology for newly armed candidates

### Requirement (recorded before implementation)

**Checked (A-O1, Claude probe `test_batch_a_observation_user_pick.py`,
reproduced on c41142e):** a Taz pick added after the close and prepared by the
10:00 slot at 10:01 is appended through `ScanLog.add_armed` →
`SignalStore.save_armed(append=True)` with no `armed_floors` row, so the
09:45–10:00 crossing that completed before its preparation triggers. The
volume-pending branch uses the same writer and has the same gap.

The rule already exists for close-job publication (G5a CP3 close-recovery F1):
a candidate first published at or after the session open never consumes a bar
ending at or before its publication. R1 applies that rule unchanged to these
two paths; it is not a new strategy rule.

Producer → consumer:

- `scanner.run` intraday slot → pending Taz picks (`close_scan(..., preparing=True)`)
  → `ScanLog.add_armed` → `armed`, `armed_history` → `SignalStore.observe`
  (`start_after` = max(peer `last_bar`, floor)) → `intraday_scan` triggers.
- Same for volume-pending names (`pending-volume` file and the close job's
  `volume_pending`), with only `VOLUME_SETUPS` armed.

Contract:

- Publish the new candidates and their floors in one `BEGIN EXCLUSIVE`
  transaction. Read the publication clock inside it, after any lock wait and
  after provider preparation has returned (production passes its existing
  decision-clock callback; deterministic replays use the explicit clock).
- Refuse a publication clock earlier than the evaluation clock; nothing is written.
- A publication at or after the target session's open writes a floor equal to
  that clock for candidate IDs not already armed. Already armed candidates keep
  their floor (or absence of one); an existing floor row is never revised.
  Nothing is backdated to evaluation or lock-start time.
- A publication before the open (genuine pre-open preparation) gets no floor,
  as for the close job.
- A failed preparation, or a refused publication, adds neither candidate nor
  floor and leaves the names unprepared so a later slot retries them.

Inventory of other arming paths (not changed here):

- **EP** (`episodic_pivots` at the 10:00 slot and pending retries at 10:15+,
  appended through `ScanLog.add_armed`): its own trigger contract
  (`_ep_entry_hit`) looks for a fresh break of the completed 15/60-minute range.
  It has no publication floor. Reported separately below for a Taz/Astra
  decision; not silently changed.
- **Revision rebuild** (`revision_rebuild.rebuild_pending` →
  `SignalStore.finish_rebuild`): the replacement inherits the retired
  candidate's `last_bar` = `floor_15min(checked)` through the peer rule.
  Reported below.
- **RSI(2) near-close** (`rsi2_estimate`): observes directly at the 15:45 slot
  with its own estimate contract; not an armed-list publication.
- Close-job publication (`commit_close_job`) already implements the rule.

Acceptance examples:

- Refused: LEAD added by Taz after the close, prepared at 10:01, does not
  trigger on the 09:45–10:00 crossing; its floor is 10:01.
- Accepted: the same pick triggers on a crossing completing at 10:30, including
  after a process reopen.
- Control: a pick present at the close (pre-open publication) still triggers on
  the 09:45–10:00 crossing.
- Volume-pending VCP/cup prepared at 09:47 get a 09:47 floor; the close job's
  candidates keep no floor.
- Rollback: revert the R1 commit; floors already written are inert rows.

### Implementation and results

- `SignalStore.publish_armed` (new) does the append, history row and floors in one
  `BEGIN EXCLUSIVE` transaction with the clock read inside it.
  `ScanLog.publish_armed` imports a legacy list first, as `add_armed` does.
- `scanner.run` publishes Taz picks and volume-pending setups through it, with the
  preparation's own evaluation clock (`close_scan`'s `rec.at`, read after its
  provider fetches) as the lower bound, before marking the names prepared.
- EP and rebuild paths are unchanged; `add_armed` remains for EP.
- Tests: `tests/test_publication_chronology.py` (12). On c41142e 11 fail
  (scanner-level ones on behavior; store-level ones because the API is absent),
  the pre-open control passes. Focused suites (close jobs, close recovery,
  rebuild, partial discovery, scanner, lifecycle, volume, G5 integration,
  discovery, symbols) pass. Mutations: 10/10 caught.

### Inventory findings for Taz/Astra (not changed)

- **R1-I1 (Checked, probe `test_r1_ep_inventory_probe.py`):** an EP armed by the
  10:15 pending retry and published at 10:17 has no floor and triggers on a
  15-minute-range break that completed at 10:00. At the 10:00 slot the same
  applies to the 09:45–10:00 bar. EP's contract ("fresh break of completed
  15/60-minute range", chase from ORH per Taz's 2026-10-02 decision) may intend
  the 10:00 case; the retry case is a separate decision. Minimal repair if wanted:
  publish EP through `publish_armed` for retries only, or for both slots.
- **R1-I2 (Checked by reading):** `finish_rebuild` floors the replacement at
  `floor_15min(checked)` with `checked` read before its `BEGIN IMMEDIATE`, so a
  lock wait does not move it. The replacement still cannot see bars ending at or
  before the retired candidate's check. Low; no change.
