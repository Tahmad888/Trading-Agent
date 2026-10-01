# Step 09 — Earnings and catalyst evidence

**Correction 2026-10-01:** Step 09 is reopened. The earlier closure below verified
only gates and a reviewed NVDA fallback, not automatic earnings supply. See
[09b-automatic-sources.md](09b-automatic-sources.md) for the remaining work.

Status: closed for normalized gates and the reviewed-input adapter after the iMac
confirmation recorded in checkpoint 09a. Source follow-up is recorded
in [09a-provider-fallback.md](09a-provider-fallback.md): iMac calendar observations,
empty income response and a bounded issuer fallback, with 688 strict tests passing.
The historical pending items below describe this initial checkpoint.
Base 3c6d58b; upstream matched. Serves watch/analyze.
Taz resumed Step 09 after the Step 08 iMac probe passed. Codex implements/self-reviews.

## Impact record before implementation

Checked: chart signals for cup-with-handle and EP say earnings are checked separately,
but no structured reported-results/catalyst gate is wired to scanner or fresh review.
The existing Webull calendar method returns raw dates; it cannot establish actual
quarterly growth, accounting comparability or a pre-trigger catalyst.

User/card policy: EP qualifies on EPS OR sales >=25% YoY, with >=50% a tag only.
Cup-with-handle retains its own latest-quarter EPS >=25% requirement; no sales
substitution there. Zero/negative prior EPS must not fabricate growth. Technical
candidates remain visible when fundamental evidence is absent. No new trading
threshold, news sentiment score, broad backtest or order/approval logic.

Sourced 2026-10-01: SEC EDGAR API documentation describes separate units, fiscal
calendar differences and as-filed disclosures; dates cannot be treated as matching
fiscal quarters merely because they share a calendar quarter:
https://www.sec.gov/search-filings/edgar-application-programming-interfaces
Alpha Vantage documentation separates earnings history, statements and calendars:
https://www.alphavantage.co/documentation/
Official Webull SDK 3.0.2 request files confirm GET v3 earnings-calendars/list and
income-statements/get (symbol, category, type, count). Retrieved wheel in memory;
not installed. https://pypi.org/project/webull-openapi-python-sdk/3.0.2/
These request definitions do not establish returned EPS accounting/share basis or
publication timestamps. Do not invent a raw-payload-to-qualified-evidence mapping.

Implementation: normalized reviewed evidence adapter with source references,
security ID, matched fiscal quarters, explicit quarterly periods, currencies,
scales, metric/accounting/share bases, publication/receipt times and dated review
validity. Latest reported quarter must be attested; no cherry-picking older growth.
Decimal arithmetic for boundaries. Compare EPS and sales independently so unknown
EPS cannot suppress known qualifying sales. Calendar estimates/confirmations and
conflicts are separate display evidence. Catalyst must be explicitly linked to
entry session, published by trigger time and received by evaluation time; an
observation cannot retroactively claim an earlier qualification. Revisions change
evidence fingerprints and are reevaluated at fresh review.

Integration: technical signals/events stay durable; scanner records separate
fundamental qualification for triggers and `revalidate_signal` enforces required
gates. Do not mutate technical event identity or promote persisted eligibility to
trade approval. A configured local evidence JSON is reloaded on each evaluation;
no source configured/malformed/stale evidence means PENDING for required setups.
Provide a read-only Webull calendar/income probe for real payload mapping; no keys,
source facts or reviewed production bundles are invented or committed.

Acceptance: 24.9/25/50 boundaries, negative/zero EPS baseline, sales-only EP,
cup EPS-only, quarter/year/currency/accounting/share mismatches, scaling,
missing/latest-quarter conflict, future/revised evidence, post-trigger catalyst,
calendar estimated/confirmed/conflicting, source failure, persisted trigger and
fresh review behavior, both strict Python suites. No independent review claimed.

Plan B: keep technical candidate visible with explicit missing evidence; never
qualify from an upcoming date/estimate. Provider automatic normalization remains
unavailable until actual payload semantics are verified. A reviewed evidence file
is an explicit supported adapter, not a claimed automatic news/filings service.
Rollback code/docs together; retain scan evidence records and signal history.

## Implemented checkpoint

- `earnings.py`: strict normalized reviewed input, Decimal growth, fiscal/metric
  comparability, dated source/review evidence, next-date confidence/conflicts,
  catalyst cutoff and content fingerprints. No current production bundle added.
- `ReviewedEarningsSource`: opt-in reloaded JSON source via DESK_EARNINGS_EVIDENCE;
  source errors expose no raw exception text. This is a working explicit reviewed
  source, not a claim that Webull supplies every required field automatically.
- Scanner candidate/trigger reports include separate fundamental qualification.
  `revalidate_signal` now enforces required gates using the original event cutoff;
  `earnings-reviews.jsonl` retains the facts/evidence fingerprint used at each review.
  Technical events do not change identity when fundamental evidence changes.
- Next earnings UNKNOWN/CONFLICT remains visible; no unapproved global blackout.
  Cup EPS-only versus EP EPS-or-sales semantics remain distinct. Card values and
  frozen card fingerprints were not changed; legacy growth helper ignores nonfinite
  values instead of accidentally giving Infinity a strong-growth tag.
- Webull quarterly income route and `desk.earnings_probe` collect raw calendar and
  five-quarter observations for mapping. Probe redacts configured Webull secrets,
  stops on transport errors and never outputs decision eligibility.

## Verification

49 new synthetic tests cover the planned boundary/unknown/revision cases, source
and request behavior, pending candidates, and required-gate fresh review. The
full prior suite also passes. No actual earnings records or authenticated Webull
calls were obtained from this Codex workspace.

- `.venv/bin/python -m pytest -q -W error`: **670 passed in 3.40s**, Python 3.12.14.
- `../verify-python314/bin/python -m pytest -q -W error`: **670 passed in 3.46s**,
  Python 3.14.6.
- `git diff --check`: passed. Codex self-review only.

## Remaining before Step 09 closure

Run `python -m desk.earnings_probe --symbol NVDA` on the credentialed iMac after
pulling this checkpoint. Inspect actual calendar/income schema and access, including
actual versus estimate, fiscal period, currencies/scales, GAAP/adjusted and EPS
share basis, publication time and revision semantics. Map only established facts;
missing fields require supported issuer/filing evidence or remain pending. Raw
OBSERVATIONS_ONLY is not a qualification PASS. No profile is enabled simply by
running this probe. Do not advance to Step 10 yet.

Rollback: revert coordinated adapter/scanner/docs; retain existing signal store
and earnings review logs. No SQLite schema change, orders, purchases or scheduled
runner activation occurred.
