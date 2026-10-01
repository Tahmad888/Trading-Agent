# Step 06 follow-up F — Corporate-action ledger and bar integration

Status: implementation verified locally; operational configuration pending. Base: dc2ae04. Serves watch/analyze.
One implementer, Codex; self-review. No Step 07, orders or activation.

## Impact record (before implementation)

User authorized continuing away from the iMac. Checked: supplied direct REST
observations include NVDA splits/dividends and SPY dividends plus an empty SPY
split response. Source connectivity is accepted for those samples. User policy:
zero-dollar dividends remain logged, have no price adjustment, and do not block.

Checked: supplied 2026-10-01 regular-session report ran dc2ae04 in Claude cloud.
Independent offline audit of the attached 24-call JSON found expected latest
completed AAPL/SPY M1/M15 bars in every response, 8 forming-bar changes and no
completed-bar revisions; true returned one forming row, false none. This accepts
the observed RTH timing only, not subsecond candle publication or other sessions.
The report's additional two count-40 replies/replay code were not attached.

Sourced: Alpha Vantage documents historical splits and historical/declared
future dividends (https://www.alphavantage.co/documentation/#splits and
#dividends). Webull documents forward-adjusted daily and raw minute bars
(https://developer.webull.com/apis/docs/reference/historical-bars/). Neither
an action response nor a ticker match proves bar normalization/identity.

Implement a reviewed security mapping and bounded coverage procedure; import
paired dated split/dividend snapshots into a transactional persistent ledger;
retain zero rows and future events as observations, omit them from currently
effective economic actions; compare terms across revisions, including removals.
Preserve prior generations and mark failed refreshes unusable. Removed actions
require explicit reconciliation rather than silently declaring an empty response
complete. Dates are source-derived event keys: a corrected date is removal/addition.

Connect the ledger to Webull/scanner through an opt-in source wrapper with
explicit host-bound price-channel evidence. Each fetch gets current ledger
coverage; new/corrected/removed events make persisted old signal evidence fail
existing price compatibility checks. Fresh bar fetches and scanner calculations
rebuild evidence; do not invent Step 07 intraday rearming or rescale prices twice.
Price/volume normalization acceptance stays the next checkpoint, so no live
channel config is enabled by this change.

Affected producers/consumers: Alpha Vantage report importer, action terms,
SQLite ledger, bar source wrapper, scanner configuration, tests, docs. Volume
policy and indicator/setup formulas unchanged. No secrets or user environment
files read. No live API calls required.

Acceptance: paired source/date/identity validation; zero rows nonblocking;
empty splits accepted under reviewed source coverage; future events activate
only on effective date and fresh evidence; exact term revisions, restart,
failed refresh, cancellation/removal review, atomic publication; real adapter
replay through bar wrapper and existing signal revalidation; freshly fetched
prices can be recalculated, while cached pre-refresh bars cannot be promoted.
Strict full suite on Python 3.12/3.14, diff check, upstream check, checkpoint push.

Plan B: retain previous evidence for diagnosis, report unavailable and do not
publish stale/incomplete price profiles. Unknown mapping/channel keeps existing
rejection behavior. Rollback: revert the additive ledger/wrapper/CLI hook and
keep its SQLite evidence file; no existing signal/log files are rewritten.

## Implemented

- `action_ledger.py`: explicit reviewed symbol/instrument/currency/exchange and
  bounded history; safe dated report import; paired split/dividend publication;
  SQLite transactions, persistent accepted generations and source snapshots;
  economic revisions, new events, removal reconciliation, restart recovery,
  rejected refresh status and older-source protection. Zero dividends persist
  with ZERO_AMOUNT_IGNORED but create no economic action. Future events persist
  but do not activate early. Same-day refresh is required by the existing
  current-session evidence contract; a receipt is never manufactured for imports.
- `action_import.py`: offline command imports dated, sanitized client reports and
  a reviewed crosswalk. Both sources must pass before publication. Failed imports
  disable publication for the identified symbol, keeping prior evidence. Explicit
  `--accept-removals` is for a reconciled removal/date correction, not quota errors.
- `action_source.py` + scanner: optional ledger-backed Webull source; checks
  configured host, observed instrument ID, regular-session request and receipt
  after ledger publication; attaches the existing PriceBasis contract. No OHLCV
  adjustment occurs. This is correct only with separately reviewed native price
  channel evidence. Unknown volume remains unknown.
- Existing scanner fetch/feature calculations provide the fresh-data rebuild
  path. Entry revalidation rejects old persisted action revisions. This does not
  auto-rearm signals intraday, delete old signals or implement Step 07 lifecycle.
- Preserved the reported NVDA 57-row dividend response and two unmodified calls
  from the RTH log with source attachment SHA-256s. Their original timestamps are
  retained. Replays combine real payloads with explicitly fictional mapping and
  channel reviews; they do not attest live operational coverage.

## Operator workflow (after price-channel review)

Keep state and reviewed config in local ignored `data/` or outside the checkout.
The source client can produce one dated report per explicit request:

```
python -m desk.alphavantage_check --symbols NVDA --functions SPLITS --include-records > data/nvda-splits.json
python -m desk.alphavantage_check --symbols NVDA --functions DIVIDENDS --include-records > data/nvda-dividends.json
```

Run separately with provider-compliant pacing; do not loop/retry on rate limits.
No calls were made during this checkpoint. Existing dated reports can be imported
on their original ET date; older reports are regression evidence, not fresh data.
A failed API report must not be replaced with yesterday's successful report.

```
python -m desk.action_import --review data/nvda-review.json --splits data/nvda-splits.json --dividends data/nvda-dividends.json --database data/actions.sqlite
```

`SecurityReview` requires source/evidence_ref, symbol/source_symbol, Webull
security_id, USD currency, exchange_mic, instrument_name, identity_evidence_ref,
coverage_evidence_ref, coverage_start, valid_through, reviewed_at,
cash_amount_basis="effective_date_per_share", exceptional_actions="none_found"
(or unresolved, which cannot publish). The evidence must actually support these
fields; receipt count/ticker alone is insufficient. No production review file is
created or enabled in this checkpoint.

`DESK_ACTION_LEDGER` selects that database; `DESK_ACTION_CHANNELS` selects a JSON
list of PriceChannelReview objects (source/evidence_ref, host, symbol, D or M15
timeframe, normalization). Native M15 must be unadjusted. Configuring these values
requires completion of the remaining Webull price/volume acceptance, not copying
fictional tests. There is no automatic credential access, action polling, scheduler
or scanner activation. Free-tier capacity for the eventual full universe remains
an operational concern; this checkpoint makes zero extra authenticated requests.

## Remaining acceptance

Corporate-action integration code now reaches scanner validation. Real operational
mapping/history/channel configuration still needs acceptance with the next price/
volume checkpoint. Historical raw minute series crossing an action still reject
until explicitly normalized; this bridge does not invent a normalization formula.
The iMac must later verify its installation/configuration. Step 06 remains open,
Step 07 paused. RTH sample timing and four source connectivity checks are accepted;
no need to repeat them merely to establish source access.

## Verification and publication

Actual local results, 2026-10-01:

```
.venv/bin/python -m pytest -q -W error
# 492 passed in 1.87s (Python 3.12.14)
../verify-python314/bin/python -m pytest -q -W error
# 492 passed in 1.96s (Python 3.14.6)
git diff --check
# passed
```

27 new tests; 465 previous tests continue passing. Includes the scanner refusing
an old persisted signal after a corrected dividend, successful adapter/bar-filter
replay, and failed paired imports disabling the previous generation. No new live
calls, credentials, orders, schedules or iMac actions. Self-review only.
Upstream was fetched and matched dc2ae04 before editing; recheck before push.
Publication is authorized on codex/repair-step-01-baseline only, without merging.
