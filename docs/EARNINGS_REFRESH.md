# Step 09e: earnings refresh and catalyst review

This is an opt-in implementation checkpoint, not Step 09 closure. Gap repairs
now take priority; see `GAP_REPAIR_PLAN.md`. No scheduler,
scanner, approval or order execution is activated by the commands below.

## What it does

`desk.earnings_refresh` collects a batch of 1–5 explicitly enrolled security/CIK
identities, reuses the reviewed full-archive normalizer, and stores immutable
SQLite snapshots. Each attempt makes the previous success ineligible until the
new attempt finishes. Failure is negatively cached until the configured retry
time. Cache reads happen again at scanner qualification and signal revalidation.
Scanner earnings initialization/refresh failures preserve working price/action
adapters. Required EP/cup evidence stays pending; unrelated setups continue.
Sanitized startup failures appear in the persisted scan discovery report under
`earnings_source`; per-read failures appear in qualification reasons. This is not
a bypass for invalid prices, missing benchmarks or expired required evidence.
A policy/reviewer-file change immediately invalidates the old snapshot. File
locking prevents concurrent refreshers on macOS/Linux. Historical replay must
use a separate database; a backwards timestamp cannot replace a newer snapshot.

Source age is anchored to original receipt. Refresh cannot renew a manual issuer
or catalyst review. An empty upcoming-date response remains UNKNOWN, while a
source outage is recorded separately. A mapped fundamental pair does not imply
an EP catalyst, chart qualification or trade approval.

Original SEC 8-K/8-K/A primary documents are collected as candidates. Each candidate
includes its URL, filing acceptance time, actual receipt, content hash and identity.
Its content is data, never instructions. Acceptance time establishes when the
filing was public; it does not establish an earlier press-release time. After-hours
and non-trading-day filings map to the next exchange session. This is deliberately
conservative; separate original issuer/news evidence is needed for earlier releases.

A reviewer must read the original document and record its relevance. No sentiment,
headline match or Item 2.02 flag automatically qualifies a catalyst. A changed
body/identity/form/items invalidates approval. A newer 8-K/A prevents reuse of an
older candidate's approval pending review of the amendment. Partial document
retrieval cannot retain EP catalyst eligibility. These are engineering safeguards,
not additional chart thresholds.

## One consolidated iMac acceptance batch

Run in the iMac terminal. Reuses the exact saved source archive; market opening is
not required. The final command makes **at most one SEC document request**, no
Webull/Alpha Vantage calls. Stop and paste the output if a command fails. Run this
block once; its unique output folder preserves earlier evidence.

```sh
(
set -e
cd "$HOME/Trading-Agent"
[ "$(git branch --show-current)" = "codex/repair-step-01-baseline" ]
git pull --ff-only origin codex/repair-step-01-baseline
source .venv/bin/activate
source "$HOME/.config/trading-desk/env"
python -m pytest -q -W error

STEP9_OUTPUT="$(mktemp -d "$HOME/Desktop/step9-acceptance.XXXXXX")"
STEP9_ARCHIVE="$HOME/Desktop/step9-sources/20261001T193535Z-14ecec3d"

python -m desk.earnings_refresh \
  --policy config/step09-refresh/historical-2026-10-01.json \
  --database "$STEP9_OUTPUT/earnings.sqlite" \
  --output-dir "$STEP9_OUTPUT/observations" \
  --archive "$STEP9_ARCHIVE" \
  --at 2026-10-01T20:23:34Z \
  --report "$STEP9_OUTPUT/refresh.json"

python -m desk.earnings_cache_check \
  --policy config/step09-refresh/historical-2026-10-01.json \
  --database "$STEP9_OUTPUT/earnings.sqlite" \
  --at 2026-10-01T20:23:34Z

python -m desk.catalysts \
  --archive "$STEP9_ARCHIVE" \
  --at 2026-10-01T20:23:34Z \
  --symbol NVDA --security-id 913257561 --cik 0001045810 \
  --lookback-days 90 --limit 1 \
  --output "$STEP9_OUTPUT/sec-document.json"
)
```

