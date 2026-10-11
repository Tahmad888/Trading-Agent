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

## R2 — local database contention is not a provider STOP

### Requirement (recorded before implementation)

**Checked (D O1; Claude's injected probe 2026-10-09 and Astra's real-lock probe
2026-10-10, both reproduced on c41142e):** when the quote store's reservation
transaction cannot get its SQLite lock, `QuoteStore.fetch` reaches its generic
branch, records a durable global `STOP` with `TRADIER_FETCH_FAILURE`, and after a
reopen even the healthy GOOD quote needs `EXPLICIT_RESUME_REQUIRED`. No request
was sent; nothing about the provider was learned.

Producers → consumers:

- `TradierClient.get` → `reserve_request` (`QuoteStore.held`, `BEGIN EXCLUSIVE`)
  before dispatch → `QuoteStore.fetch` outcome rows / global STOP →
  `QuoteStore.read` / `proof` → `TradierRiskSource.verified`, `resolve`,
  `held_event` (final approve/consume fence) and `executable` →
  `tickets.recheck` / `final_problem` / approve and consume refusals.
- `QuoteStore.begin` and `finish` (also `held`) around the request.
- `tradier_quotes.main refresh` command output and exit.

Contract:

- Any SQLite failure inside the quote store is a fixed local code:
  `LOCAL_QUOTE_STORE_BUSY` (busy/locked) or `LOCAL_QUOTE_STORE_FAILURE` (other).
  Raw SQLite text is never shown or stored.
- A local failure never writes `STOP` and never needs an operator resume.
  A reservation failure dispatches nothing.
- The attempted names are withdrawn: `FAILED` rows with the local code when the
  store is writable. If it is not, the withdrawal is held in the process and
  written before the next check; until then reads of those names refuse with
  `LOCAL_QUOTE_EVIDENCE_WITHDRAWN`, and a started check (durable `START`) already
  keeps them unavailable after a reopen. Unattempted peers stay readable.
- The withdrawal changes the attempted names' generation, so a ticket prepared on
  the old evidence cannot be approved after a fresh check; a fresh normal check
  recovers without resume.
- Real 401/403/429/transport/reply failures keep the durable STOP and explicit
  resume. If the STOP row itself cannot be written, the STOP is held in the
  process and written before any later check; nothing is dispatched meanwhile.
- Local rate and workload limits keep their existing codes and behavior.
- Refusals show the code: ticket approve/consume refusals show a fixed evidence
  code instead of the exception class; the refresh command's JSON names the
  cause (`local`, `provider` or `request`).
- Append-only events are never updated or deleted.

Acceptance examples: Astra's probe now gives `LOCAL_QUOTE_STORE_BUSY`, one send,
no STOP, GOOD readable after reopen, LEAD unavailable until a fresh check, which
then succeeds without resume. A 401 still stops and needs resume.
Rollback: revert the R2 commit; local failures then become STOP again.

### Implementation and results

- `tradier_quotes`: `LocalQuoteStoreUnavailable`, `local_failure`, `cause`;
  `held()` and store creation translate SQLite errors; `fetch` handles a local
  failure before the budget and provider branches; `_withdraw` and
  `_persist_held` keep process-held withdrawals and STOPs until written;
  `read` refuses them. The refresh command adds `cause`.
- `tickets`: approve/consume refusals show the fixed evidence code (`_cause`).
- `docs/TRADIER_TICKET_QUOTES.md` documents the local-failure policy.
- Tests: `tests/test_tradier_local_store.py` (14). On c41142e 9 fail and the 5
  controls (local rate limit; 401/403/429/transport STOP) pass. Astra's probe
  now fails its old expectation with `LOCAL_QUOTE_STORE_BUSY` (it passes on
  c41142e). Tradier, ticket, quote, after-hours and risk suites pass (726).
  Mutations: 10/10 caught.
- Limits: an in-process withdrawal is lost if the process exits before the store
  becomes writable. If the check had started, its durable `START` row still keeps
  the name unavailable after a reopen; if the store refused even the start,
  nothing was sent and the earlier evidence stands as it was before the attempt.
  An unwritable provider STOP is likewise held only by that process; a new
  process can send once more before it sees a provider refusal again.

