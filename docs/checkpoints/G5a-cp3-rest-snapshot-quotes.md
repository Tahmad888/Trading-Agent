# G5a CP3 — timestamped REST quote snapshots

2026-10-05. Taz authorized the researched timestamp repair. Base `c3c661c`;
local checkout and dedicated remote agree. G5 → G5a parent Checkpoint 3 →
children 5/6. Step 09, clock tolerance and live RTH30 volume remain separate.

## Requirements before implementation

Checked: Webull's diagnostic already extracts the observed `quote_time`; extraction
is not the gap. The accepted tastytrade DXLink capture supplies zero side times.
Sourced: dxFeed side times are last-change times, not proof of transport delay.
Sourced: tastytrade's read-only `GET /market-data/by-type` returns bid/ask and
`updated-at`; the reference defines it as an ISO-8601 quote-update time. REST is
appropriate for one-off snapshots; streaming is preferred for continuous updates.

- https://developer.tastytrade.com/docs/concepts/market-data/
- https://developer.tastytrade.com/reference/market-data/getMarketDataByType/
- https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Quote.html

This bounded stage adds an immutable, distinctly labelled snapshot producer and
read-only diagnostic. It does not replace streaming Trades or connect a new BBO
to ticket approval before provider acceptance. Unknown snapshot times are not
replaced with receipt, and quote-update times are not called bid/ask change times.

Producer: existing `ReadClient`, extended only with exact GET market-data route and
encoded query; new snapshot normalizer. Consumer: new diagnostic only. Existing
QuoteService, signal revalidation, risk/ticket inputs, mappings, volume and
operational schedules are unchanged. New diagnostic bounds are engineering
request/runtime limits, not trading rules. No persistent snapshot cache is added:
a failed refresh has no current result and cannot reuse a previous success.

1. Validate requested resolved instruments before transmission; equities and OCC
   options use separate parameters, actual provider symbols and the documented
   combined 100-symbol maximum. Retain TLS, redirect refusal, request accounting,
   OAuth read scope and safe HTTP error cleanup. No account/order endpoints.
2. Resolve identities through existing instrument routes. Match reply rows by
   canonical symbol and exact supported instrument type. Isolate malformed peers,
   missing rows, duplicates and unexpected identities. Retain Decimal fields.
3. Parse timezone-aware ISO quote-update timestamps; retain provider timestamp,
   request-start and response receipt separately. Explicitly handle the documented
   dasherized fields and reference camelCase aliases; conflicting aliases refuse.
   Invalid/zero/future times refuse normalization. Do not add clock tolerance.
4. Record snapshot source/environment/identity, timestamp meaning and evidence
   digest. Coverage is NOT_ATTESTED; a symbol match is not a reviewed cross-provider
   mapping. Halt true cannot be active; missing status is unknown. Bid/ask sizes
   retain provider units, not assumed shares/contracts/lots.
5. Report quote-update age under the existing 60-second policy separately from
   timestamp validity, receipt duration and live-session scope. Off-hours may
   establish schema/access only. Never output overall PASS or decision eligibility.
6. Diagnose two bounded rounds of equities and an optional listed call/put pair.
   Use an explicit strike, or a fresh observed underlying snapshot midpoint, only
   for diagnostic selection. No fabricated contracts or automatic median fallback.
   Any transport/auth/rate-limit/budget/shape failure stops further requests; prior
   rounds remain labelled history and do not become a current result. Symbol-specific
   identity failures isolate. Reports suppress credentials and raw bodies/headers.

Acceptance: stock and actual option control, timezone offsets, preserved decimal
precision, unchanged prices with a new update timestamp, old time/new receipt,
naive/missing/zero/future times, wrong type/symbol, duplicates/conflicting aliases,
bad peer, failed refresh after success, wrong nested identity, halt/unknown status,
locked/crossed/zero-size book, budgets/auth/redirect, share class versus OCC encoding,
option selection and denial stops, off-hours labelling and credential rejection.
Combined strict suite required because the shared REST client changes.

Unresolved provider acceptance: unfunded account's REST access, stock/option wire
fields and live freshness; any claim of consolidated coverage; Webull stock
timestamp contract. No current provider acceptance is inferred from synthetic
tests. After this stage, run the diagnostic on the iMac and review evidence before
implementing any snapshot-aware approval consumer. No subscription purchase.

Rollback: revert this stage's files/callsite additions. No schema migration,
environment change, stored trade state or host service exists to undo.

## Implementation and verification

- `tastytrade_transport.py`: exact GET route, encoded typed symbol groups,
  documented 100-symbol batch limit. Existing read-scope OAuth, TLS, redirect
  refusal, safe error disposal and request counting remain in use.
- `snapshot_quotes.py`: immutable decimal observations, strict zoned provider
  time, explicit schema aliases, contradictory identity/alias refusal, per-symbol
  isolation, distinct source/time meaning and capture digest. No cache or trading
  consumer; sizes and coverage remain unattested.
- `snapshot_quote_check.py`: identity resolution, two bounded fresh rounds by
  default, optional actual listed call/put resolution, final-time age checking,
  safe report output. A failed refresh cannot recover an earlier round as current.
  A stale option reference leaves stock observations available; provider stops
  stop the whole diagnostic without retrying. No stream or account endpoint.
- Tests: `test_snapshot_quotes.py`; the explicit diagnostic-only importer list
  in `test_quote_measure.py` includes this new diagnostic. An additional importer
  check proves snapshots have only that diagnostic consumer. Updating the list
  does not allow a risk/scanner/ticket consumer to import measurements.
- Documents: this checkpoint, `TASTYTRADE_QUOTES.md`, `G5_ACCEPTANCE.md`, `CLAUDE.md`.

An initial targeted run found that a conflicting size alias was reported as a
generic numeric error. Alias extraction now happens before numeric validation;
the regression checks the distinct refusal. The initial full runs each found
only the older diagnostic-importer list (1 failed, 1,870 passed); that list was
updated explicitly, and the stricter snapshot-consumer check was added. Final
strict runs are recorded below, separately from those initial failures.

| Actual check | Result |
| --- | --- |
| Snapshot, existing quote service, repairs, re-audits, diagnostic, risk and Webull quote checks; Python 3.12.14 | 304 passed in 42.15 s |
| Current snapshot and measurement checks; Python 3.12.14 | 92 passed in 0.97 s |
| Three deliberate safeguard removals in scratch copies: receipt substitution, future-time acceptance, row-identity bypass | All 3 caught by their regressions |
| Final full strict suite, Python 3.12.14 | **1,872 passed in 202.03 s** |
| Final full strict suite, Python 3.14.6 | **1,872 passed in 178.10 s** |

Both full commands: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src:$PWD"
<python> -m pytest -q -W error`. There are 59 new snapshot regressions, compared
with the accepted 1,813-test base. The iMac Python 3.14.7 run on this new code is
still pending; its earlier 1,813-test result covers `c3c661c` only.

The production implementation was not changed after the final full runs began;
later edits are documentation only. All test transports are synthetic; zero
market-data provider calls, credentials, host changes, schedules or orders in this
batch. Provider acceptance and a separately reviewed approval consumer remain the
next requirements, not implicit consequences of passing tests.
