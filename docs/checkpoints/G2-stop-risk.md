# G2 — event stops and independently computed sizing

2026-10-01. Base d2cf63b; upstream checked and matches. Implemented and locally verified (results below).
Serves watch, plan and eventual approve. No orders or runner activation.

## Before-code impact and evidence

Checked: breakout/EP detectors substitute ADR distance for the card's day low;
risk sizing trusts proposal.max_loss_usd. User policy: selected option quantity
must remain reviewable with full exposure, not an imposed maximum-loss budget.
Sourced: the existing Kullamägi cards specify day low with ADR width limits.
FINRA explains that a stop trigger is not a guaranteed fill and stop-limit orders
may not execute:
https://www.finra.org/investors/insights/stop-orders-factors-consider-during-volatile-markets
Existing sizing/option references are in ../GAP_REPAIR_PLAN.md. No profitability
claim or claim that all retail traders use this implementation.

Engineering assumption: this scanner observes completed M15 bars. Bind the
cumulative session low through the trigger observation, not a future full-day low.
This is a decision after that bar, not an asserted fill within it. ADR% width uses
actual entry as denominator and the already-frozen completed-daily ADR%; retain
1x breakout / 1.5x EP caps. Freeze that stop for this event; subsequent touch
invalidates it rather than widening it. Incomplete candidates have no stop yet.

Affected: detector Signal, card version, scanner observations/revalidation,
SQLite event payload and migration, proposal/decision schema, trusted risk-source
adapter, risk sizing, fixtures and lifecycle/integration tests. Legacy proposals
and old card events must rebuild, not receive invented terms. Unrelated fixed
structural stops retain their behavior.

Risk will resolve an event reference through an independently injected source
which revalidates current bars, quote and required fundamentals. Proposal stop
and loss fields are assertions, never authorities. Shares use executable limit
minus event stop and the full declared cost reserve. Short arithmetic is tested
without enabling short-share execution. Options support explicit selected-quantity,
maximum-loss-budget and conditional stop-budget modes; the latter needs independent
option-exit terms. An underlying stop alone yields unavailable option-dollar loss.
Exposure and hypothetical stop-fill arithmetic have separate labels. Exact-term
fingerprints prepare for G4; they are not approval tokens or authorization.

Acceptance: actual low/cumulative low/width boundary, no future low, frozen stop,
restart/legacy/revisions, forged or missing event/stop, stale evidence, wrong-side
and chased executable limits, exact rounding and costs, independent option exit
arithmetic, unknown option estimate with selected quantity, over-budget warnings,
term edits changing fingerprint, deterministic scanner-to-risk positive path.
Run focused tests then strict combined suites on Python 3.12 and 3.14.

Rollback: revert G2 commit, retain SQLite data and rebuild candidates. The additive
event column retains history. Do not run older code on the migrated database without
restoring its pre-G2 backup. G3 action coverage and G4 actual approval enforcement
remain separate. No broker/provider or actual-host acceptance implied.

## Implemented and verified

Completed locally on 2026-10-01. Detectors now arm without a fabricated stop;
scanner/store bind the observed low with full source precision to each event.
The original chase reference is preserved. Changed card fingerprints and an
additive event-terms migration prevent legacy events from passing as repaired.
Revalidation uses the original candidate identity and the frozen event stop.

Schema 3 requires an event reference, stop assertion and explicit sizing mode.
Risk resolves fresh evidence independently, checks target/direction/chase/width,
computes share stop loss from the actual limit, and distinguishes optional option
stop arithmetic from full strategy exposure. Selected quantity can carry budget
warnings. A false caller loss claim is rejected, including the reported $0.20
MSFT stop example. No order can be authorized by a RiskDecision. Exact-term hashes
and evidence-bounded warning expiry prepare G4 but do not implement its approval.
See ../RISK_TERMS.md for contracts and migration limitations.

Verification:
- 53 focused G2 acceptance cases passed, including scanner -> persisted event ->
  revalidation -> risk, plus fresh-stop invalidation, EPS/catalyst prerequisites,
  unavailable prices, legacy events, precision, short arithmetic, option choices,
  forged terms and changed quantity/entry/stop/target fingerprints.
- `.venv/bin/python -m pytest -q -W error`: **856 passed in 4.71s** (Python 3.12).
- `../verify-python314/bin/python -m pytest -q -W error`: **856 passed in 4.72s**
  (Python 3.14).
- The first 3.14 run found two unclosed SQLite connections in the new migration
  test. Added explicit connection closure; both complete strict suites then passed.
- `git diff --check`: passed. Self-reviewed producer/consumer diffs. No external
  implementation review, live provider request or iMac G2 acceptance performed.

Remaining: G3 automatic corporate-action coverage/revisions, G4 actual approval
binding and warning/blueprint reconciliation, G5 integrated acceptance, then
unfinished Step 09. Step 10 volume/other detector work remains outside G2.
No runner, signals-to-orders pipeline, orders or operational-day count activated.
