# Checkpoint 04 — Instrument identity and loss arithmetic

Status: complete locally. Base: `837d43e`. Trader-day functions: plan, approve, manage.
Implementer: Codex. No independent review claimed.

## Requirement and impact record (before implementation)

User policy: include shares, long calls/puts, debit/credit verticals, long debit
calendars and credit iron condors in the initial scope. Reverse calendars, diagonals,
ratio spreads, adjusted deliverables and naked options are not those structures.
HALF is advisory: it must not silently alter the user's selected dollar budget.
The broader treatment of account halts and NO_NEW_LONGS is awaiting clarification.

The current engine trusts declared maximum loss and leg counts. Replace that with
contract metadata supplied separately by a trusted adapter, OCC identity cross-checks,
structure validation and exact-decimal payoff calculations. Never assume a 100-share
multiplier for an unknown contract. Metadata fields must explicitly establish a
standard US physically settled American option. Unknown/adjusted contracts fail closed.

Impacted: contracts.py, new instruments.py, risk.py, all risk fixtures and tests;
future instrument selector (11), ticket (13), broker adapter (15), management (16).
There are no production proposal writers or saved proposal migrations yet. Version
the proposal contract and require setup version / quote / stop-estimate provenance.
Source timestamps must not imply a live broker check when tests use fixture records.

Compute gross contractual/strategy loss independently; declared loss is only an
assertion to cross-check. Stop risk remains explicitly an estimate (pricing/exit
implementation belongs to 11). Costs remain the full proposal reserve after sizing.
Premium is signed debit/credit. A conservative funding estimate is NOT broker buying
power: expose separate fields and leave broker verification false until an adapter
supplies it in 15. Risk approval here means local eligibility, never order authorization.

For a calendar, debit is the theoretical bound while the long option covers the
short and assignment is managed. Never use a same-expiry payoff calculation for it.
Actual assignment funding, pin risk, fees and failed exits can exceed strategy loss;
management acceptance in 16 remains required, including calendars and condors.

Acceptance: all supported structures; strike/right/expiry/underlying/ratio mismatch;
duplicate/unknown/stale/future/adjusted metadata; independently enumerated payoffs;
understated loss; integer downsizing; premium larger than stop budget; costs and
funding separation. Repair inconsistent old test fixtures instead of bypassing checks.
Metadata freshness of 24 hours is a configurable engineering assumption, separate
from executable quote freshness; adapter semantics require live verification in 20.

Sources (Sourced, definitions, not profitability claims):
- https://docs.alpaca.markets/us/docs/options-trading-overview — contract identity fields.
- https://docs.alpaca.markets/us/reference/get-options-contracts — deliverable discovery.
- https://www.optionseducation.org/strategies/all-strategies/bull-call-spread-debit-call-spread
- https://www.optionseducation.org/strategies/all-strategies/bull-put-spread-credit-put-spread
- https://www.optionseducation.org/strategies/all-strategies/short-condor
- https://www.optionseducation.org/strategies/all-strategies/long-call-calendar-spread-call-horizontal
- https://prd-web.optionseducation.org/strategies/all-strategies/long-put-calendar-spread-put-horizontal

Retail sizing research: Fidelity describes fixed-dollar and account-percentage
methods; Schwab describes smaller positions as one response to volatility. Neither
establishes a universal HALF rule or what a majority of retail traders do. User
choice is policy, not a claim to have identified a proven optimum.
- https://www.fidelity.com/learning-center/investment-products/options/7-common-options-mistakes
- https://www.schwab.com/learn/story/how-traders-can-take-advantage-volatile-markets

Plan B: refuse incomplete/contradictory contracts, retain a diagnostic reason.
Rollback: revert this step and schema/fixtures together; no persistent state added.

## Verification

- Confirmed both upstream branch heads unchanged from the baseline using read-only
  `git ls-remote`. No remote mutation.
- Focused risk/instrument acceptance: **145 passed in 0.15s**.
- Full `.venv/bin/python -m pytest -q -W error`: **244 passed in 1.30s**.
- `git diff --check` passed; source and affected consumers self-reviewed.
- Added 75 cases covering structure/payoff/identity/provenance failures and success.
  The payoff oracle evaluates 401 underlying prices for eight strategy examples at
  three quantities, independently of the production formula. Calendars have separate
  call/put debit-bound and invalid-front/back/ratio/diagonal cases.
- Old fixtures now have coherent symbols, strikes, premiums and asserted losses.
  Their metadata is an independent explicit synthetic catalog, not generated from
  whichever proposal the test is trying to validate. Existing 169 tests remain passing.
- Malformed copied metadata initially emitted serialization warnings before validation;
  corrected boundary serialization so it reaches validation and returns a rejection.
  Warnings-as-errors remains enabled for the full suite.
- Option proposal JSON cannot supply its own trusted contract book. Contract size,
  deliverable, broker ID, source and observation time must be supplied explicitly.
  Adjusted/unknown contracts, contradictory expiry/right/strike, ratio spreads,
  reversed wings, duplicate identities and stale/future metadata are refused.
- Decisions separate selected stop-risk budget, estimated stop loss plus costs,
  computed gross maximum loss, signed premium, local funding estimate and the
  absent/unverified broker requirement. Downsizing recalculates each measure.
- Example: three units of the tested condor have $60 estimated stop loss, $2 cost
  reserve, $900 gross strategy maximum loss and $600 premium credit. A $62 stop-risk
  budget sizes these three units; it is not presented as a guaranteed $62 loss cap.

## Remaining limits and next step

These are offline engineering checks, not profitability, live metadata, broker-margin
or management acceptance. `approved` still means local risk eligibility only. There
is no order path in this step. Actual broker buying power is explicitly null/unverified;
15 must validate account/structure permission and funding before order submission.
ContractBook is a typed adapter boundary, not cryptographic source authentication.
The adapter must establish deliverables; it cannot invent missing provider fields.

Stop losses and option exit prices remain estimates pending 11; setup registry/tier
verification belongs to 05. Market regime integration belongs to 05. Quoted option
spread/OI provenance requires the fresh selector/adapter in 11/15. Intraday expiration
cutoffs and live source/calendar semantics remain 06/15/20. Unsupported spot/short
shares, adjusted contracts, cash-settled index contracts and reverse calendars cannot
be approved by this validator. No claim of all possible options support is made.

Calendars and condors are included in initial scope, not deferred out of the project;
assignment/exercise/expiry management is still required in 16 before unattended use.
No live-data queries, broker orders, deployment or remote push occurred.

- [x] User's calendar/condor decision recorded.
- [x] Explicit identity, quantity units and source/version contracts added.
- [x] Gross loss independently calculated and proposal assertions cross-checked.
- [x] Premium, stop estimate, funding estimate and broker verification separated.
- [x] Positive/negative/boundary and integration tests passed.
- [x] Checkpoint and tracker updated; no independent-review claim.

Next: **05**, pending the user's account/market-policy answer. The question asks
whether the existing $200 daily, $400 weekly, 10% drawdown and NO_NEW_LONGS blocks
should become approval-time warnings. HALF alone is already resolved as advisory.
Do not silently remove or retain broader blocks under an ambiguous instruction.