## R3 — contract-size metadata and malformed offline evidence

### Requirement (recorded before implementation)

**C-1 (Checked, Claude probe at c41142e):** `tastytrade_quotes` builds
`Instrument.metadata` from text, integer and boolean fields only. The transport
decodes JSON fractions as `Decimal`, so `shares-per-contract` 100.0, 100.5 or
10.0 (and an injected float 100.5) disappears. `quote_check`'s pair check then
reports `INCOMPLETE`/`PARTIAL` and subscribes the counterpart; the same value on
the Tradier side is `CONFLICT`.

Producer → consumer: tastytrade instrument row → `Instrument.metadata` →
`quote_check.diagnostic` pair check (`diagnostic_pair.size_match`) → pair status,
counterpart registration, `provider_fields` in the report.

- Keep a fractional `Decimal` (or float) as its exact text. `size_match` already
  refuses any non-integer text as `CONFLICT` (`INVALID_PROVIDER_SIZE`); no default
  multiplier, no ×100, no adjusted-contract support.

**D O2 (Checked):** `saved_greek_audit.audit` and `tradier_readiness.prepare`
call `.get` on nested values without checking their type; Astra's probe
`prepare({'identity_capture': []}, {'rounds': [None]}, ['SPY'], …)` raises
`AttributeError`. Consumers are each tool's CLI report.

- Check every root and nested shape before use: capture root, `measurements`,
  `records`, `records_dropped`, `identity_capture` and each record's
  `source_time` (audit); both roots, `identity_capture`, `rounds`, each round,
  `observations`, each observation and `issuer_reference` (readiness).
- Damage that cannot be tied to one symbol (a bad root, list or round) is a
  fixed capture issue that withholds every draft or makes the audit a refusal.
  Damage tied to one row or symbol isolates that row or symbol; healthy peers
  keep their result. The input hash and the historical-only labels stay. No
  catch-all exception handler; no state restored from incomplete logs.
- The CLIs report invalid JSON as a structured refusal and exit 1.

**D-O1 (Checked):** a Webull row spelled `BRK/B` beside the requested `BRK.B` is
silently ignored. The readiness report now lists, per symbol, the rows it used
and any alternate spellings found on either side. A Webull alternate row whose
identity differs from the exact row is a contradiction and withholds the draft;
a Tradier observation filed under another spelling also withholds it, since the
draft could not say which observation the reviewer should trust.
Drafts stay `DRAFT_NOT_INSTALLABLE`; equality of spellings never approves anything.

Acceptance: the four fractional sizes give `CONFLICT` and keep the counterpart
unregistered while the healthy stock peer stays; integer 100 matches; missing
size stays `INCOMPLETE`; Astra's malformed-round call returns a structured
refusal; malformed rows are isolated with peers kept.

### Implementation and results

- `tastytrade_quotes._captured` keeps fractional `Decimal`/float metadata as text.
- `saved_greek_audit.shape_issues` and `tradier_readiness.capture_issues` check
  shapes first; per-row and per-symbol checks isolate attributable damage;
  `tradier_readiness` adds `input_sha256`, `capture_issues` and
  `evidence_inventory`. Both CLIs refuse invalid JSON and capture-shape damage
  with exit 1. Docs: `OPTION_CONVENTIONS.md`, `TASTYTRADE_GREEKS.md`,
  `TRADIER_TICKET_QUOTES.md`.
- Tests: `tests/test_offline_evidence_shapes.py` (33). On c41142e 30 fail; the
  integer, missing and contradictory-size controls pass. Focused option, quote,
  Greek, readiness and diagnostic-pair suites pass (384). Mutations: 11/11 caught.

## R4 — host-command and wrapper evidence correctness

### Requirement (recorded before implementation)

Findings E1–E3 of the 2026-10-10 backlog audit (Checked with stubs at c41142e) and
the wrapper's evidence reporting. Consumers: Taz pasting the three blocks of
`docs/TASTYTRADE_QUOTES.md` into the iMac Terminal (zsh; the inner scripts run in
`bash`, macOS ships 3.2), and anyone reading `tools/g5_tradier_comparison.sh`
output or `summary.json`.

