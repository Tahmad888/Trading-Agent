# G5a CP3 — close-recovery audit repairs (F1/F2)

2026-10-05. Implementer: Astra, authorized by Taz's explicit "Go ahead" after
the independent audit of `c9cf026ef1f43081bc112a64c34676686601a669`.
Hierarchy: G5 → G5a parent Checkpoint 3 → children 1–6. This is a bounded repair
of live-run Package 2. Step 09, activation, host changes and orders remain paused.

## Requirements recorded before implementation

**Checked F1:** the independent production-path probes reproduced a 09:59 attempt
completing at 10:01 with a 09:59 observation floor, admitting a 10:00 crossing.
A pre-open attempt finishing after the open admitted the same crossing with no
floor. **Checked F2:** explicit provider resume left MOVR in PROVIDER_STOP; the
next attempt fetched only SPY/QQQ and finished without evaluating MOVR.
Audit/probe files are retained outside the repo in Taz's Codex workspace.

These are chronology and state-transition defects, not trading-policy choices.
No price/volume threshold, dollar cap, quote policy or new tolerance is introduced.
SQLite's exclusive write transaction must precede the final publication clock;
requests remain outside it. BEGIN EXCLUSIVE also moves existing reader waits
before that clock in rollback-journal mode; in WAL it behaves like IMMEDIATE.
Primary engineering reference: https://www.sqlite.org/lang_transaction.html.

### F1 contract and affected pipeline

Producer: `close_scan` fetches and evaluates data, recording its evaluation time.
Consumer: `close_jobs.attempt` → `SignalStore.commit_close_job` → target armed list,
publication history and `armed_floors` → `SignalStore.observe` → normal intraday scan.

- Take a fresh final decision clock after acquiring the SQLite exclusive transaction
  and checking the claim token. Production supplies its existing decision-clock
  callback; deterministic replays without a callback use their explicit clock.
- Refuse a final clock earlier than the evaluation clock. Keep invocation-start,
  evaluation and publication times distinct in the attempt record.
- Recheck the frozen target deadline and latest completed source session before
  publication. Expiry records a terminal job outcome without publishing the
  attempt's candidates or replacing already committed candidates.
- New intraday candidates receive a floor at least as late as final publication;
  candidates already committed keep their existing floor. Pending retry spacing
  starts at final publication, not at a stale invocation clock.
- Keep the existing token/lease takeover contract: a superseded token cannot
  publish. This repair does not create a new maximum provider-call duration.

Acceptance: two-minute and pre-open delays reject earlier crossings; a later
crossing passes; before-open preparation still permits the next session's valid
crossing; real SQLite writer/reader waits precede the final clock; final target expiry
and backwards clock publish nothing; prepared peers survive partial recovery.

### F2 contract and affected pipeline

Producer: an operator explicitly calls `resume_close_job` on a BLOCKED job.
Consumer: persisted per-ticker outcomes → `close_jobs.attempt` retry selection.

- Requeue only PROVIDER_STOP outcomes as RETRY_AFTER_OPERATOR_RESUME, atomically
  with clearing the block. Preserve their prior outcomes in attempt history.
- Prepared peers, invalid-history/identity refusals and removals are untouched.
- Resume makes no request. The next ordinary bounded invocation evaluates the
  resumed ticker; another denial/rate limit blocks again. No automatic quota reset
  or background retry is inferred.

Acceptance: 401/403/429 stops followed by explicit resume really refetch the
affected name and update its outcome; continuing failures block again; state
survives reopen; no automatic retry occurs while blocked; removed names remain
removed; market-reference failures recover; committed healthy peers survive.

### Limits and rollback

Only the two recovery paths, regression tests and their documentation change.
No schema migration is required: the new outcome fits the existing JSON field.
Earlier code does not know RETRY_AFTER_OPERATOR_RESUME; after rollback, an affected
job needs an explicit recovery migration before that older code can retry it.
Do not silently reinterpret that outcome as PREPARED. Revert this repair commit
to undo the code; retain evidence and already committed candidate history.

`e237309` already has Astra's bounded child-3 identity-ordering sign-off. Its stale
"unreviewed" labels are corrected as part of this handoff. These repairs still
need independent review; implementation and offline tests do not close G5.

## Implementation and verification

Changed production files: `close_jobs.py` and `signal_state.py` only. The store's
publication transaction takes BEGIN EXCLUSIVE, then reads the final callback
clock, refuses backwards clocks, rechecks the frozen session deadline and sets
new-candidate floors. Retry spacing and the attempt report use that final clock.
The attempt record retains `started_at` and `evaluated_at`. A final expiry keeps
committed peers, records discarded prepared names and emits no new armed payload.
Claim-token fencing and the existing lease-takeover behavior are unchanged.

Explicit resume atomically changes only PROVIDER_STOP outcomes to
RETRY_AFTER_OPERATOR_RESUME and stores `previous_provider_stops` in history.
The next bounded attempt selects those names. Another 401/403/429 blocks again;
healthy peers, removals and integrity refusals are not requeued.

New regression file: `tests/test_close_recovery_audit.py`, 22 collected cases.
No tests contact providers. Verification used Python 3.12.14 and the existing
development/quotes environment, with `PYTHONDONTWRITEBYTECODE=1`, explicit
`PYTHONPATH=src:.`, strict warnings, and pytest's cache provider disabled.

| Verification | Actual result |
| --- | --- |
| New 22 cases against untouched `c9cf026`, in a temporary archive checkout | 18 failed, 4 legitimate controls passed |
| New cases + existing `test_close_jobs.py` | 45 passed (6.81 s) |
| Original independent probes, unchanged | Both delayed-publication cases now emit no earlier trigger; before-open control still triggers; resumed MOVR requested and PREPARED on attempt 2, coverage FULL |
| Seven focused mutations, each in a temporary copy | All caught; failure counts 7 / 2 / 1 / 4 / 8 / 8 / 1 |
| Combined strict suite | **1,766 passed in 201.19 s**, Python 3.12.14; code/tests unchanged afterward |

Mutations removed, independently: the fresh final clock, final expiry refusal,
EXCLUSIVE reader protection, final floor, resume requeue, resumed-name selection,
and committed-peer floor preservation. None modified the working checkout.
External evidence logs/probes are retained in Taz's Codex workspace under
`G5_CLOSE_RECOVERY_*_2026-10-05`; no provider or host acceptance is inferred.

Self-review and regression verification are not independent review of this
implementation. Claude can re-audit the repair after his reset. The prior
`e237309` sign-off stays accepted; this new repair, bounded provider checks,
current-commit iMac work and remaining G5 acceptance stay separate.

`git diff --check` passed. This implementation made no provider calls, iMac
changes, schedules or orders. Python 3.13/3.14 results are not claimed here.
