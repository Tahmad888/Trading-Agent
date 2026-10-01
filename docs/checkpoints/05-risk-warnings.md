# Checkpoint 05 — User-overridable warnings and durable account state

Status: complete locally. Base: `2789f8a`. Trader-day functions: watch, approve, manage.
Implementer: Codex; no independent review claimed.

## Impact record before implementation

User policy: $200 daily loss, $400 weekly loss, 10% drawdown and bearish-market
conditions become warnings the user can override at approval. HALF is advisory and
does not reduce the selected budget. No account-wide automatic loss halt or silent
warning acknowledgement. An explicit manual stop is distinct and remains effective.
No change to structural setup rules, option liquidity rules or broker requirements.

Producers/consumers: risk.py and contracts.py; market filters and trigger suppression;
new trusted setup eligibility registry and persistent SQLite account/review state;
fixtures/tests; later ticket approval (13), broker reconciliation (15–16), journal (14).
No existing account database to migrate. Schema/version incompatibility must fail.

Account accounting convention (engineering specification, not a trading threshold):
adapter supplies daily/weekly net liquidation P&L including realized/unrealized and
fees, excluding deposits/withdrawals; explicit ET calendar date / Monday week label.
Never invent a zero baseline after restart. Cash-flow-adjusted high-water marks must
not decrease silently; material cash flows require an explicit audited reconciliation.
Missing, nonfinite, future, stale, wrong-period or unreconciled state blocks eligibility.
Open/pending exposure is persisted/displayed without count caps; rejected/canceled
orders are no longer pending in the next authoritative snapshot. Store never infers fills.

Registry comes from code/operator control, not proposal tier. Default all setups to
paper until explicitly promoted; proposals require current card version. A test
registry can allow synthetic live eligibility without changing the production registry.

Market context is separate, timestamped and required. Legacy FULL/HALF/NO_NEW_LONGS
labels remain readable in scanner history but represent favorable/mixed/bearish
conditions. Remove general long-signal suppression so a warning can reach the user.
The Weinstein-specific below-full setup condition stays until its card repair (19).

Warnings are independent of technical rejection checks. A locally eligible ticket
can show warnings; it is not an executable approval. Save warning acknowledgement
against a digest of exact proposal/decision and current account revision. Do not
carry an override to a new ticket/revision. Actual approval tokens/order enforcement
remain 13/15. No order adapter or UI exists yet.

Acceptance: warning thresholds and recovery, HALF/grade combinations, bullish versus
bearish exposure, missing/stale regime, proposal-tier spoofing, stale card version,
restart, corrupt state, out-of-order snapshot, high-water mark preservation, explicit
manual halt/reset audit, open/pending/rejected exposure and exact-review acknowledgements.

Plan B: preserve prior state, reject uncertain inputs and leave recovery/exit management
independent of new-entry eligibility. Rollback: revert code/schema together; preserve
the SQLite file for audit rather than deleting it. No live state will be created here.


## Verification and checkpoint

- Source branches rechecked read-only; remote heads unchanged from baseline.
- Focused warning/state/trigger suite: **70 passed in 0.37s**.
- Full `.venv/bin/python -m pytest -q -W error`: **294 passed in 0.91s**.
- `git diff --check` passed; source changes self-reviewed. No independent review.
- All regime/grade combinations retain budget/quantity. Daily/weekly/drawdown
  boundaries warn; recovery clears warnings rather than leaving an automatic halt.
- Derived instrument direction keeps long puts/bear spreads separate from long
  exposure; neutral calendars/condors still warrant bearish-regime review.
- General long suppression removed, including EP and Connors paths. Trend Template
  and individual card rules remain. Card fingerprints are unchanged.
- Proposal tier cannot promote a setup; unknown/stale card versions reject. Production
  registry defaults to paper; test-only live entries are explicit synthetic fixtures.
- Nonfinite/missing/stale/incorrect-period account or market evidence rejects.
  Open/pending exposure is required explicitly, not silently assumed to be zero.
- SQLite restart restores P&L, high-water mark and 40 tested exposures without count
  limits. Observation replay is idempotent; conflicting/old observations reject.
  Loss warnings never set a manual stop. New snapshots cannot silently clear one.
- Manual stop/resume requires current revision, named actor and reason. Withdrawal
  reconciliation is explicit and audited; a lower incoming high-water mark alone
  cannot erase drawdown history. Unknown schema/corrupted state fails.
- Reviews are calculated from saved account state, digest-bound to proposal/decision,
  and expire with account/quote/market/contract evidence. Changed risk, refreshed
  account, timeout, omitted warnings or duplicate acknowledgement cannot reuse a review.
  Overrides are retained across restart as audit history, never account-wide bypasses.

## Remaining limits

The broker adapter must still produce the defined P&L/cash-flow/exposure semantics;
these tests do not certify a broker endpoint. MarketContext is an adapter boundary:
its timestamp says when evaluated, not whether source bars were genuinely current;
Step 06/20 owns that evidence. Durable snapshots never infer fills or treat a rejected
order as a position. Account events/reconciliation are completed in 15–16.

Acknowledgements are internal review/audit records, NOT executable approval tokens.
Step 13 must display warnings and require an explicit user decision; Step 15 must
revalidate and bind execution. There is no live order path or unattended activation.
Actor identity is supplied by a trusted control handler, not authenticated by a string.
No production state, broker queries, orders, deployment or push occurred.

The Weinstein card's below-full-market condition is a setup definition, distinct from
NO_NEW_LONGS, and remains for source/card repair in 19. Existing option liquidity/DTE
and chase policies were not silently removed by this account/market decision.

- [x] Latest user policy implemented across risk and scanner paths.
- [x] Trusted registry and explicit timestamped account/market inputs.
- [x] Durable state, exposure, manual control and warning-review audit.
- [x] Positive/negative/restart/boundary/integration checks pass.
- [x] Tracker and checkpoint updated; next step is 06.
