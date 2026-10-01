# Step 08 follow-up — verified Webull share-class identity

Status: code complete; final iMac metadata/bar probe pending. Base 650884e; upstream matched before edits.
Serves watch/analyze. Step 09 remains paused. Codex implements and self-reviews.

## Impact record before implementation

Checked: Taz supplied iMac observations on 2026-10-01. At 18:10:21Z metadata
validated NVDA 913257561 and SPY 913243251, but BRK.B was missing. At 18:11:23Z
NVDA/SPY metadata and daily-bar IDs matched; BRK-B was also missing. A subsequent
single-symbol lookup returned BRK B, ID 916040668, COMMON_STOCK, USD, NYSE,
BERKSHIRE HATHAWAY INC DEL. That final report has no capture timestamp; do not
invent one. Exact host was not printed; the conversation identifies paper keys.
These are user-supplied observations, not a local authenticated test by Codex.

Cause: provider spelling differs from desk spelling; the generic symbol validator
excludes spaces. Fixing only the regex would leave dotted/hyphenated user inputs,
rankings, removals and bar identity matching inconsistent.

Plan: one explicit, ID-checked mapping for the observed Berkshire B identity.
Desk identity BRK.B; Webull request identity BRK B; accept BRK-B as a user alias.
No blanket punctuation substitution for other securities. Normalize watchlist
sources/additions/removals, metadata request/response/cache and bar request/response;
retain the raw provider identity for audit. Existing symbol-based read-only methods
send the same wire spelling. Unknown symbols/types still require metadata. No new
corporate-action coverage, price profiles or eligibility follows from an alias.

Affected: symbol utility, security validation, Webull adapter, watchlist, scanner
fetch/removal checks and tests. Preserve normalized IDs for ActionBackedSource,
price/volume evidence and signal persistence. No DB migration; no known accepted
Berkshire production candidates existed in the observed failing path.

Acceptance: all three user spellings deduplicate; metadata cached under canonical
identity; ranking-origin candidate reaches bar fetch; D and M15 requests use wire
spelling; raw and normalized identities retained; wrong/absent IDs and duplicate
response aliases reject; unrelated symbols aren't rewritten; removals match aliases;
existing strict suite passes on both Python versions. Then Taz runs a bounded iMac
check of the repaired adapter; that live bar observation is still outstanding.

Plan B: explicit metadata/identity failure with no signal, not guessed identity.
Rollback coordinated alias changes while retaining observations. No orders,
schedules or broad source scans. Step 09 does not resume automatically.

## Implementation and verification

Explicit alias map lives in `src/desk/symbols.py`. Both metadata and bar responses
must match 916040668 when resolving Berkshire B. Raw wire spelling remains in
`provider_symbol` / `provider_identity_raw`; canonical identity continues into
price/action validation. No provider-wide share-class naming rule is inferred.
Watchlist source labels merge across aliases; removals work with any accepted
spelling. Read-only snapshot/calendar/action request parameters use wire spelling;
their payloads remain raw observations and are not new decision evidence.

`python -m desk.metadata_check --symbols NVDA SPY BRK.B` now provides the bounded
read-only probe: metadata once, up to two category batches for D and M15, no
fetch retries, raw transport details suppressed, actual host printed. This is
identity verification only. It does not validate freshness, corporate-action
coverage, volume definitions, setups, earnings or orders.

- Python 3.12.14: `.venv/bin/python -m pytest -q -W error` — **621 passed in 3.21s**.
- Python 3.14.6: `../verify-python314/bin/python -m pytest -q -W error` —
  **621 passed in 3.34s**.
- `git diff --check`: passed.
- 22 new regression cases use the user-observed metadata identity and generated
  bar payloads. Synthetic bar tests do not establish live Berkshire bar access.
- Existing generic metadata/cache tests use a generic ABC fixture instead of
  pretending the earlier invented BRK.B record represents the observed provider.
- Self-review only. No authenticated request made from this Codex workspace.

Next: user pulls this checkpoint on the iMac and runs the probe. Resolve any
returned mismatch before closing Step 08 provider verification. Step 09 paused.