- **Part 1.** Before anything changes: full 40-character SHA, the folder (a failed
  `cd` stops), a clean tracked tree and the dedicated branch. Fetch once. The SHA
  must be fetched, reachable from the fetched branch and a forward descendant of
  (or equal to) HEAD. Merge exactly that SHA (`--ff-only`), never the branch head.
  A host past it stops and is not rewound. Refused examples: advanced origin
  (c41142e moved the host to the newer unreviewed head and then printed STOP),
  absent SHA, unpushed SHA, host already past, fetch failure, dirty tree, wrong
  branch, short or unreplaced SHA, failed `cd`. Successful: origin at or past the
  reviewed commit, from any Terminal directory.
- **Part 2.** An explicit guarded script: fail on `cd`, require the exact HEAD,
  branch and clean tree, and confirm `.venv/bin/python` and the tool exist before
  running it. The tool keeps the Terminal as stdin (hidden prompts). Refused at
  c41142e: none; a failed `cd` ran another directory's tool.
- **Part 3.** Per command, its real exit and its file's state, separately. Never
  claim an absent file was written. Only a valid measured clock file is passed as
  `--host-clock`; a missing, malformed or unmeasured clock is named in the
  summary. Later read-only captures still run after a failure; the script ends
  non-zero when any command failed or left no valid report. Collection is never
  live-data approval.
- **Wrapper.** The same properties for the selection, clock, three captures and
  the normalized Greek file; selection success is printed only after exit 0 and
  a valid pair file. Same pair, raw files, durations, request caps and read-only
  calls.

Not in scope: running anything on the iMac or against a provider, and bash 3.2
itself (not available in this container; the scripts avoid 4.x-only constructs:
no associative arrays, `mapfile`, `${var,,}` or `|&`).

### Implementation and results

- `src/desk/evidence_files.py`: `state(path, kind)` gives `VALID`, `MISSING`,
  `MALFORMED` or, for a clock, `UNAVAILABLE` (a well-formed "not measured"
  record). A report needs a JSON object with a string `status`; a clock is valid
  only when `load_host_clock` accepts it as `MEASURED`; a selection needs
  `option_pair.selection_id` and call/put symbols. `python -m desk.evidence_files
  KIND FILE` prints the state.
- `docs/TASTYTRADE_QUOTES.md` Parts 1–3 rewritten as described; Part 2 is now a
  guarded script that `exec`s the tool, so stdin stays the Terminal.
- `tools/g5_tradier_comparison.sh` records the selection exit and file state,
  attaches the clock only when `VALID` and says so otherwise, and `summary.json`
  gains `evidence_files` (all six), `report_errors` as `REPORT_<state>` and
  `host_clock_passed_to_captures`.
- Tests: `tests/test_host_commands.py` pastes each Part's block, as written in the
  guide, into real `bash` (5.2.21) and `zsh -f` (5.9, installed in this cloud
  container only for the test) with stub `git` (a linear OLD → reviewed → newer
  history) and stub `python` (only `-m desk.evidence_files` runs for real). Part
  2's hidden prompt is checked under a pseudo-terminal: the fake tool sees a
  terminal on stdin, reads 15 characters, and the typed text is not echoed.
  `tests/test_evidence_files.py` covers the checker; `tests/test_comparison_command.py`
  gains selection, exit-zero-without-file and clock cases.
- Results: 121 focused tests pass (`test_host_commands` 78 including both shells,
  `test_comparison_command` 24, `test_evidence_files` 19); the decision-module import
  guard in `test_quote_measure.py` now lists `evidence_files.py` as a diagnostic. On
  c41142e the host-command and wrapper tests give 72 failed, 30 passed (no collection
  error); the passing ones are the controls (origin at the reviewed commit, tool run
  from another Terminal directory, Part 2's terminal stdin, guards that already
  stopped). `test_evidence_files` imports the new module and is not counted as
  before/after proof. Mutations: 20/20 caught, including the pre-commit
  `SUMMARY` line that printed the clock path.
- Not shown here: bash 3.2 itself, macOS `mktemp`, and the real credential tool.
  These blocks are tested against stubs; that is not host or deployment acceptance.