Expected: strict test count recorded in checkpoint 09e; refresh READY for all three
companies; cache inspection EVALUATED, with supported cup fundamentals and EP
pending (this archive replay does not fetch/approve catalysts). The last command
should show AVAILABLE and one candidate, with the full body saved privately rather
than printed. NO_DOCUMENT_TESTED is inconclusive, not a successful route test.
Enter your own application/contact if asked; this is the SEC User-Agent, not an API
key. Stop after an SEC error; do not repeatedly retry.

Paste the test count and three JSON outputs together. These results verify actual
archived mapping/cache behavior and original-document access. They do **not** prove
current live batch refresh, a reviewed real catalyst through the scanner, or broad
watchlist coverage. Those remain explicit Step 09 exit items.

## Review format and runtime wiring

`config/step09-refresh/historical-2026-10-01.json` is an expired historical test
policy. Do not activate it or simply extend the dated MSFT issuer review. A live
policy needs current source identities, current issuer evidence where SEC lacks
explicit quarters, an explicit validity period, and operator-chosen refresh/retry
intervals. The one-hour example is an engineering test setting, not a trading rule
or a claimed sufficient news latency. The bounded collector is not yet a full
watchlist refresh queue.

The optional `catalyst_reviews` policy field names a local JSON file relative to
the policy. The file contains `{"schema_version":1,"reviews":[...]}`. Each review:

```json
{
  "document_id": "COPY_EXACT_64_CHARACTER_HASH_FROM_CANDIDATE",
  "symbol": "NVDA",
  "security_id": "913257561",
  "decision": "accepted",
  "reviewed_at": "ACTUAL_REVIEW_TIMESTAMP_WITH_TIMEZONE",
  "valid_until": "EXPLICIT_REVIEW_EXPIRY_WITH_TIMEZONE",
  "kind": "earnings",
  "summary": "What the original filing actually reports",
  "relevance_ref": "Why this event is relevant; supporting original evidence",
  "report_period_end": "YYYY-MM-DD"
}
```

This is a template, not approved evidence. `decision` may be `rejected`; `kind`
may be `other`, with a null period. Earnings candidates require Item 2.02 and a
period matching the actual results used by the existing evaluator. A primary
filing may point to a separate earnings-release exhibit: do not approve details
that you have not verified in original evidence. Exhibit/issuer/news ingestion
beyond the primary document is still a coverage limitation.

An explicit refresh is `python -m desk.earnings_refresh --policy PATH --database
PATH --output-dir PATH --report NEW_FILE`. Without `--archive/--at` it uses current
Webull/SEC sources. It can collect current candidates but does not approve them.
Changing a review file then refreshing binds the exact reviewed content to the new
snapshot. Each symbol is still subject to all existing chart and freshness checks.

Future authorized scanner wiring uses `DESK_EARNINGS_POLICY`,
`DESK_EARNINGS_CACHE`, `DESK_EARNINGS_ARCHIVES`, `SEC_USER_AGENT`, and
`DESK_EARNINGS_AUTO_REFRESH=1`. These conflict with the older standalone
`DESK_EARNINGS_EVIDENCE` setting. No environment values or schedules were changed
in this checkpoint. Remove the new opt-in configuration to roll back; retain the
cache/archive as evidence. Append-only archives currently require operator-managed
retention; no automatic deletion is implemented.

## Evidence and remaining limits

- [SEC APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
  describe submissions/companyfacts dissemination; they do not guarantee that a
  press release is already represented in a filing or normalized quarter pair.
- [Form 8-K](https://www.sec.gov/files/form8-k.pdf) distinguishes event items and
  permits filing after the underlying event. Filing acceptance is not release time.
- [Qullamaggie's EP workflow](https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/)
  supports checking news and original company/SEC material. It does not validate
  our refresh cadence or justify inventing catalyst relevance.

SEC-only candidates miss non-filing news and may lag original releases. Unknown
issuer mappings remain pending. viaNexus is optional and unverified here; no report
or callable connector is available. Current live refresh, real catalyst review,
full watchlist enrollment/coverage and final actual-host scanner-to-review acceptance
remain before closing Step 09. Step 10 is not started.
