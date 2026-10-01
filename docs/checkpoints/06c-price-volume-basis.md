# Step 06 follow-up C — Price and volume compatibility

Status: protective implementation verified locally; provider acceptance still open. Base: `76298b6`.
Function: watch / analyze. Implementer: Codex; self-review only.

Requirement: replace arbitrary matching scale labels with security-specific,
dated corporate-action coverage; prevent unexplained daily/intraday volume
comparisons. Taz authorized these two remaining Step 06 repairs now. Step 07 stays
paused. Preserve every setup threshold, risk policy and session rule.

Evidence: Webull documents forward-adjusted daily prices and unadjusted minute
prices (https://developer.webull.com/apis/docs/reference/historical-bars/).
Claude's user-supplied adapter addendum reports NVDA split discontinuity,
SPY dividend adjustments, and unequal daily/RTH summed volumes for AAPL and SPY.
These are reported observations, not independent live checks by Codex. Unequal
totals do not establish extended-hours inclusion or an adjustment formula.

Producers/consumers: bar provenance, Webull parsing/profile boundary, completed
daily/intraday data, composed daily/hourly bars, scanner signals and persistence,
feature metadata, volume-consuming triggers and leader selection; synthetic
fixtures and focused regression tests. Existing stored signals remain readable
but require rebuilding if structured price evidence is absent.

Implementation boundary: require explicit complete action coverage, stable
security identity, evidence time, normalization basis and event revisions. Reject
raw histories across effective actions and invalidate old levels after actions or
revisions. Do not invent factors, automatically fetch a new provider, or treat an
empty action list as verified coverage without an explicit evidence contract.
Keep volume channel/definition/share basis separate from price adjustment. Require
compatible verified definitions for EP's first-30-minute versus daily comparison.
Mark composed daily open/volume as intraday-derived; prevent mixed-volume ratios
while preserving price-only RSI(2) evaluation. Leave live profiles disabled.

Acceptance: no-action continuity; split/dividend/revision/security mismatch;
missing/incomplete/future coverage; raw split-spanning history; persisted signal
proof; matching volume positive case; differing/unknown definitions rejected;
mixed developing volume unavailable but price indicators unchanged. Full strict
Python 3.12 and 3.14 suites, diff check, review and branch push after verification.

Unresolved: retail-accessible complete action source and Webull daily open/volume
sale conditions, auction/correction inclusion and share adjustment. Supply an
unsent provider question and precise evidence request. No provider answer or live
acceptance can be replaced with an offline test. Plan B: visible missing-evidence
reason, no qualified affected setup; labeled offline fixtures for integration.
Rollback: revert this checkpoint's implementation/tests together. No orders,
runner activation, credentials or trading-state migration.

## Implementation and verification

- Added `data_basis.py` with structured price/action and separate volume contracts.
  Completed bars, hourly aggregation, scanner entries, EP, RSI(2) snapshots and
  saved signals now use that evidence. Old label-only signals remain readable but
  need rebuilding. No guessed factor or double adjustment is applied.
- Webull retains security identity/native volume channel. Its optional volume
  profile hook awaits a verified producer; default definitions remain unknown.
  Price evidence must match the returned symbol and instrument identity.
- Volume checks cover EP, VCP, cup, Luk's anchored VWAP and leader selection.
  Setup-level missing-data reasons preserve other eligible setup evaluations.
- Features preserve metadata. Developing daily open/volume are intraday-derived;
  developing relative volume is unavailable while the RSI(2) price path works.
  Compatible historical volume ratios remain available. The price-only screen
  diagnostic validates only displayed fields, avoiding unrelated volume failure.
- Review caught/fixed a leader validation-order bug: wrong-security data must not
  remain in the accepted collection after validation fails.
- Synthetic fixtures explicitly identify fictional coverage/volume semantics;
  the historical early-close replay still uses test-only evidence. None of those
  attestations certifies a production feed.
- Added **42** focused cases for actions/revisions/deletions, identity, incomplete/
  future evidence, raw histories, restart, volume/threshold compatibility, setup
  isolation and price-only diagnostics.
- Full strict Python 3.12.14 suite: `.venv/bin/python -m pytest -q -W error`:
  **391 passed in 1.73s**. Python 3.14.6: `../verify-python314/bin/python -m pytest
  -q -W error`: **391 passed in 1.93s**. Taz's iMac re-run remains pending.
- `git diff --check` passed; remote repair branch still matched base `76298b6`
  before publication. Deliver this checkpoint to `codex/repair-step-01-baseline`
  under Taz's existing per-checkpoint push authorization; no merge.
- Self-review only; no independent Claude review or new authenticated live call.
  No thresholds, cards, dependencies, risk policy, schedules or order paths changed.
  No real decision profile is enabled.

## External acceptance is not closed

The code represents/enforces evidence, but a real complete action producer and a
documented compatible volume definition remain unconnected. Adding metadata does
not itself solve the provider problem. [DATA_BASIS_ACCEPTANCE.md](../DATA_BASIS_ACCEPTANCE.md)
contains source limits, an unsent provider question and a read-only Claude request.

Six-item status: (1) sandbox authentication previously reported working; (2) RTH
freshness/completion pending; (3) action safeguards implemented, real producer/
coverage acceptance pending; (4) volume isolation implemented, definition and
reconciliation pending; (5) reported 1,000-session sample verified previously;
(6) reported historical early-close rows replayed previously, without claiming
live early-close publication behavior. Step 07 remains paused.