## R5 — diagnostic integrity and carried notes

### Requirement (recorded before implementation)

- **B-1** (`webull_quote_check`, carried W1). Producer: `WebullData._call` sleeps
  for the per-path throttle, signs, then calls the transport; `BoundedData._call`
  counts the request first. Consumer: the diagnostic report, read by Astra/Taz to
  compare sources. The old `request_elapsed_ms` spanned all of it (0 ms vs 886 ms
  in the 10-10 re-run). Required: a transport bracket around the urllib open and
  read only, the wait before it shown separately, and the whole call labelled as
  workload, not latency. Budgets, signing and routes unchanged; no send time is
  invented.
- **B-2** (`snapshot_quote_check`, `webull_quote_check` CLIs). Local arguments and
  the output path are checked before any client is built or credentials are
  needed. Refused examples: `--rounds 9`, a NaN interval, a bad symbol, a duplicate
  symbol, a negative strike with missing credentials must report the argument
  error. Successful control: valid arguments without credentials still report
  `UNAVAILABLE` (`CREDENTIALS_MISSING` / `WebullError`). No request in either case.
- **B-3.** Both CLIs refuse an existing `--output` (also a symlink) before any
  client and keep its bytes; a file that appears during the run is not replaced
  (exclusive create) and the printed report says
  `OUTPUT_EXISTS_NOT_OVERWRITTEN`; another write error says
  `OUTPUT_WRITE_FAILED_<error>`; both exit 1. Inventory of
  `quote_measure.write_report` callers: `webull_quote_check`, `snapshot_quote_check`
  (changed to the exclusive writer); `quote_check`, `quote_measure` clock/volume,
  `option_conventions` and `tradier_option_check` CLIs and the comparison wrapper's
  own `summary.json` (unchanged; the default still overwrites, which the wrapper's
  deliberate internal write needs). `tradier_fields.write_report` callers are a
  separate function, unchanged.
- **B-4.** Document the observed `updated-at` behaviour (T1).
- **A-O2/N1.** `close_jobs.attempt` publishes `close_job.last_attempt` from the job
  read after its commit, which can be a later worker's attempt; `evaluated_at` is
  read after the bar fetch, while earnings evidence may be requested after it. The
  attempt must report its own chronology. Job history and behaviour unchanged.
- **D O7.** Correct the focused count with the exact command.
- **Dispositions** of D O3–O6, B-5/B-6, A-O3, N2: keep safe restrictions, add no
  tolerance and no invisible cross-application accounting.

### Implementation and results

- B-1: `BoundedData` wraps its transport and appends one bracket per wire call
  (`started_at`, `returned_at`, `elapsed_ms` from the monotonic clock,
  `throttle_and_signing_ms`, `outcome` RETURNED/RAISED). Each snapshot group and
  row carries `transport_bracket` (`MEASURED` for exactly one call, otherwise
  `NOT_SINGLE_REQUEST` or `NOT_RECORDED`) with its basis text. The whole call is
  `workload_started_at`/`workload_elapsed_ms` with `workload_note`;
  `request_started_at` and `request_elapsed_ms` are gone from the Webull report
  (no code consumer; Checked by grep). `received_at`, used for source-time ages, is
  unchanged. The tastytrade REST snapshot and Tradier brackets contain no throttle
  sleep (Checked by reading `snapshot_quote_check.observe` and
  `tradier_option_check.quotes`), so they are unchanged.
- B-2/B-3: `webull_quote_check.validate` and `snapshot_quote_check.validate` are
  shared by the diagnostic and the CLI; the CLIs also check the request budget and
  `os.path.lexists(--output)` first. `quote_measure.write_new_report` writes with
  `open("x")`, removes only a partial file it created, and returns the
  credential-checked report with `output_write` on failure.
- B-4: `TASTYTRADE_QUOTES.md` REST section now carries the 2026-10-05 observation
  (Checked) and says `updated-at` is not a side-change time and its age alone does
  not show bid/ask freshness.
