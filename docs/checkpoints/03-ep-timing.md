# Checkpoint 03 — EP entries without an 11:00 condition

Status: complete locally. Base: `e367d7c`. Trader-day function: watch.
Implementer: Codex; no independent review claimed.

## Requirement and impact record (before implementation)

D03 and Taz's explicit reply: either completed 15-minute or 60-minute opening-range
breakout can qualify throughout day one, subject to other checks. No 11:00 cutoff or
clock-driven range switch. Evaluate both alternatives, return one result; choose the
earliest observed breakout, with the 15-minute range breaking ties. Do not emit two
entries for simultaneous alternatives. Full cross-scan event deduplication remains Step 07.

Targets: scanner entry evaluation, EP card/trigger description, frozen card hash,
scanner tests and timing acceptance tests. Existing EP `as_of` is the prior daily
reference bar; day one is its next trading session, not an arbitrary later day.

Completed M15 bars only: a bar stamped at its start cannot contribute until 15 minutes
later. Opening ranges start at 09:30 ET; extended-hours bars cannot become that range.
Require the latest expected completed M15 bar and contiguous regular-session coverage
before declaring a historical crossing observed on valid current data. This localized
protection is necessary for the timing repair; Step 06 generalizes provider/calendar
semantics, including early closes. No claim of live-feed timestamp verification here.

Acceptance: morning, 11:00 and afternoon opportunities; both range alternatives;
no unfinished/future-bar influence; no equality-only breakout; missing opening or
hour constituents, stale data, extended-hours contamination, next-day reuse and
duplicate scan alternatives. Genuine structural/earnings/chase eligibility remains
owned by later repairs; this function detects a crossing, not permission to place an order.

Rollback: revert this step; card fingerprint and behavior must roll back together.
No new persisted signal schema. Plan B on missing bars: record the data error, no trigger.

## Verification and results

- Focused scanner/card/timing checks: **38 passed in 0.97s**.
- Full suite: `.venv/bin/python -m pytest -q -W error` — **169 passed in 1.31s**,
  no warnings. Whitespace check passed and obsolete cutoff/switch references were searched.
- New tests place the first crossing at 10:00, 10:45, 11:00, 11:15, 14:00 and 15:45 ET.
- Mutating all forming/future highs leaves the earlier result unchanged. The crossing
  becomes visible only when its bar completes.
- Missing/duplicate/out-of-order/misaligned data fails closed, including through the
  scanner's skipped-symbol logging. Extended-hours highs cannot alter either range.
- Equal-to-range prices do not count as a breakout. Previous-session EPs cannot be
  reused on the next day. Weekend-to-Monday day-one handling is covered.
- Both alternatives crossing yields one result with the earliest observed crossing;
  the log reports all observed alternatives. This is not cross-scan deduplication.
- The EP card was re-frozen under Taz's explicit approval; the other nine hashes remain unchanged.

## Remaining limits

This step repairs timing and local M15 completeness/freshness. It does not certify
live provider timestamps, exchange early closes (Step 06), lifecycle/approval validity
(Step 07), or the still-missing earnings/structural-stop/chase repairs (Steps 09–10).
The returned crossing is a scanner observation, not an executable trade approval.
No broker orders, live-data requests, deployment or remote push occurred.

- [x] Exact opening-range behavior resolved by user before implementation.
- [x] 11:00 cutoff and switch removed from runtime and descriptions.
- [x] Completed ranges and session-day restrictions verified.
- [x] Positive/negative/boundary/integration tests pass; source diff self-reviewed.
- [x] Runtime, card, fingerprint, tests and documentation agree.

Next: Step 04. Options scope decision was requested while Step 03 was verified.
