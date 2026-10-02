# G3a — targeted rebuild after accepted history revisions

Base bb75602; 2026-10-01. User asked for research before implementing revision
rebuild and automatic volume fixes. Claude is implementing G4 separately. This
follow-up uses an isolated branch; do not overwrite his branch or claim G4 review.

## Before-code record

Requirement: a history revision must retire the old signal and promptly re-run
its detector on validated replacement history, without replaying past crossings.
A revision alone does not imply that the replacement still meets the setup card.
Do not rescale saved levels or change thresholds. Watch/analyze workflow.

Sourced: QuantConnect documents reset/warm-up on newly adjusted history after
splits/dividends, and Algoseek documents re-downloading a security's rebuilt data:
https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/key-concepts#11-Reset-Indicators
https://us-equity-market-data-docs.s3.amazonaws.com/algoseek.US.Equity.Trades.Only.Adjusted.Minute.Bar.pdf
These support the design, not a universal retail convention or proof it is best.

Checked: scanner.run handles next-close preparation and new user additions, but
has no persisted retry queue for revised existing signals. G3 invalidation already
protects old events and records a no-replay boundary. Add a per-candidate queue,
batch re-detect only affected symbols, retain unaffected armed signals, and retry
unavailable sources once per ordinary scheduled scan. No added scheduler or loop.

Affected: typed daily-history revision exception; signal_state additive SQLite
queue and atomic replacement; scanner targeted detector call/integration; tests.
No risk/approval contract edits. Preserve existing EP/volume/fundamental checks.

Acceptance: immediate queued rebuild with a real detector, no automatic trigger
on old crossing, future crossing accepted, no duplicate replacement after restart,
source outage/recovery, ticker/setup isolation, no-longer-qualifying replacement,
missing evidence remains pending, real gaps untouched. Strict full suites on both
Python versions; local fixtures distinct from provider observations.

## Research limits affecting the second requested fix

A daily/raw price conflict still blocks upstream of revision revalidation. Real
ex-dividend data can have newly adjusted D and unchanged historical raw M15; the
older synthetic G3 dividend fixture changed both. Add a realistic counterexample;
this rebuild does NOT by itself resolve that price-channel conflict.

Unchanged volume history does not prove comparable share units. TradingView
explains EOD/intraday feed differences; Kullamagi compares early activity with
average daily volume. Neither documents Webull's historical volume adjustments:
https://www.tradingview.com/pine-script-docs/faq/other-data-and-timeframes/
https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/
Algoseek documents separate price/volume effects; its convention is NOT a Webull
guarantee. Alpha Vantage's documented adjustment description even differs from
Algoseek on cash dividends, so do not universalize a provider convention.
https://www.alphavantage.co/support/

The existing Alpha Vantage SPLITS adapter can obtain split history, but the free
25-request/day quota cannot refresh 30–50 symbols daily. A cached/budgeted source
needs identity/coverage/latency validation; lack of changes alone cannot renew it.
No new automatic volume attestation is justified by the evidence available here.
Volume repair remains open; record an exact provider acceptance procedure separately.

Rollback: revert this code; retain SQLite audit/queue tables. They are additive.
No runtime activation. Do not close G3 price-conflict or volume-source limitations.

## Scope extension before code — 2026-10-02

Both requested fixes remain active. Research found a batch alternative to the
Alpha Vantage per-symbol quota: Massive's documented `/stocks/v1/splits` and
`/stocks/v1/dividends` include the free Stocks plan, daily updates, two years of
history and pagination up to 5,000 rows/page. Query the market-wide split window
and today's dividends; filter locally against freshly pinned Webull identities.
https://massive.com/docs/rest/stocks/corporate-actions/splits
https://massive.com/docs/rest/stocks/corporate-actions/dividends
https://massive.com/stocks
This is a sourced candidate, NOT checked account access or a real-time guarantee.
No registration, purchase or activation is authorized/implied.

