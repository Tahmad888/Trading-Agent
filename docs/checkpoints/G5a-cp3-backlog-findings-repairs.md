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
