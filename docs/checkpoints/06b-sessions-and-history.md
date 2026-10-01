# Step 06 follow-up B — Explicit sessions and historical windows

Status: bounded adapter patch verified locally; new live-path recheck pending. Base: `45c45cb`.
Function: watch / data verification. Implementer: Codex; self-review.

## Impact record before implementation

Taz supplied Claude's adapter addenda and authorized completing the six Step 06
items before Step 07. The second addendum reports 1,000 unique SPY daily sessions
with no calendar gaps and supplies all 14 SPY RTH M15 rows for 2025-07-03 on both
the sandbox HTTP and connector routes. The HTTP tests used the adapter's private
`_call` because `bars` cannot accept historical windows. This bounded patch exposes
that capability and removes reliance on the undocumented intraday session default.

Changes: `WebullData.bars` explicitly requests RTH for minute timespans by default,
validates returned row session tags against the requested set, retains session
metadata, and accepts optional integer epoch-millisecond start/end bounds and an
explicit boolean `real_time_required` (default stays true). Filter returned bars
to inclusive timestamp bounds because the earlier report observed a row before
the requested lower bound. Do not infer that a requested range is complete.

Daily session tags are empty in observed replies; do not require RTH row tags for
daily/weekly/monthly/yearly data or claim daily open/volume equivalence. Explicit
extended-session requests are diagnostic data and must not receive a regular-only
decision profile. Time/adjustment/price-scale profiles remain unwired in the CLI.

Consumers: scanner's existing minute fetches gain explicit RTH automatically;
read-only verification can call `bars` rather than private request internals.
No scanner schedule, indicator formula, trading rule or database schema changes.

Acceptance: default RTH payload; explicit multiple sessions; reject missing,
unexpected or malformed intraday session tags; reject malformed request bounds
before transport; inclusive bounds actually trim provider over-return; historical
early-close supplied rows survive parsing and match the exchange calendar; default
unknown provenance continues to prevent decision eligibility. Full strict suites
on Python 3.12 and 3.14. No claim of new live calls by Codex.

Evidence (user-supplied observations, not independently repeated live):
- `webull-data-verification-2026-10-01-adapter-addendum.md`, audited `7c11d54`.
- `webull-data-verification-2026-10-01-addendum-2.md`, audited `45c45cb`.
- https://developer.webull.com/apis/docs/reference/historical-bars/
- https://github.com/webull-inc/webull-openapi-skills/blob/main/webull_skill/market_data/stock.py
- https://www.nyse.com/publicdocs/ICE_NYSE_2025_Yearly_Trading_Calendar.pdf

Plan B: retain raw historical evidence and a visible failed/unknown check. Do not
replace missing session tags with a guess or label history as a current opportunity.
Rollback: revert this patch and new tests together; no persistent state migration.

## Six-item acceptance tracker

1. Adapter/key path: Claude reports successful sandbox authentication and parsing;
   iMac offline installation confirmed by Taz (328 passed in 3.93s). This does not
   establish production access or real-time entitlement.
2. Sessions/completion/freshness: session/window patch implemented and tested offline. RTH observations
   pending. Prior OVN observations show about 900s lag despite delay_minutes=0.
   Do not shift every bar by a guessed 15 minutes; M15 behavior remains inconclusive.
3. Corporate-action compatibility: evidence confirms differing adjustment treatment;
   normalization/coverage and meaningful scale provenance remain unresolved.
4. Daily open/volume: mismatch reproduced; definition remains unknown. Unequal
   totals alone do not prove extended-hours inclusion.
5. History: AAPL 1,000-row retrieval reported in addendum 1; SPY 1,000 unique expected
   sessions and zero gaps reported in addendum 2. Scope is these observed samples.
6. Early close: 14 SPY RTH bars on July 3, 2025 reported on both routes; their full
   supplied rows were replayed offline as a regression fixture. This does not
   establish how forming bars are published during a live early-close session.

Taz authorized temporary sandbox integration / clearly labeled historical tests.
Production real-time acceptance remains pending. This patch neither enables a live
profile nor implements a full replay runner; Step 07 remains paused.

## Verification and delivery

- `tests/fixtures/webull-spy-2025-07-03.json` preserves the 14 supplied rows with
  the source report's SHA-256, audited commit and request timestamp. Values were
  extracted from the report, not regenerated or fetched independently by Codex.
- `tests/test_webull_history.py` exercises public `bars()` with those rows: exact
  14-bar calendar coverage and a 4-bar hourly aggregation ending in a 30-minute bar.
  Calendar/aggregation uses an explicitly test-only historical profile; ordinary
  parsed frames still fail the provenance check. This is offline historical data
  verification, not a current signal or observed live early-close publication.
- Explicit RTH request, missing/incorrect session rejection, multiple-session raw
  diagnostics, inclusive window clipping and empty-window failure covered.
- Invalid session strings, noninteger/negative/out-of-range/reversed bounds and
  nonboolean real-time flags fail before transport. Daily empty session tags remain
  readable without inventing daily/intraday volume equivalence.
- Focused provider/history/data suite: **68 passed in 0.59s** on Python 3.14.6.
- Full strict suite: **349 passed in 1.71s** on Python 3.14.6 and **349 passed in
  1.62s** on Python 3.12.14. `git diff --check` passed. Self-review only.
- No dependency changes, new live requests, credential access, scheduler activation
  or broker orders. Ready for the authorized repair-branch push and iMac re-run.

Next: RTH feed timing evidence and an independently verified freshness boundary;
corporate-action provenance and daily-volume definitions remain open. A zero delay
field is still insufficient. No production decision profile is enabled by this patch.