Implement an opt-in read-only batch source, persistent same-day cache and shared
5/minute request accounting. A failed/incomplete/stale feed never publishes empty
coverage. Fully paged empty results mean provider-reported absence in the queried
interval, not universal corporate-action completeness. Preserve the provider's
stock-dividend split classification, IDs, dates and ratios. No adjusted volume is
imported from Massive. No viaNexus dataset-membership classification is used.

A single explicit host-level native-volume policy replaces daily hand-enrolment;
this accepts a bounded inference across the same documented US share channels,
not undocumented vendor volume adjustment. It is opt-in and needs provider
acceptance before use. Only rows at/after the latest reported share-change event
within the fetched interval are accepted. Older/longer windows stay unavailable;
50 sessions and the EP threshold are unchanged. Native D and RTH remain distinct.

For price pairing, reconcile only an ordinary action effective after the latest
completed daily anchor and on the current session: either one USD cash dividend
(raw anchor close minus original cash amount), or one reported split (raw close
/ new-to-old share ratio). Require the observed daily close to match to existing
6-decimal tolerance. Combined/ambiguous events remain unavailable. Retain original
bars untouched and embed source economics/receipt in the price evidence. The
multiplicative ordinary cash/split convention is Checked only by the supplied
NVDA/SPY report; its application to the same API is an explicit engineering
inference, not a promise about every event or provider. No factor inferred from
prices. No cumulative historical Massive factor transplanted into Webull.

Acceptance additions: realistic D-only dividend adjustment through rebuild/new
crossing; missing/wrong amount/date/currency rejection; split boundaries; fully
paged batches, failure on last page, rate limits and secret handling; first-ever
volume coverage on a no-split window; restart/date renewal, ticker isolation;
actual host/source probe remains a separate external requirement.

## Implementation checkpoint — 2026-10-02

Both paths are implemented locally, not operationally accepted. See
`docs/G3_FOLLOWUP.md` for source limits and exact host-check requirements.
Changed: additive persisted revision queue and atomic targeted replacement;
ordinary action anchor proof/model validation; safe optional batch source/cache;
volume-policy integration; read-only probe; env placeholders and regression tests.
No risk, approval, setup-card thresholds or active configuration changed.

Verification on original bb75602 base (PYTHONPATH=src so the worktree source,
not the neighboring editable-install source, is imported):
- Python 3.12: `../Trading-Agent/.venv/bin/python -m pytest -q -W error`:
  933 passed in 18.85s.
- Python 3.14: `../verify-python314/bin/python -m pytest -q -W error`:
  933 passed in 15.47s.
- New focused suites: 41 passed in 9.10s under Python 3.14.
- `git diff --check` and probe `--help` pass.
Python 3.14 initially exposed an unclosed adapter-constructor SQLite connection;
fixed with explicit closure before the final passing runs. Warnings not suppressed.

Checked synthetic outcomes: D-only cash-dividend adjustment rebuild/new crossing;
no replay/restart duplicate; outage recovery; withdrawal/no-longer-qualified setup;
required volume pending; price-only survival; fifty tickers share one split request;
post-split windows, stock-dividend classification, cache/date/revision renewal;
pagination/last-page failure, bad-row ticker isolation, rate budget and safe errors.
These are engineering tests, not live-provider or profitability observations.

Remote repair branch advanced to d1e4514 (Claude G4); no overlap with these source
changes. Integration will be checked on the isolated follow-up branch based on
that revision. This does not audit or close G4/G5. Do not overwrite his branch.

### Final combined verification

Integrated onto G4 d1e4514 on the isolated branch, with no source conflicts.
Final additional hardening isolates broken optional-cache storage and malformed
pagination URLs from otherwise valid prices. Added explicit regressions.
- Python 3.12 strict combined suite: **975 passed in 19.87s**.
- Python 3.14 strict combined suite: **975 passed in 16.40s**.
- No warnings suppressed; `git diff --check` passes.
Remote repair branch remained d1e4514 when checked before publication.
Publish only `codex/g3-revision-followup`; user/Claude may review and integrate
its one G3 commit. No actual Massive key/source response was available here.
The two requested fixes remain operationally OPEN pending the provider checks
in `G3_FOLLOWUP.md`; Step 09 remains paused and G4/G5 audit is separate.
