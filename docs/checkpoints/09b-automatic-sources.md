# Step 09 reopened — automatic earnings sources

Status: IN PROGRESS. Base 30d8585; upstream matched. Taz explicitly asked to finish
remaining Step 09 work before Step 10. The earlier closure was premature: local
validation and one reviewed NVDA file do not demonstrate automatic data supply.
Serves watch/analyze. Codex implements and self-reviews; no independent reviewer.

## Remaining work and exit criteria

1. Source access and schema: inspect official requests, collect bounded Webull
   calendar/income/financial-alert observations and SEC identity/submissions/facts
   on the actual host. Retain timestamps and raw public evidence safely. Empty,
   unavailable and malformed are distinct. Source observations alone do not close 09.
2. Reported-results adapter: fetch matching quarters automatically for the watchlist,
   preserve units/accounting/split basis and revision provenance, validate against
   issuer reports for several companies. Do not confuse fiscal labels of the filing
   with each comparative fact's period, annual/YTD with quarters, or a recent 10-Q
   with the newest earnings release. Support missing/negative baselines explicitly.
3. Future calendar: retrieve dates automatically, distinguish estimated/confirmed,
   preserve conflicts, receipt and revision times. Company-unannounced stays unknown;
   retrieval failure must not look like a successful search finding no event.
4. Catalyst retrieval and review: collect timestamped linked source material for
   candidate symbols, verify relevance and connect it to the entry session. Prior
   after-hours/weekend news may belong to the next trading session. Do not require
   a headline's calendar date to equal entry date or treat every 8-K as earnings.
   No sentiment score automatically qualifies a catalyst. Current cards/user policy
   remain unchanged; any source-policy disagreement must be shown to Taz.
5. Watchlist integration: opt-in fetch/refresh, bounded caching, source health,
   revision invalidation and preserved evidence at each evaluation. No scheduler
   activation. Test normal, stale, conflict, missing and outage paths together.
6. Actual-host acceptance: several companies, a real event example, next-date
   confidence, revisions/outage and complete scanner-to-fresh-review path. Both
   strict suites and iMac check; all outstanding limitations stated before closure.

## Research and impact record before code

Sourced trader workflow: Qullamaggie describes scanning gappers, checking catalyst
news, and inspecting historical earnings/sales. He explicitly uses SEC/company
sites and lists multiple catalyst types, including sector moves without specific
company news. This supports source gathering, not a universal rule every retail
trader follows and not a new requirement to buy a service.
https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/
The existing 25%/50% thresholds remain user/card policy, not newly attributed to
that article. The current card's growth gate on all EP types is narrower than the
article's examples; no silent policy change in this source-access checkpoint.

Sourced provider: official Webull SDK 3.0.2 (wheel inspected without installing)
get_financials_alert_request.py defines GET v3
/market-data/fundamentals/financial-alerts/get with symbol/category. Existing income
request type QUARTERLY/count 5 matches the SDK. No basis to guess another parameter
to force data out. SDK calendar docs describe six months before/after today, with
actual EPS distinguishing reported entries; this is not a prior-year history feed.
https://pypi.org/project/webull-openapi-python-sdk/3.0.2/
https://developer.webull.com.mx/apis/docs/reference/financial-alert/
Webull news summary is LLM-generated; it cannot replace original source evidence:
https://developer.webull.com/apis/docs/reference/news/

Sourced SEC: submissions and company facts APIs are public, with company CIK,
filing history and separate facts/units. Custom taxonomy and fiscal calendars need
care; availability follows filings, not necessarily the earlier earnings release.
https://www.sec.gov/search-filings/edgar-application-programming-interfaces
SEC requires a declared user agent and moderate traffic (maximum 10 requests/sec
across machines). Use a real operator contact in SEC_USER_AGENT; never log it.
Engineering choice: sequential requests spaced at least 0.25s; no retries after
403/429, no IP/key cycling. This is pacing, not a trading threshold.
https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data

Checked in this Codex environment: one request each to NVDA submissions and company
facts returned HTTP 403. No successful SEC payload obtained, no cause established.
Webull credentials are on the iMac/Claude, not loaded here. A request specification
is not live provider acceptance. Actual-host collection is the next dependency.

This checkpoint's implementation: add the exact financial-alert GET method and one
read-only source collection CLI, with strict identity matching, bounded requests,
redacted Webull payloads, SEC contact kept out of reports, local dated observations
and compact report. Producers: Webull/SEC; consumers: later source-mapping work,
not scanner qualification. No guessed earnings normalization or dummy production
facts. No API-key purchases, trading calls, runner or source activation.

Acceptance: documented route/parameters, share-class alias, source identity mismatch,
empty versus failed response, HTTP-200 error envelopes, stop-on-error per provider,
secret redaction before disk, receipt/hash provenance and no promotion to eligibility.
SEC tests are synthetic until a successful actual-host observation is supplied.
Plan B: independent sources still run; report the failed source and retain earlier
observations without representing them as current successful data. Never fill gaps
with forecasts or silently extend the old NVDA review's validity.
Rollback: code/docs together; keep collected evidence files outside repo. No DB
migration. Step 09 stays OPEN until items 1–6 are verified, not merely this probe.

## Implemented source-access checkpoint

- `WebullData.financial_alert`: verified SDK GET v3 route and symbol/category,
  including the established BRK.B wire alias. No account/order methods.
- `SecData`: public ticker-to-CIK lookup, submissions and companyfacts collection,
  declared operator contact, sequential pacing, no redirects/retries, bounded JSON,
  CIK/ticker checks, sanitized transport/schema errors and closed HTTP error bodies.
- `desk.earnings_sources`: bounded multi-company collection, one source failure
  does not suppress the independent provider, empty/error distinctions, private
  local evidence files, pre-write Webull credential redaction, receipt/hash
  provenance and compact samples. No quarter normalization or eligibility output.
- Source observations are immutable per run, not current-data cache guarantees.
  No new real response fixtures were fabricated. SEC tests remain synthetic.
- Repository status now explicitly reopens Step 09 and supersedes the earlier
  closure. No Step 10 changes. All six remaining work items above remain tracked.

## Verification

27 new cases cover documented signed request/alias, source errors and stop behavior,
independent source continuation, redaction, identity mismatch, duplicate ticker
mapping, schema/HTTP-200 failures, response resource cleanup, quarter/YTD retention,
pacing, no redirects, contact validation and immutable observation files.

- `.venv/bin/python -m pytest -q -W error`: **715 passed in 3.76s** (Python 3.12).
- `../verify-python314/bin/python -m pytest -q -W error`: **715 passed in 3.70s**
  (Python 3.14).
- `git diff --check`: passed. Codex self-review only.

## Actual-host dependency and current stopping point

Run the documented `desk.earnings_sources` command on the iMac once and retain its
report/observation files. No market-open requirement. This workspace received SEC
403s and has no configured Webull credentials; schema/access cannot be certified
here. Do not guess a production normalizer for an unseen income/alert payload.
This is completion of the collector implementation, not source acceptance or
Step 09. Source collection result is needed to choose/verify normalization; items
2–6 are still pending, including fallback choice if the actual host also fails.

## Follow-up 09c

The iMac source report subsequently confirmed SEC access and populated Webull
calendar/alert responses for all three companies; income was empty. See
`09c-source-normalization.md` for the eight researched interpretation repairs,
new mapper and actual-host dependency. Earlier source-access stopping statements
above are historical. Complete Step 09 exit criteria remain open.
