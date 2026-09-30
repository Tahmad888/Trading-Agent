# Checkpoint 02 — user-selected dollar risk

Status: complete locally. Base: `5caa07d`. Trader-day functions: plan, approve.
Implementer: Codex. Review: self-review and behavior tests; no independent reviewer yet.

## Requirement and impact record (before implementation)

User decision D02: Taz supplies the risk budget; no hard $100 ceiling and no grade-to-dollar
mapping. Latest instruction authorizes sequential advancement after each step is verified.
Upstream branches were checked with `git ls-remote`; both still match Step 01's pinned heads.

Integrate PR #1's existing risk work locally, preserving the newer scanner history.
Affected contracts/producers: TradeProposal, fixtures and future ticket construction.
Affected consumers: risk evaluation, RiskDecision, risk tests and developer instructions.
No runtime UI or broker order path exists yet; final human approval is implemented in Step 13.

Acceptance: chosen budgets above $100 are honored; finite positive budget required;
grades do not alter size; round down to whole strategy units; never grow a proposal;
stop-loss estimate plus estimated costs stays within budget; retained account/quote/
tradability/liquidity/buying-power checks still reject their invalid cases.

Cost convention: `max_loss_usd` is gross estimated price loss at the stop for the full
proposal. `est_costs_usd` reserves the full proposal's estimated round-trip fees/spread/
slippage, including when quantity shrinks, until costs are explicitly recomputed.
This conservative implementation convention prevents a reduction from silently excluding
fixed fees. These are estimates, not a guarantee of realized loss. Return the budget,
final gross stop estimate and reserved costs separately for future ticket display.

Known interaction: minimum-leg-quantity rounding can produce fractional legs for unequal
quantities. Whole-unit sizing must be corrected here to satisfy this step's quantity
criterion; complete option-structure validation remains Step 04.

Rollback: revert this step's integration commit to its first parent, preserving later
work. No stored production state or live service is migrated. Missing/invalid risk inputs
produce no approval, never a substitute default budget.

## Verification and results

- Focused existing risk suites: 40 passed after integration and behavior changes.
- Full combined suite: 147 passed. An expected serializer warning from an intentionally
  malformed copy was then handled at the revalidation boundary and the suite rerun
  with warnings treated as errors: `.venv/bin/python -m pytest -q -W error`
  finished with **147 passed in 0.76s**, no warnings. `git diff --check` passed.
- Independent quantity oracle enumerates affordable whole shares across 96 combinations
  of proposal size, per-share stop loss, costs and selected budget.
- Manual deterministic scenario: $100 / $150 / $500 selected budgets produce 1 / 2 / 3
  contracts respectively from a three-contract proposal with $60 gross risk per contract
  and zero fixture costs. This verifies removal of the old ceiling and no position growth.
- Adversarial cases cover missing/nonfinite/negative/string/bool budgets, invalid costs,
  grade independence, costs at the exact budget boundary and unequal-leg integer sizing.
- Existing dollar loss halts, stale/future quotes, tradability, buying power, margin,
  open interest, DTE and live/paper tier checks remain exercised by the combined suite.
- No runtime references to the removed ceiling field or grade-dollar functions remain.

## Changed files and limitations

PR #1's seven affected files are integrated. `tests/test_user_risk.py` adds behavioral
acceptance and the enumerated oracle. README, the plan and this record document the new
policy. The CLAUDE.md merge conflict was resolved preserving both the repair instructions
and the latest policy, rather than selecting an entire side.

This step validates proposal inputs and sizing arithmetic, not the provenance or
accuracy of caller-supplied stop/maximum-loss estimates; Step 04 addresses option
identity and independent contractual loss calculations. Collateral still uses the
legacy worst-case estimate until that step. Account-state integrity/persistent halts
and regime application belong to Step 05. No broker orders, deployment or remote push.

- [x] PR #1 integrated with latest working-branch scanner history preserved.
- [x] No fixed per-trade ceiling or grade-dollar sizing remains.
- [x] User budget required; costs reserved; whole-unit quantities returned explicitly.
- [x] Positive, negative, boundary and interaction checks verified.
- [x] Superseded tests replaced with the newly approved behavior, retained checks still pass.
- [x] Documentation and checkpoint updated. Next: Step 03 under sequential authorization.
