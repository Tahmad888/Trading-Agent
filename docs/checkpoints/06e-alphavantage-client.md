# Step 06 follow-up E — Read-only Alpha Vantage client

Status: local client verified; successful authenticated data remains unverified.
Base: `dffb60f`. Impact record below was written before implementation.
Serves watch/analyze. Continues Taz's authorized Step 06 provider repairs; no
Step 07 work, deployment, subscriptions, orders or schedules. One implementer,
Codex; self-review.

## Evidence and scope

**Checked by Claude, reported to Taz (not repeated here):** 2026-10-01 direct
REST DIVIDENDS/NVDA returned HTTP 200 with an Information rate-limit response
that echoed the API key. The earlier SPLITS reply was discarded; its result is
unknown. No successful direct REST data response was obtained. Connector data
reported split/dividend samples, missing date strings and a zero dividend.
The provider's echo does not prove credential validity. Shared connector quota
and quota-reset time remain unknown; no midnight reset is assumed.

**Sourced:** documented GET /query with function SPLITS or DIVIDENDS, symbol,
datatype=json and apikey. Standard free allowance is 25 calls/day.
https://www.alphavantage.co/documentation/#dividends
https://www.alphavantage.co/documentation/#splits
https://www.alphavantage.co/support/

Implement a bounded client plus diagnostic CLI: environment-only key; fixed host;
no credential-bearing redirects, raw error bodies/URLs or exception chains in
diagnostics; HTTP-200 error detection, response cleanup, no retries, stop this
client after a rate limit. Validate symbol and data envelope. Parse event dates
and finite economics, flag zero amounts rather than silently dropping records.
Successful rows are source observations, never automatic PriceBasis coverage.
No assumed event identity, security mapping, currency, split basis or adjustment.

Affected producers/consumers: new Alpha Vantage request client and read-only
check command, .env.example, regression tests and current checkpoint/tracker.
Existing Webull client, scanners, action contracts and trading policies stay as
implemented. Full real producer/mapping/coverage/rebuild integration remains open.

Acceptance: reported 200-error fixture rejects without leaking a synthetic key;
malformed/empty-error/mismatched-symbol replies fail closed; HTTP/network errors
are safe and bodies close; first rate limit prevents subsequent calls; successful
synthetic split/dividend observations preserve dates/economics and mark anomalies;
probe stops at failure and never marks coverage complete. Full strict suites on
local Python 3.12 and 3.14 before the authorized checkpoint push.

Plan B: return a safe unavailable result, preserve previously saved evidence and
defer an explicitly initiated direct check until quota/access is available. No
automatic retries or stale-data promotion. Rollback: revert this additive client,
probe, tests and documentation together; no trade-state migration.

## Implemented and verified

- `alphavantage.py`: direct read-only request client for SPLITS/DIVIDENDS. All
  provider errors use fixed diagnostic codes. HTTP error bodies close; provider
  exception chains are suppressed from normal tracebacks. Credential-bearing
  redirects are refused. Information/Note/Error Message fields reject even with
  HTTP 200 or a simultaneous data field. Rate limits prevent subsequent requests
  on that client instance. No retry or automatic reset guess.
- Successful envelopes require matching symbols and a data list. Dates and exact
  decimal values are parsed; malformed/duplicate events reject, zero dividends
  remain flagged observations. Optional literal "None" dates become missing,
  not invented dates. No currency, security mapping, adjustment basis, event IDs
  or coverage is inferred. No corporate-action ledger is published.
- `alphavantage_check.py`: one-request option; at most four distinct requests
  with defaults, stopping on the first failure. Output uses fixed/validated
  fields, never raw error payloads or URLs. Optional parsed records are suitable
  for inspection but not a substitute for full provider evidence acceptance.
- Added the environment-variable name with no value, the user's redacted error
  fixture, and 39 tests. Success envelopes are explicitly synthetic/provisional.

Actual local results on 2026-10-01:

```
.venv/bin/python -m pytest -q -W error
# 465 passed in 1.73s (Python 3.12.14)
../verify-python314/bin/python -m pytest -q -W error
# 465 passed in 1.79s (Python 3.14.6)
```

No user-key Alpha Vantage request was made here. The public documentation demo
requests returned Information notices, not usable success samples. Upstream
inspection initially failed under the session's network restriction; publication
requires rechecking remote state after network access is granted. Self-review;
no independent reviewer. Rollback remains a bounded additive revert.

Taz subsequently replaced the key in Claude. This does not prove quota reset or
credential validity. The next direct check can run in Claude's environment; the
iMac is not a prerequisite:

```
python -m desk.alphavantage_check --symbols NVDA --functions SPLITS --include-records
```

One request maximum. Stop if unavailable. If successful, the three remaining
symbol/function combinations can be checked once; no repeated quota probing.
Do not recreate keys to assume additional quota, buy access or schedule retries.
The same adapter can later be verified on the iMac. Source identity mapping,
persistent evidence reconciliation, accepted coverage and actual price/signal
rebuilding are not implemented by this observation client. Volume-definition and
regular-session checks remain separate; Step 07 stays paused.
