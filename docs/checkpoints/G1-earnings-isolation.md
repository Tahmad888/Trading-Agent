# G1 — isolate earnings failures from price scans

IMPLEMENTED AND LOCALLY VERIFIED. Base 9ba1cb8c9f7300fc9c986b1f7d3f2a6d5b8089b5; upstream matched.
Serves watch/analyze. Read `../GAP_REPAIR_PLAN.md`. Step 09 remains incomplete.

## Before-code impact record

Checked defect: scanner.main initializes Webull, refreshes earnings, and builds
both action/earnings wrappers in one exception boundary. A malformed earnings
policy, filesystem error or cache fault can destroy access to healthy price data.

Requirement: preserve the initialized price source and its action checks. Isolate
optional earnings initialization/refresh exceptions with a delegating unavailable
source that cannot fall back to old or underlying earnings. Existing qualify
already handles per-read errors and returns NOT_REQUIRED for unrelated setups,
PENDING_EVIDENCE for required EP/cup. Preserve that behavior and diagnostics.

Producers: Webull/price-action setup; optional earnings file/cache/policy/refresh.
Consumers: scanner CLI, qualification, signal revalidation, persisted scan reports.
Record sanitized startup earnings failure even if no signals are found. Do not log
raw provider errors, filesystem paths or credentials. No provider calls in tests.

Acceptance: original main() reproducer with invalid policy; cache/refresh/file
failures; nondependent breakout continues while cup/EP remain pending; fresh
review cannot reuse previously qualified evidence after outage; malformed/stale
per-symbol evidence does not contaminate other symbols; genuine price errors and
missing benchmark errors remain visible. Existing unknown-growth tests retained.

No schema migration or new trading policy. No changes to stop construction,
sizing, action coverage, approval, schedules or orders in G1. Price-side failure
semantics retained. Rollback: revert G1 code
and its documentation together, preserve historical scan records.

## Implementation and verification

`earnings.scanner_source` catches optional setup/refresh failures and returns an
unavailable-earnings wrapper that delegates price/action/discovery methods. Both
earnings access methods are masked, so earlier underlying evidence cannot leak
through. Cache policy/reviewer bindings are checked at initialization; subsequent
read failures still pass through the existing qualification handler. The scanner
initializes its price/action source first and records safe earnings startup
failure in the scan discovery log without marking a healthy price scan failed.

14 new synthetic tests exercise the CLI/main reproduction with malformed/missing
policy, invalid SQLite contents/path, conflicting sources and refresh exceptions;
prove action checks are retained; test trigger/review outage and recovery; isolate
bad/expired evidence by symbol; and retain SPY/QQQ and ticker-price failure handling.
Existing tests cover missing credentials, unknown growth and cache revisions.

- Focused scanner/earnings/isolation suite: **74 passed in 1.12s**.
- Python 3.12 strict full suite: **803 passed in 4.48s**.
- Python 3.14 strict full suite: **803 passed in 4.42s**.
- `git diff --check`: passed; upstream still matched base before commit.
- Codex self-review only; Claude/DeepSeek reviewed the plan, not this patch.
- No live APIs, credentials, runtime settings, orders or schedules used/changed.

G1 is complete for this code failure boundary. Optional refresh is still synchronous;
whole-watchlist queuing is not implemented here. Missing benchmark data still
prevents arming under the existing shared market gate; healthy ticker features are
processed and benchmark errors remain visible. G1 does not claim to repair that
broader dependency or to activate operational observation days.

Next checkpoint: G2, actual stop construction and independently resolved risk terms.
Step 09 remains incomplete. Stop at this checkpoint before changing G2 contracts.
