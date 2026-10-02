# G5 — combined verification of G1–G4 and G3a

2026-10-02. Implementer: Claude (cloud container). Requested by Taz ("Lets continue
G5", relayed 07:10Z). **Astra's audit of G4 is still pending**; nothing here is
Astra's approval, and this self-check does not replace it.

## Before-code record

Requirement (GAP_REPAIR_PLAN G5): verify the combined repairs, resolve review
findings, and keep the Step 09 remainders and actual-host acceptance tracked. No
Step 09 implementation, no Step 10, no operational-day count. Trader-day step:
approve (the combined path from watch/analyze evidence to a single-use ticket).

Base: the remote repair branch was `d1e4514` (G4). Astra had pushed one G3 follow-up,
`da33b59` ("G3 follow-up: rebuild revised setups and add bounded batch action
evidence"), on `codex/g3-revision-followup`, built directly on `d1e4514` and offered
for integration (`checkpoints/G3a-targeted-rebuild.md`). G5 builds on `da33b59`, so
pushing to `codex/repair-step-01-baseline` is a fast-forward: no merge commit, no
rebase, no force. Combined baseline before G5 edits: **975 passed** strict on
Python 3.12.3 and 3.13.14 (cloud).

Scope: one synthetic end-to-end path through all five repairs, a review of G3a, and
a findings list with owners. No provider requests, no source/risk/approval logic
changes unless a finding requires one (none did).

## Integration evidence

`tests/test_g5_integration.py` drives real components together: `scanner_source`
(G1 wrapper), `close_scan`/`scanner.run` and `SignalStore` (G2 event stop, G3a queue),
`VendorBasisSource` with `BatchActions` (G3/G3a), `EventRiskSource`, `risk.evaluate`,
`RiskStateStore` and `TicketStore` (G4). Bars, the action feed, quotes and clocks are
synthetic; approvals use the automated fixture actor, never Taz.

1. `test_combined_repairs_from_broken_earnings_to_single_use_ticket_and_revision`
   - G1: a malformed earnings policy yields `UnavailableEarningsSource`; prices still
     arm the breakout; EP evidence is `PENDING_EVIDENCE`, breakout `NOT_REQUIRED`.
   - G2: the candidate has no ADR-distance stop (`stop_basis` session_low); the event
     freezes the observed session low (146.5).
   - G3: the candidate carries `webull-history-v1` price evidence; no enrolment.
   - G4: the ticket defaults to `stop_budget`, sizes floor((budget − costs) / (cents
     limit − event stop)), and is fixture-approved.
   - G3a: an ordinary cash dividend then revises daily history (daily-only adjustment,
     raw intraday anchor unchanged, batch evidence of the cash amount). Consuming the
     approved ticket inside the event window is **refused** ("Recheck failed"); the
     recheck invalidates the event and queues the rebuild. The approval stays unused.
   - The next ordinary scan reports `REBUILT` without triggering; the old crossing is
     not replayed. A later crossing creates a new event with stop 146.5 × 0.9975.
   - The old approval still cannot be consumed. The new event needs its own ticket;
     consuming before approval is refused; after fixture approval it is consumed once,
     `order_submitted: false`, `broker_action: none`.
2. `test_unexplained_ex_dividend_revision_blocks_the_approved_ticket_without_rebuild`
   - The same revision without action evidence (the G3a default) refuses the approved
     ticket, leaves the event ineligible, and the next scan reports
     `DAILY_RAW_CLOSE_MISMATCH` with no rebuild and no trigger. The ticket is never
     consumed.

## Review of G3a (`da33b59`), by Claude

Read: `revision_rebuild.py`, `batch_actions.py`, the `signal_state`, `scanner`,
`vendor_basis` and `data_basis` diffs, both new test files, `G3_FOLLOWUP.md` and the
G3a checkpoint. G3a changes no risk, approval or setup-card threshold; the G4 tests
pass unchanged on top of it. Credentials are sent only as a bearer header, provider
`apikey` parameters are dropped, responses echoing the key are refused, and errors are
fixed codes. Rebuilds re-run the real detector, never rescale saved levels, and only
a subsequent crossing triggers.

## Findings

| # | Finding | Status | Owner |
| --- | --- | --- | --- |
| F1 | Test fixture `RevisedCharts` scaled integer-typed M15 prices in place; with exactly two bars (10:00–10:14 ET) pandas raised `LossySetitemError`, surfacing as "independent evidence unavailable (TypeError)" instead of the revision. Test-only; production fails closed either way. | **Resolved in G5**: prices cast to float before scaling (`tests/test_revision_rebuild.py`). | Claude |
| F2 | Does G4's consumption recheck see G3/G3a revisions? | **Verified**: refused, event invalidated, rebuild queued, approval never transfers (test 1). | Claude |
| F3 | An approval cannot outlive its event window (15 min after the trigger bar, extended by each scan while the breakout holds). Consuming at the next bar boundary with no intervening scan returns "expired", by G4 design. | Recorded, not a defect. Live use depends on the scheduled scan (not activated). | — |
| F4 | `rebuild_pending` omits an outcome when `finish_rebuild` declines (candidate withdrawn during fetch). The queue row closes and nothing is resurrected, but discovery does not report it. | Tracked, low; fail-closed. Not changed in G5. | Astra (G3a author) to decide |
| F5 | Rebuild requests still `PENDING` when the session ends are day-keyed and never retried or closed on later days. Candidates are per day, so no stale signal can arm. | Tracked, low; fail-closed. Not changed in G5. | Astra to decide |
| F6 | G3a volume policy and ex-dividend price pairing need a Massive key and the read-only host probe in `G3_FOLLOWUP.md`; both stay operationally OPEN. | Tracked. | Taz (key/host), Astra (procedure) |
| F7 | G3 actual-host check (`VENDOR_BASIS.md`) not yet run on the iMac. | Tracked. | Taz |
| F8 | Live account, market-filter, contract-metadata and quote adapters for tickets do not exist; the CLI refuses to prepare/approve/consume without them. | Tracked. | Steps 11, 13, 15, 20 |
| F9 | Earnings date and catalyst confirmation on the ticket (rule 4), whole-watchlist earnings queue, source budgets, per-symbol status, Step 09 actual-host acceptance. | Tracked; Step 09 is not started by G5. | Step 09 |
| F10 | Broker submission, idempotency, fills and reconciliation. | Tracked. | Steps 15, 16 |
| F11 | External blueprint still says loss limits halt and carries `$100` wording. | Tracked; not edited here. | "Independent check of v2.3" thread, if Taz asks |
| F12 | Python 3.14: only 3.14.0rc2 is installable here and it fails at import with pydantic 2.13.5 on every base. Astra reported 975 strict on 3.14 for `da33b59` (G4 tests included); the two G5 tests have not run on 3.14. | Tracked. | Codex host run |
| F13 | Astra's audit of G4 (`d1e4514`). | **Pending.** | Astra via Taz |

## Step 09 resumption (not started)

Per CLAUDE.md and `checkpoints/09b-automatic-sources.md`, Step 09 still needs:
automatic reported results and upcoming dates for the whole watchlist (not one
reviewed file), the earnings queue with per-source request budgets and per-symbol
status, real catalyst retrieval and review, refresh integration with the scanner,
earnings date and catalyst confirmation on the G4 ticket, and actual-host acceptance.
Resumption waits for Astra's G4/G5 audit and Taz's go-ahead.

## Verification (cloud container, 2026-10-02)

| Python | G5 + G3a rebuild tests | `python -m pytest -q -W error` |
| --- | --- | --- |
| 3.12.3 | 15 passed | **977 passed** (combined baseline 975) |
| 3.13.14 | 15 passed | **977 passed** |
| 3.14 | not run (see F12) | not run |

`git diff --check` passes. No warnings suppressed. Before pushing, the remote repair
branch was still `d1e4514` and Astra's follow-up branch `da33b59`. These are
engineering tests on synthetic data, not provider, host or trading observations.

Rollback: revert the G5 commit (tests and docs only, plus the fixture cast).
Reverting `da33b59` as well restores the G4 state; G3a's SQLite tables are additive.
