# G5a CP3 — quote-source and clock check

2026-10-05. Authorized by Taz's request to proceed with the researched solutions.
Base: `6cbc3e982a05f88cfa53b5e22ef6439dc3d18e54` (local and GitHub branch agree).
Hierarchy stays G5 → G5a parent Checkpoint 3 → children 1–6. This is the next
bounded source diagnostic supporting children 4/5; no new parent checkpoint.
It serves watch/analyze/plan by finding a usable source of live bid/ask evidence.

## Requirements before code

- **Checked:** retained tastytrade live rows have zero bidTime/askTime; the accepted
  field list includes both. The quote policy correctly withholds those rows.
- **Checked, limited:** Claude's saved Webull connector summary has bid, ask,
  quote_time and instrument_id for SPY/QQQ/NVDA. It has no request receipt bracket
  or full source response, so it cannot establish current executable BBO coverage.
- **Sourced:** Webull's retail snapshot endpoint is GET
  `/market-data/stocks/snapshots/list`; it describes a real-time stock snapshot.
  https://developer.webull.com/apis/docs/reference/snapshot/
- **Sourced:** dxFeed side times describe the last bid/ask change. Missing source
  time cannot be repaired by assigning our receipt time.
  https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Quote.html
- **Sourced:** Apple supports network-time synchronization; NTP separately measures
  offset and uncertainty. Enable/verify host synchronization before proposing a
  future-time tolerance.
  https://support.apple.com/guide/mac-help/set-the-date-and-time-automatically-mchlp2996/mac
  https://www.rfc-editor.org/rfc/rfc5905

Affected producer: existing read-only Webull metadata and snapshot client.
New consumer: diagnostic report only, plus the existing saved host-clock report.
No scanner, quote service, risk/ticket policy, historical volume, source stores,
credentials, scheduler or account/order endpoint changes.

1. Resolve fresh Webull metadata once; group snapshot requests using the resolved
   COMMON_STOCK/ETF classification, preserving the reviewed BRK.B provider alias.
2. Record request-start and response-receipt UTC separately. Associate each row
   with exactly one requested canonical symbol and the metadata instrument ID.
   Duplicate/mismatched/missing identities are explicit; they cannot become usable
   by returning another healthy ticker. Keep valid peers' observations.
3. Record only whitelisted numeric/source-time fields and their states. `quote_time`
   is an observed snapshot field, **not** an assumed bidTime or askTime. Its numeric
   epoch-millisecond interpretation follows the retained sample; that observation
   does not establish side-change semantics or NBBO coverage. Never substitute,
   clamp, average or infer a missing timestamp. A future timestamp is reported.
4. Bound actual client calls, including metadata pagination, through an allowlist
   of the existing GET metadata/snapshot routes. Refuse HTTP redirects before
   forwarding any signed request; retain standard TLS verification.
   No retry on a denied, limited,
   unavailable or malformed response. Budgets bound this diagnostic, not trading.
5. No raw exception/body/header/credential output. Use the existing credential
   guard for saved reports. Attach clock evidence without applying it or claiming
   a different host's clock is the iMac clock.
6. Output observations, never overall PASS, trade eligibility, NBBO attestation or
   options coverage. Off-hours checks cannot establish regular-session freshness.

Acceptance: stock/ETF grouping and alias identity; correct receipt brackets;
duplicate/missing/wrong IDs; per-symbol malformed prices/times; future and zero
times; provider failures stop further calls; metadata pagination consumes budget;
invalid arguments consume zero calls; reports suppress credentials; no input store
or environment file writes. Existing quote/clock policies must stay unchanged.

Plan B: if Webull cannot supply the required quote contract, probe Tradier production
after account approval. Its documented consolidated real-time stock/option coverage
is a candidate, not verified access for Taz:
https://docs.tradier.com/docs/market-data

Remaining decisions: snapshot quote_time semantics, NBBO/channel coverage, separate
options source, real account entitlement, clock uncertainty policy and exact live
RTH30 volume. Step 09's estimated earnings calendar stays paused. Do not promote
this diagnostic or the existing dayVolume receipt brackets into decision evidence.

Rollback: remove the standalone diagnostic and its documentation/tests; no schema
migration or persisted trading state changes. Carry forward the two close-recovery
repairs and accepted identity-ordering evidence separately.

## Verification

