# Step 06 follow-up D — Provider failures and action evidence

Status: bounded local repairs verified; provider acceptance remains open. Base: `d1e96b1`.
The impact record below was written before implementation.
Serves watch/analyze. Taz authorized repairing the failures in Claude's off-hours
report. One implementer, Codex; self-review. Step 07 remains paused.

## Findings and bounded work

Reported evidence: `step06-offhours-provider-checks-2026-10-01.md` tested sandbox
HTTP at `d1e96b1`. A corporate-action path returned 404; 1.05-second pacing produced
429; dividend data was accessible but incomplete; native daily high differed from
RTH aggregates in two samples. No live requests have been repeated by Codex.

Sourced: the corporate-action list is documented under **Display Solution**,
not the retail non-display product. Official SDK environment docs distinguish
server-to-server and client-to-server hosts. This is evidence of a product/route
mismatch, not proof the existing retail key will work on a different host.
Do not switch credentials/hosts automatically or invent an alternative endpoint.
https://developer.webull.com/apis/docs/reference/broker-market-data-api/corp-action-using-get/
https://developer.webull.com/apis/docs/sdk/

Sourced: market-data sandbox limits are 30/60s versus production 60/60s, keyed
by app and endpoint. Repair host-aware default pacing; retain explicit override
for isolated tests. 429 may still occur when processes share a key.
https://developer.webull.com/apis/docs/rate-limits/

Planned changes: typed HTTP diagnostics that preserve host/path/status without
secrets; safe sandbox pacing; documented retail dividend/fund action reads as
partial evidence only; a reproducible read-only probe for the credentialed host.
Correct composed-bar descriptions to cover high/low/close as well as open/volume.
Build deterministic event revisions and evidence handling only from explicit
source fields; no vendor version counter is inherently required. Scope each
coverage attestation to a documented interval/source procedure, not a universal
promise that an event can never be missed. No automatic live eligibility.

Affected producers/consumers: Webull request client, diagnostic CLI, price/action
contracts and composition metadata, scanner evidence and regression tests.
Taz delegated the volume-baseline decision to research. Decision: retain native
daily volume as the existing 50-day denominator; do not substitute summed RTH
history solely to make aggregates equal. See the research section below.
No thresholds, risk rules, subscriptions, order interfaces or runner schedules
are changed. Real Webull comparison eligibility remains unverified.

Acceptance: sandbox/production pacing with fake clocks; HTTP 404/403/429 diagnostics
and response cleanup; supported partial-evidence requests and malformed replies;
unknown/full-coverage separation; stable revisions under formatting changes but
changed economic values produce changed revisions; snapshot fields cannot imply
provider daily equivalence. Full strict Python 3.12/3.14 suites before publication.

Plan B: retain precise unavailable/unknown results and require provider entitlement
or supported source access; do not retry 404 as a timing problem or label an empty
dividend response as complete corporate-action coverage. Rollback: revert bounded
code/tests/docs together; no persistent trade-state migration.

## Research decision: EP volume

**Sourced:** Qullamaggie describes exceptional opening volume relative to average
daily volume, ideally reaching that average within 15–20 minutes. His page does
not establish this desk's 50-day/0.5/30-minute combination, or prescribe replacing
daily bars with aggregated RTH minute bars.
https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/

**Sourced:** Trade Ideas uses a same-time historical comparison for its Relative
Volume filter. That answers a different question from fraction of a normal full
day; it should not silently replace EP's current metric.
https://www.trade-ideas.com/help/filter/RV/

**Sourced:** TradingView documents daily/intraday feed differences, including
trades reported only in the end-of-day feed and daily highs not present in
intraday history. This establishes that unequal totals can be legitimate, not
which filters Webull actually applies.
https://www.tradingview.com/pine-script-docs/faq/other-data-and-timeframes/

**Decision/inference:** retain first-30-minute RTH volume / prior 50 native daily
volumes' average, with the existing 0.5 threshold (**Assumption**). Label this
as a fraction of average daily volume, not same-time RVOL. Do not lower the
denominator by constructing an RTH-only series just to get past a data check.
This preserves the setup's intent; it does not establish profitability or
Webull compatibility. Confirm session/trade coverage, split share units and
finalization before enabling a real profile. Equal aggregate totals are not an
acceptance requirement. If documented differences require an explicit permitted
source-pair comparison, amend producer and consumer contracts together; do not
fabricate matching definition IDs. The current unknown-data guard remains.

## Findings tightened after Claude's report

- A 404 is an unavailable route on the tested host, not proof that no retail
  source exists or that live funding will fix it. Display/Broker access is separate.
- A missing vendor version counter is not a blocker by itself. The new
  `ActionRecord.to_action()` creates a deterministic content revision from
  reviewed terms and binds security/currency to the price producer. Exact numeric
  formatting and receipt-reference changes preserve it; economic corrections
  change it. It is a conversion tool, not a connected vendor adapter.
- Coverage is a bounded producer attestation backed by the procedure in
  `DATA_BASIS_ACCEPTANCE.md`, not a claim that a vendor can never miss an event.
- Native daily high differed from RTH high in 2/10 reported symbol-sessions.
  All composed O/H/L/C/V fields now identify their origin. Reported matching
  low/close samples do not prove universal equivalence.
- The six-date adjustment fit is useful reported evidence, not a general
  adjustment implementation. Agreement with another vendor is not independent
  confirmation unless the upstream sources are known to differ.

## Implementation and verification

- `webull.py`: sandbox default 2.1 seconds and production 1.05 seconds between
  completed requests to the same endpoint; first request has no artificial wait.
  Explicit test overrides remain. This is client-local pacing, not a shared
  multi-process quota manager. Added status/host/path diagnostics and response
  cleanup; no retries on guessed hosts. Added partial dividend/fund reads.
- `provider_check.py`: bounded read-only diagnostic, counts/field names only;
  successful or empty replies never become complete coverage.
- `action_evidence.py`: reviewed term validation, exact economic revision hash,
  security/currency binding. Unknown/cancelled/unsupported terms cannot publish
  active supported actions. Source parsing, conflict resolution and coverage
  publication remain the unconnected producer's responsibility.
- `bar_contract.py`: composed high, low and close metadata added alongside open
  and volume. No feature math or setup thresholds changed.
- Added 35 automated cases, including end-to-end compatibility rejection after
  a dividend correction and acceptance of a rebuilt synthetic basis.

Commands run on 2026-10-01:

```
.venv/bin/python -m pytest -q -W error
# 426 passed in 1.88s (Python 3.12.14)
../verify-python314/bin/python -m pytest -q -W error
# 426 passed in 1.87s (Python 3.14.6)
git diff --check
# passed
```

Self-review, not independent review. Remote branch remained at `d1e96b1` before
publication. No authenticated Webull request was made here: credentials are on
the iMac/Claude side, not in this execution environment. These runs neither
verify the new routes on that host nor attest real action coverage. No orders,
runner activation, support messages or subscription changes were made.

Next checkpoint: run the small route probe from the credentialed host; obtain a
documented accessible action source and identity mapping, resolve feed definitions
and incorporate the separate regular-session report. Unknown data remains
ineligible. Step 07 stays paused.

Additional source for fund-request category:
https://github.com/webull-inc/webull-openapi-python-sdk/blob/main/webull/data/quotes/fundamentals.py
