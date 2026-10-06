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
