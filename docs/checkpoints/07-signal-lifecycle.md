# Step 07 — Persistent signal lifecycle

Status: complete; verified locally on Python 3.12 and 3.14. Base 9741071; upstream matched before edits.
Serves watch, approve and journal. Codex implementing and self-reviewing; no
independent reviewer dispatched. Taz authorized Step 07 and verified-step pushes.

## Impact record before implementation

User policy: no blanket daily trigger cap. A genuinely new qualifying event after
failure/closure may create a new ticket, always requiring fresh approval. Step 06
supplies completed, current bars and action compatibility. No new setup threshold.

Checked in existing code: entry_hit searches all prior bars with .any(); run logs
that historical hit again in later slots. JSON armed lists persist setups but not
trigger identities, failure history or eligibility. Approval/order execution is
not implemented and must remain separate from signal state.

Engineering definitions for this checkpoint (not profitability evidence):
- A candidate is the exact setup/card version, symbol/direction, reference bar,
  entry/stop/target and price evidence, for one entry session. Session close is its
  deadline, including early closes; existing next-session armed-list scope stays.
- Persist each completed observation once. Record bar-end trigger time separately
  from first observation time. Historical replay may record events, but only a
  currently surviving event is returned as newly eligible.
- A close back on the wrong side of the entry invalidates that trigger. A later
  fresh crossing can create a new event; it cannot reuse an old .any() result.
  A stop touch invalidates the candidate, including ambiguous trigger/stop bars;
  a newly evaluated setup is needed. No invented intra-bar order or fill.
- Missing/stale/incompatible data suspends eligibility. Time alone removes fresh
  eligibility at the next expected M15 close and expires events at session close.
  A data correction to consumed bars invalidates the candidate; don't silently
  replay a revised history as a new trade.
- Direction-aware current-price checks use the existing risk-layer 3% move limit
  and any stricter card chase limit; no new per-card calibration. Revalidation is
  a prerequisite API for later approval/execution, not an approval token or order.

Producers/consumers: scanner intraday and near-close observations, Signal numeric
validation, durable signal store, armed-list storage, scan/funnel output and a
fresh-data revalidation entry point. risk.py financial decisions remain unchanged.
Legacy armed JSON may import as candidates only; historical log entries never
become approvals. New SQLite state retains event transitions across restarts.

Acceptance examples: duplicate/restarted scans produce one event; a failed long
and mirrored short cannot remain eligible; later recross creates a different ID;
stop + trigger in one bar rejects; missing bars, action revisions, delayed review,
clock-only expiry and early close reject; new setup evidence can rearm; closing an
event doesn't immediately recycle the same crossing; numeric/unknown-card input
rejects. Include scanner integration, concurrency and rollback/recovery cases.

Plan B: retain history and suspend eligibility on uncertain data/store errors.
Rollback: stop runner, retain signals.sqlite as audit evidence, revert coordinated
scanner/store changes; never infer executable approvals from legacy scan logs.
No activation, authenticated calls, new scheduler, orders or Step 08 in this step.

## Implemented and checked

- `signal_state.py`: SQLite transactions for current armed lists, immutable armed
  revisions, candidates, consumed-bar hashes, trigger events and transition history.
  Unique candidate/event IDs deduplicate concurrent and restarted processing.
- `scanner.py`: runtime `run()` always supplies the durable store; per-bar crossing
  observations replace whole-day hit replay on that path. EP range alternatives
  share an event. Near-close RSI uses the already-fetched validated constituents.
  Missing data suspends eligibility; failed scans suspend current candidates.
- `Signal` stores its evaluated card fingerprint and validates finite positive
  prices/direction/stop ordering. Unversioned legacy JSON remains readable but
  cannot trigger until the setup is evaluated again. Existing JSON is preserved.
- Changed terms cannot recycle a crossing consumed by the prior candidate. Closed
  events need a subsequent reset and fresh crossing; terminal history is retained.
- `revalidate_signal` checks current bars/action basis, event/card state, symbol,
  underlying quote age, directional entry/stop and existing chase limits. A fresh
  quote through the stop invalidates the candidate; a stale quote cannot do so.
  The future approval and order adapters must call this prerequisite; they are not
  implemented here. No approval state is stored in the signal database.
- Wall-clock eligibility expires at the next required M15 observation, independent
  of arriving bars. Entry-session close is terminal. Reads reject time travel into
  a state validated later than the requested clock.
- Scan-log funnel deduplicates event IDs. SQLite remains the authoritative event
  history if a JSONL write fails; future consumers must recover from the store,
  not treat JSONL as a guaranteed delivery queue.

Verification, 2026-10-01, final implementation:

```
.venv/bin/python -m pytest -q -W error
# 564 passed in 2.96s (Python 3.12.14)
../verify-python314/bin/python -m pytest -q -W error
# 564 passed in 3.01s (Python 3.14.6)
git diff --check
# passed
```

56 new cases cover duplicate/restart/concurrency, atomic rollback, failed/repeated
crossings in both directions, stops and ambiguous bars, delayed/stale/missing data,
action evidence failure, revised bars, early close, EP alternatives after 11:00,
RSI same-day scope, Luk/Weinstein entry shapes, changed/unversioned cards, preserved
armed history, time regression, stale/future/wrong-symbol/nonfinite quotes, and
scan-log failure recovery. Existing 508 tests remain passing; card fingerprints,
EP thresholds, financial risk decisions and provider configs were not changed.

During development the combined test caught a SQLite insert placeholder count;
corrected before passing. A new fixture's integer dtype rejected a fractional
price; made its numeric dtype explicit. Self-review then identified and covered
historical-crossing reuse after replacing terms, unversioned-card promotion,
pre-closure reset reuse and backwards-clock reads. No independent review claimed.

## Scope and next checkpoint

This is offline implementation acceptance, not an iMac install or live provider
run. No authenticated calls or runner activation were needed. Daily/M15 evidence
cannot establish tick-by-tick order: trigger_at is the completed bar's end and
observed_at is when the scanner learned it. Stop/trigger ambiguity rejects rather
than inventing fills. Failure/re-entry definitions are engineering policy for
this repair, not validated profitability claims. No per-card threshold was tuned.

Step 07 complete. Next: Step 08 discovery/watchlist coverage. Stop here until Taz
continues. See `docs/SIGNAL_LIFECYCLE.md` for interfaces, migration and recovery.
