# G3 — automatic vendor price basis and persisted-history revisions

2026-10-01; base fcf7c72, upstream matched before edits. Implementation complete; host acceptance pending. Serves watch/analyze/plan.
No orders, runner activation, external messages or additional data subscriptions.

## Before-code record

Checked: ActionBackedSource requires symbol/timeframe reviews plus a current dated
ledger before any bars can pass. Signal compatibility compares event ledgers but
not the actual daily OHLCV version that produced its levels.
Sourced: Webull says daily-and-above are forward-adjusted and minutes unadjusted:
https://developer.webull.com/apis/docs/reference/historical-bars/
Checked (bounded prior evidence): six NVDA/SPY adjustment observations and ten
AAPL/SPY sessions corroborate adjusted daily prices / raw RTH closes. They do not
prove universal action coverage or matching open/high/volume definitions. See
../evidence/step06-native-channels.md. viaNexus split results included dividend
records and conflicting dates; do not use that connector as an automatic local feed.

Design: a distinct vendor-history basis, never a fabricated complete action ledger.
Resolve current Webull metadata automatically for requested common shares/ETFs;
pin identity durably. Fetch native daily history and a complete raw RTH M15 session
for the latest completed daily bar; compare closes only. Engineering assumption:
absolute 0.000001 rounding tolerance, based on six-decimal reported observations,
not an economic gap threshold. Mismatches/missing data are per-ticker exceptions.
Use optional existing reviewed action evidence diagnostically for unresolved comparisons only.
No jump detector, guessed adjustment factor or silent price rewriting.

Preserve the exact completed daily OHLCV reference in each signal's basis. On
intraday revalidation fetch current daily history and compare the overlapping
rows against that reference, not just the last cached refresh. Require all original
reference dates: insufficient overlap means unavailable/rebuild, never pass.
Revisions invalidate affected old candidates; new detection can rebuild from fresh
history. Persist refresh summaries and pinned identities in SQLite across restarts.

Volume is independent: automatic price acceptance does not assert historical
share-volume adjustment. Preserve reviewed volume evidence when available; otherwise
volume-dependent checks stay visibly unavailable while price-only checks proceed.
This checkpoint cannot honestly claim automatic EP-volume coverage on every ticker.
The legacy source remains usable for unresolved price/volume cases without becoming
a mandatory per-ticker enrollment step on the primary price path.

Affected: new vendor source/store/probe, price-basis union, bar provenance and
compatibility, scanner failure isolation/invalidations, tests, config docs.
Acceptance: 10:1 split versus genuine gap; ordinary/special dividend revisions;
unchanged history/first-time ticker; altered volume; missing historical rows;
D/M15 mismatch; changed identities; exact old signal reference after cache refresh;
restart, provider outage, per-ticker isolation, reviewed evidence cannot override contradictions, and no fabricated
volume coverage. Run strict combined suites on Python 3.12/3.14.

Rollback: revert G3, retain new SQLite/evidence files; rebuild candidates through the
legacy reviewed source. Mixed basis types cannot silently revalidate each other.
No current host checks/credentials available here; provide a read-only probe for
actual-host verification and report that result separately from local tests.

## Implemented and verified

Added VendorBasisSource, durable identity/history storage and a read-only vendor_check
CLI. Price evidence is a distinct VendorPriceBasis contract; no complete-action
attestation is invented. The scanner preserves original daily OHLCV with candidates
and invalidates revised ones, including candidates that had not yet created an event.
Replacements cannot replay crossings through the revision-detection time. The daily
request ends at the latest closed session, excluding a forming bar at the provider.

Review discoveries repaired within G3:
- One malformed Webull row previously raised before the wrapper could isolate it.
  Opt-in partial bars/metadata parsers now preserve attributable healthy peers;
  strict callers retain their prior behavior. Provider-wide failures are not retried
  once per ticker by this source.
- An early implementation let a dated reviewed fallback override contradictory
  daily/raw closes and bypass the new history reference. Removed that acceptance
  path. Reviewed action evidence is diagnostic only for a price conflict.
- A revision detected after a previously consumed bar could leave a replay window
  for a rebuilt candidate. Persist the revision-time boundary, including when no
  original event existed.

- Identical history refreshes now preserve candidate identity despite new receipt
  times; executable terms, history and instrument identity stay in the hash.
- A detected history revision retires older optional volume attestations, including
  across restart. A consistent subsequent refresh cannot revive that older review.

Tests cover all acceptance cases above plus wrong host, parser duplicates/delay/
missing/bad rows, mixed evidence migration, repeated refresh and restart, and an
integrated preparation-to-triggered-breakout path with no manual enrollment and
an isolated bad ticker. No setup thresholds or volume requirements changed.

Validation on the final code:
- `.venv/bin/python -m pytest -q -W error`: **892 passed in 5.96s**, Python 3.12.
- `../verify-python314/bin/python -m pytest -q -W error`: **892 passed in 5.83s**, Python 3.14.
- `git diff --check`: passed. `desk.vendor_check --help`: passed.
- Remote repair branch rechecked before commit: still base `fcf7c72`.

All 36 added G3 cases use synthetic observations; no current provider result is
claimed. The integrated case verifies preparation, a real detector, observed stop
construction and persisted event behavior, not just evidence helper functions.
The actual-host probe remains unrun here; no provider credentials were read.

Changed surfaces: action_source configuration, data_basis/bar_contract evidence,
scanner and signal_state invalidation, Webull partial parsing, new vendor source/
probe/tests, .env.example and active plan/checkpoint documentation.

Remaining: actual-host report; separately sourced whole-watchlist volume evidence;
explicit reconciliation for conflicting price pairs or changed instrument identities.
None is claimed complete by synthetic tests. G4/G5 and unfinished Step 09 remain.
Next action is the read-only iMac command in ../VENDOR_BASIS.md, then review the
result before runtime activation or claiming host acceptance. No G4 work started.