- A-O2/N1: `rec.discovery["this_attempt"]` gives this attempt's `attempt`,
  `committed`, `started_at`, `evaluated_at` with `evaluated_at_basis`, `final_at`
  (the clock read under the publication lock) and `status` (`CLAIM_LOST` when its
  results were discarded); `close_job.last_attempt_is_this_attempt` says whether the
  job's last history row is this attempt. The persisted history row is unchanged.
- D O7: corrected in `checkpoints/G5a-afterhours-four-loops-2026-10-08.md` and
  `G5_ACCEPTANCE.md`; the evidence JSON is left as the developer's record.

| Item | Disposition |
| --- | --- |
| D O3 CLI refresh skips the ledger | Kept; now documented in `TRADIER_TICKET_QUOTES.md` as an invisible same-store user (one GET per process). No cross-process accounting added. |
| D O4 no margin at 60 s | Kept; documented. A 429 fails closed to STOP. Adding a margin would be a new tolerance (Taz/Astra decision). |
| D O5 clock read before the lock | Kept; two concurrent clients can refuse one refresh with the backwards-clock check. Fails closed; window is microseconds (Inferred). |
| D O6 same path, new store object | Kept; over-conservative only. |
| B-5 `equity=` comma form | Kept; documented. The comma form answered on 2026-10-05; the documented array form is untested. |
| B-6 NaN body code | Kept; documented. Refused as `INVALID_JSON`; fails closed. |
| A-O3 `SOURCE_SESSION_NO_LONGER_LATEST` | Kept as a defensive check; effectively unreachable before `TARGET_SESSION_ENDED`, harmless. |
| N2 backwards wall-clock step | Kept; refused and retried after the spacing. A one-off `ValueError`/`SignalStateError` in the scan log after an NTP step is this, not a data fault. |

