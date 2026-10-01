# Step 08 — Discovery and watchlist coverage

Status: code complete, verified locally on Python 3.12 and 3.14. Base c49ecd8; upstream matched. Serves watch.
Codex implements and self-reviews; no independent agent dispatched. Taz authorized
this step and verified checkpoint pushes, not activation or orders.

## Impact record before changes

Checked: current discovery only ranks gainers/activity, .isalpha() excludes valid
share-class symbols, all non-core-ETF symbols are guessed to be stocks, close/EP
preparation uses a blanket 260-bar requirement, additions wait for Friday, and a
healthy zero-leader result is treated as a failed refresh.

User/plan requirements: keep leaders, gap/momentum, bearish names, core ETFs and
user additions distinct. Option liquidity must not suppress underlying analysis.
An insufficient history for one setup must not suppress unrelated setups.

Sourced 2026-10-01: Webull documents ASC losers and DESC gainers on the existing
retail screener, limited to top 200 results (not exhaustive market coverage):
https://developer.webull.com/apis/docs/reference/get-gainers-losers/
Retail read-only instrument reference route:
https://developer.webull.com/apis/docs/reference/instrument-list/
Official webull-openapi-python-sdk 3.0.2, data/request/get_instruments_request_v2.py
confirms GET /trading/instruments/stocks/profiles/list, v3, symbols/category and
pagination_key. This is security reference data, not order/account access.
The older public GitHub SDK branch still has obsolete v1 paths; do not use those.

Planned changes: validated security metadata and metadata-based bar routing;
separate bearish sources; source-labelled discovery diagnostics; per-setup warm-up;
next-scan user preparation including eligible EP checks after its first 30 minutes;
atomic watchlist/status persistence separating healthy empty, partial/failed and
stale prior builds. Existing risk and frozen card numbers stay unchanged.
EP needs max(60 sideways bars, 50 volume bars, 20 ADR bars), not 260. Retain 1,000
bar requests as maximum warm-up; accepting young data does not waive required
windows. Template-dependent setups retain the full template requirements. Bearish
setups do not inherit a long-template failure. Underlying metadata does not infer
borrow, option liquidity or permission to execute shorts.

Affected: watchlist, scanner, Webull read-only reference adapter, SignalStore
suspension for removals, trigger dispatch/history guards, shared fake data sources,
new coverage tests, tracker and operator docs. No schema migration to signal events.
Acceptance: share-class symbol; ETF routing; missing/conflicting metadata; non-
optionable stock; bearish setup; 60-bar EP and 59-bar rejection; per-setup reasons;
next intraday user addition/removal; healthy empty vs failed build; stale fallback;
combined existing lifecycle/data/risk tests on both local Python versions.

Plan B: preserve last successful list with explicit stale/degraded status; still
analyze core/user candidates on independently validated data. Unknown security or
bar identity is skipped with a reason. Live metadata access remains a provider
acceptance observation, not inferred from mock tests. No new broad provider test
program or source entitlement claim. Rollback coordinated discovery changes,
retain state/logs; legacy watchlist mappings remain readable.

## Completed changes and review

- Added `security.py` for validated common-stock/ETF identities, currency, exchange,
  category, subcategory and instrument ID. Share-class punctuation is accepted.
  Missing/malformed/duplicate/conflicting IDs reject observably; no optionability
  gate or inference of borrow availability. Bar identities must agree when supplied.
- Added read-only Webull instrument lookup, cursor handling and a five-minute
  in-process cache. The five-minute cache is an engineering assumption. Primary
  SDK source: https://pypi.org/project/webull-openapi-python-sdk/3.0.2/ . Schema
  reference: https://developer.webull.com/apis/docs/reference/broker-fd-api/list-stock-instruments/ .
  The implementation uses the retail route, not a broker API host.
- Daily close preparation includes separate gainers/activity and DAY_1/DAY_5/MONTH_1
  losers. Existing weekly leadership and core/user source labels are preserved.
  Negative change selects bearish candidates; setup rules still decide eligibility.
- Warm-up requirements derive from existing cards/indicators. EP accepts 60 full
  prior sessions; Holy Grail and Stage 4 shorts no longer inherit long-template
  rejection. Darvas now checks the 52-week feature at the box top itself, avoiding
  a NaN comparison for a young candidate. No card or trading threshold changed.
- User additions receive daily preparation on the next intraday scan and EP
  preparation once the first 30 minutes are complete, including additions after
  10:00. Daily and EP completion are tracked separately; failed inputs retry on a
  later slot. Removals suppress candidates and fresh review except mandatory ETFs.
- Atomic list/status writes distinguish READY, healthy EMPTY, PARTIAL and FAILED.
  Partial/failed builds keep the prior list. UNKNOWN/INVALID status and seven-day
  age are explicit; age is an engineering label, not a trading signal.
- Removed the generic category guess. Self-review caught its shared Step 06
  diagnostic consumer; that CLI now uses its explicit reviewed NVDA/SPY map and
  rejects other symbols. It retains its five stages and original API-call scope.
- Updated shared synthetic fixtures, discovery tests, tracker and operator guide.

## Verification

All market data in the new tests is synthetic, not live provider evidence.

- `.venv/bin/python -m pytest -q -W error`: **599 passed in 3.18s**, Python 3.12.14.
- `../verify-python314/bin/python -m pytest -q -W error`: **599 passed in 3.36s**,
  Python 3.14.6.
- `git diff --check`: passed.
- 35 new cases cover identity rejection, ETF routing, non-optionable stocks,
  punctuation, bearish actual setup dispatch, EP history boundaries, next-scan
  and late additions, EP retry, removals, malformed config, healthy-empty/partial/
  failed refresh, stale/corrupt status, metadata cache expiry/pagination and the
  bounded Step 06 diagnostic. Existing lifecycle/data/risk tests remain passing.
- Codex self-review only; no independent reviewer or Claude run claimed.

## Limits and handoff

Update: later iMac observations and the resulting alias repair are recorded in
`08a-webull-symbols.md`; its final provider probe remains pending. The following
paragraph describes the initial implementation checkpoint.

No authenticated Webull metadata request was made. Route/SDK research and mocked
transport tests do not prove sandbox or live entitlement. Before activation,
Step 20 must capture real metadata and verify symbol/ID/category mapping on the
configured host; if unavailable, discovery skips with explicit reasons. The
Step 06 NVDA/SPY observations remain bounded to their dated reviewed profiles.

Top-200 lists are candidate sources, not exhaustive exchange coverage. Weekly
leadership still requires its own full history; young names reach EP through
movers or user additions. Earnings/growth/catalyst evidence is Step 09; setup
fidelity remains Step 10. This checkpoint does not prove trading profitability.
No keys, orders, live runner, scans against accounts or schedules were activated.

Rollback: preserve list/status, scan logs and signals.sqlite, then revert the
coordinated Step 08 code/docs. No database schema migration is introduced.
Step 08 complete. Stop here. Next: Step 09 earnings and catalyst evidence.