Implemented `src/desk/webull_quote_check.py`, a standalone read-only diagnostic.
It uses a new allowlisted/budgeted Webull client subclass with redirect refusal;
the shared Webull client and every existing decision consumer stay unchanged.
New regression file: `tests/test_webull_quote_check.py` (41 cases). The existing
measurement architecture test now permits the two explicitly named opt-in
diagnostic modules, while continuing to refuse imports into any other consumer.
`CLAUDE.md` and `G5_ACCEPTANCE.md` record the same open scope.

| Actual verification | Result |
| --- | --- |
| Source probe, Webull, measurement, symbols and quote diagnostic tests; Python 3.12.14 | 123 passed in 0.59 s |
| New source probe plus existing measurements; Python 3.14.6 | 74 passed in 0.65 s |
| Combined strict suite, Python 3.12.14 | **1,807 passed in 188.35 s** |
| Four mutations in temporary copies | All caught: removed budget (2 failures), ignored instrument identity (1), fabricated absent/zero time (3), removed redirect handler (1) |
| CLI help and git diff whitespace check | Passed; zero provider calls |

All tests use synthetic data/fake transports; no credential, provider, account,
iMac, scheduler or order operation occurred. The first combined run was stopped
to add redirect refusal before final verification; the final result above covers
the complete code. No full Python 3.14 suite or iMac result is claimed. Local
3.14.6 is distinct from Taz's iMac Python 3.14.7.

Provider/iMac checks remain pending. There are no source credentials available
to this process; use the existing iMac configuration or a separately authorized
Claude cloud diagnostic. Next: capture the host clock and the bounded snapshot
report. Off-hours can establish access/shape only; regular-session observations
and a verified BBO time/coverage contract precede adapter integration. No overall
G5 acceptance or bid/ask eligibility follows from these offline results.

## iMac strict-suite cleanup follow-up — requirements before code

Base: `c0c5e673523a0b47c70ee0886135c6a46aabce2b`. Taz ran command block 1
on Python 3.14.7: **1 failed, 1,806 passed**. Block 2 is held; no provider
diagnostic follows a failed strict suite.

- **Checked:** `test_redirect_is_refused_and_credentials_go_nowhere_else`
  reproduces on local Python 3.14.6. Redirect refusal itself succeeds, but
  `request_json` translates the raised HTTPError without closing its response.
  Its destructor then emits a ResourceWarning, rejected by `-W error`.
- **Sourced:** HTTPError is also a file-like response, not only an exception:
  https://docs.python.org/3.14/library/urllib.error.html
  The local CPython implementation owns a temporary-file wrapper and supports
  explicit close. Existing Webull and Alpaca clients already close translated
  HTTP errors; apply that ownership rule to tastytrade's REST transport too.
- Producer: urllib's error response. Consumer: `tastytrade_transport.request_json`,
  which returns only the existing safe HTTP status refusal. Do not read the error
  body, expose its URL/headers/reason, follow a redirect, retry, or suppress warnings.
- Acceptance: a real synthetic redirect cannot forward a Bearer header and its
  response is closed; injected 302/401/403/429/500 errors and an absent body are
  closed before returning the same safe refusal. Successful-response and transport
  contracts stay unchanged. Run relevant quote/transport tests and the combined
  strict suite on local Python 3.12 and 3.14 before pushing; iMac 3.14.7 remains
  separately pending.
- Rollback: revert only this resource cleanup and its regression/documentation.
  Quote timing, rules, identity, volume, scanner state and source diagnostic scope
  stay unchanged. No provider, account, schedule, iMac or order calls are needed.

### Cleanup verification

`request_json` now explicitly closes HTTPError before translating its status.
Six new cases retain the error object so garbage collection cannot hide the leak;
they check both body and wrapper closure, safe refusal text, no body reads and no
retry. The existing synthetic redirect test also checks response closure and still
proves the redirected destination is never contacted.

| Actual check | Result |
| --- | --- |
| Original iMac failure reproduced, local Python 3.14.6 before repair | 1 failed with the same HTTPError 302 ResourceWarning |
| Six new regressions before the production repair, Python 3.12.14 | 6 failed; existing redirect control passed |
| Quote repairs, tastytrade quotes, Webull source probe and measurement tests; Python 3.12.14 and 3.14.6 | 185 passed on each |
| Full strict suite, Python 3.12.14 | **1,813 passed in 201.78 s** |
| Full strict suite, Python 3.14.6 | **1,813 passed in 175.77 s** |

The combined runs use fake transports and make zero provider calls. Python 3.14.7
on the iMac still needs its own repaired-commit result. Rerun command block 1 on
the updated commit, then review that result before block 2. No source eligibility,
clock evidence, regular-session acceptance or G5 closure is claimed.