Other writers found by the B-3 inventory that still replace an existing output:
`quote_check`, `quote_measure clock/volume` and `option_conventions` CLIs, and
`tradier_option_check` between its own existence check and its write (a race
window only). Not changed here (outside B-3's named paths); the host commands use a
fresh `mktemp -d` folder.

Results: `tests/test_diagnostic_integrity.py` (26) plus the focused Webull, snapshot,
close-job, close-recovery, publication-chronology, quote-measure and scanner suites
pass. On c41142e the new file gives 24 failed, 2 passed (no collection error); the two
passes are the snapshot controls (`INVALID_REST_CONFIG` and `CREDENTIALS_MISSING`
were already reported before credentials). One existing assertion was renamed with
the field (`request_elapsed_ms` -> `workload_elapsed_ms`). Mutations: 16/16 caught;
the first run let "exit 0 although the write failed" survive, so a test for it was
added and both CLIs' versions of that mutation were then caught.

# Re-audit repairs at 283c032 (Astra F1–F3, 2026-10-10)

2026-10-11. Implementer: Claude, authorized by Taz forwarding
`CLAUDE_G5_R2_R3_REAUDIT_REPAIR_PROMPT_2026-10-10.md` with Astra's re-audit
(R1–R5 NOT APPROVED as a set). Base `283c03237e343e86d0d134180a77e30a14f28027` on
`codex/repair-step-01-baseline` (origin had not advanced). Same hierarchy and
exclusions as above; G5 stays open. Offline only: no provider call, credential
read or host command. Astra's probe file `test_astra_r2_r3_followup.py` is kept
unchanged in the handoff; on 283c032 it gives 4 failed (F1 approve and consume,
F2 `[]` and `{}`), 2 passed (clean-restart control, real setup tool hidden input).

## R2b — a restart must not revive an old ticket (Astra F1)

### Attempt lifecycle and recovery mechanism (recorded before implementation)

**Checked (Astra's probe, reproduced on 283c032):** a refresh writes a durable
`START`; the store then stays locked, so neither the outcome nor the withdrawal
can be written and the withdrawal is held only in `_withdrawn`. After a restart
the `START` keeps the name unavailable, as intended. The next healthy refresh
writes a newer `START` and `OK`; `read()` then accepts it, and because the
generation is `digest([instance, stop_id, latest FAILED id])` and none of those
changed, the old prepared version approves and the old approval consumes.

Attempt lifecycle, per symbol and environment (all rows append-only):

1. `begin()` writes `START` (key `env|symbol`). A failure here sends nothing and
   writes nothing; the earlier evidence stands (unchanged).
2. The request is sent once.
3. The attempt ends in one of three ways:
   - **terminal:** `finish()` (or a durable `_withdraw`) writes one `OK` or
     `FAILED` per component (`env|symbol|trade`, `env|symbol|quote`) with
     `start_id` = that `START`;
   - **provider STOP:** a global `STOP` row (`env|*`), components left open;
   - **no outcome:** the process could not write (store still locked) or died.
4. `read()` accepts a component only when its latest `OK` (by `start_id`) is
   newer than every `FAILED`, the last `STOP`, and every `START` for the symbol.

Recovery mechanism (the smallest durable change; no new row type, no writes on
another attempt's behalf):

- A `START` is **overtaken without an outcome** for a component when an `OK` for
  that component from a later `START` was written before any `OK`/`FAILED` for
  this `START`. This is computed from the stored rows at every `read()`, so it
  survives a reopen and is evaluated inside the final approve/consume fences.
  Because rows are only appended, a `START` once overtaken stays overtaken.
- When at least one `START` is overtaken, `read()` adds `(count, newest id)` of
  those `START`s to the generation. A ticket made before the unfinished check
  therefore no longer matches after recovery; a ticket made after recovery keeps
  matching until another check is overtaken. Stores with no overtaken `START`
  keep exactly the old generation, so upgrading invalidates nothing healthy.
- Ordinary refreshes run in order (each `START` gets its outcome before the next
  `START`), so they never count and never change the generation by themselves.
- Active overlap vs. abandonment: they are not told apart when the newer `OK`
  lands, and nothing is inferred from age. In both cases the older check's
  outcome is unknown, so the generation changes once (fail-closed). Afterwards
  they differ only by what the older attempt itself writes: a late `OK` changes
  nothing (the newer `OK` stays latest, the count is fixed); a late `FAILED` makes
  the component unavailable until a fresh check (the existing rule); an
  abandoned attempt writes nothing. No process marks another process's check
  failed. Cost: two overlapping refreshes of the same symbol invalidate tickets
  prepared before them once.
- `_withdrawn` stays as the in-process immediate refusal; it is no longer what
  protects old tickets across a restart.

Provider STOP that cannot be recorded (the restart bound): when the `STOP` row
cannot be written, the store writes a STOP file next to the database
(`<store>.stop-<environment>.<random>`, mode 0600, fsync) holding only the fixed
code. `begin()` and `read()` refuse with `TRADIER_STOPPED_EXPLICIT_RESUME_REQUIRED`
while such a file exists, in any process. The next writer that gets the store
(`begin` or `resume`) copies it into a `STOP` row, commits, then removes that
exact file; `resume` therefore always records the `STOP` before its `RESUME`.
Only if the STOP file cannot be written either is the STOP held in this process
alone; that remaining bound is documented, not called durable: a restarted
process sees the open `START` (no ticket can use the name) but may send one more
request before it meets the provider refusal again.

Unchanged: local codes never become a provider STOP; a reservation failure sends
nothing; real provider STOP/resume; healthy peers per component, environment
and symbol; concurrent ordering, the timestamp recheck, mapping binding and the
final fence; no deletion or rewriting of events.

Acceptance examples: Astra's approve and consume regressions raise
`TicketError` after the restart and a healthy refresh; a ticket prepared after
recovery approves and consumes; a clean reopen still approves the healthy
ticket; a restarted process with an unwritten provider STOP sends nothing.
Rollback: revert the R2b commit; the 283c032 restart behaviour returns.

### Implementation and results

- `tradier_quotes.read` adds `(count, newest id)` of overtaken `START`s for the
  component to the generation, only when there are any (one indexed query; a new
  additive index `quote_events_start` on `start_id`, schema text unchanged). The
  no-marker return keeps the literal `digest([instance, stop_id, failure])`, so the
  committed `tools/check_tradier_quote_guards.py` failure-epoch case still applies
  (Checked: 7/7 at this commit; a first draft that rewrote the expression made the
  tool stop with an assertion before it was committed).
- `fetch`'s provider branch calls `_hold_stop` when the STOP row cannot be written:
  a STOP file (`O_EXCL`, 0600, fsync of file and folder) holding only a fixed code,
  falling back to the process-held STOP only if the file cannot be written.
  `read` refuses while a STOP file is unrecorded; `_persist_held` (called by
  `begin` and `resume`) records each file once (by its own name in the STOP row,
  so a second copier skips it; a file with any other text is recorded as
  `TRADIER_FETCH_FAILURE`), commits, then removes it. `STOP_CODES` is now a module
  constant (same members).
- `docs/TRADIER_TICKET_QUOTES.md` documents both; `G5_ACCEPTANCE.md` has the row.
- Tests: `tests/test_tradier_restart_invalidation.py` (13) and
  `tests/test_astra_reaudit_regressions.py` (Astra's R2 regressions and
  clean-restart control, assertions unchanged). On 283c032 the new file gives
  8 failed, 5 passed; the passes are the controls (new ticket after recovery,
  clean restart and ordinary refreshes, late old failure, append-only history,
  and the documented bound when neither the STOP row nor its file can be written,
  which behaves the same before and after). Astra's two R2 regressions fail on
  283c032 (`DID NOT RAISE`) and pass now. Tradier, ticket, quote, after-hours and
  risk suites: 604 passed. Mutations: 10/10 caught (one survived the first run, a
  STOP file whose own text was recorded unchecked; a test was added and it is caught).
- Limits: two overlapping refreshes of one symbol invalidate tickets prepared
  before them once (fail-closed). The process-only STOP bound above remains when
  the folder itself cannot be written. Rollback: revert this commit.

## R3b — saved identity symbols are validated before grouping (Astra F2)

### Requirement

**Checked (Astra's probe, reproduced on 283c032):** `saved_greek_audit.audit`
checks only that identity rows are objects, then groups them by `desk_symbol`
outside the record handler. `[]` or `{}` there raises `TypeError` (the command
prints a traceback). Other bad values did not crash but were worse: a row whose
symbol is `""`, `None`, missing, a number or `True` was simply never matched, and
the capture could still be reported `OBSERVATIONS_AUDITED` (Checked on 283c032 by
the new tests: 22 such cases audited without refusal).

Contract: a saved identity's `desk_symbol` must be printable text with no
whitespace before grouping. Otherwise the row cannot be attributed to one symbol
and could contradict any observation, so the whole capture is refused
(`CAPTURE_SHAPE_INVALID`, no observations audited) with one issue per row at path
`identity_capture[n].desk_symbol` and a fixed code: `IDENTITY_SYMBOL_MISSING`
(missing or null), `IDENTITY_SYMBOL_NOT_TEXT` (any non-text JSON value) or
`IDENTITY_SYMBOL_INVALID_TEXT` (empty, whitespace or non-printable). No
stringifying, no discard, no catch-all. Rows that name a valid symbol but carry
other bad fields stay attributable and fail only that symbol's observations, as
before; bad records stay isolated. The input SHA-256, `provider_calls=0`,
`live_state=NOT_RESTORED`, `HISTORICAL_OBSERVATION` labels and Greek unit
behaviour are unchanged. The command writes the JSON report and exits 1.

### Implementation and results

- `saved_greek_audit.shape_issues` checks each identity row with `symbol_issue`.
- Tests: `tests/test_saved_identity_symbols.py` (39) and Astra's two R3
  regressions (assertions unchanged) in `tests/test_astra_reaudit_regressions.py`.
  On 283c032 the new file gives 30 failed, 9 passed (controls: healthy,
  conflicting identities, other bad identity fields, bad record symbols).
  Offline-evidence and after-hours suites pass. Mutations: 6/6 caught.
- Not changed (noted for Astra): `tradier_readiness.prepare` does not crash on the
  same values (Checked: `[]`, `{}`, null, `True`, `5`, `""`); a Webull identity row
  with an unusable symbol matches no symbol, so the symbols it might belong to are
  withheld as missing identity, but other symbols still get non-installable
  drafts. Making such a row withhold every draft would follow the same principle;
  it is outside F2's named path, so it is left for a decision.
- Rollback: revert this commit.
