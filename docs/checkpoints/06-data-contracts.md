# Checkpoint 06 — Bar timing, provenance and exchange calendar

Status: complete and verified locally. Base: `af5c6b6`. Function: watch/analyze.
Implementer: Codex; no independent review claimed.

## Impact record before implementation

Replace hand-maintained 2026–27 holidays and full-day-only scheduling with a pinned
XNYS exchange calendar, checked against NYSE's published 2026–28 schedule. Calendar
coverage must be bounded; do not extrapolate unverified distant dates. Use actual
session closes for early-close scan slots and bar completion; timezone conversion
must preserve DST. Build hourly/weekly OHLCV only from complete constituent bars.

Bar contract: timezone-aware start timestamps; daily session labels normalized to
ET midnight; regular-session prices; explicit source/timeframe/delay/adjustment and
price-scale provenance. Missing semantics cannot become an assumed real-time feed.
Raw numeric timestamps require an explicit unit. Reject naive/duplicate/out-of-order,
nonfinite/misaligned/gapped/stale data. Ignore forming/future bars in signal evidence.
Validate expected latest completed bar by timeframe/session, not wall-clock age alone.
Unknown corporate-action scale or mismatched daily/intraday scale blocks comparison.

Targets: bars.py, new calendar/bar-contract helpers, Webull adapter, scanner fetch/
entry/EP/near-close handling, fixtures and tests. Indicator formulas remain unchanged.
Affected later consumers: discovery, hourly/weekly setups, lifecycle and live acceptance.

Webull documentation warns that timestamp formats vary by endpoint. Actual intraday
start/end semantics and adjustment mapping still require read-only live evidence in
20; the adapter must expose unknown semantics until an explicit verified profile is
provided. Do not fabricate a successful live check from a synthetic test or docs alone.
Local parsing/screen comparison can remain available without decision eligibility.

Acceptance: early close/holiday/DST; incomplete bar isolation; stale/missing session
constituents; prior close before opening; real-time delay missing/negative/fractional;
unknown timestamp unit; calendar/hour/week completeness; split scale mismatch;
all existing indicator parity checks and scanner integration remain covered.

Sources:
- https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/default.aspx
- https://developer.webull.com/apis/docs/market-data-api/data-api/
- https://pypi.org/project/exchange_calendars/ (pinned 4.13.2)

Plan B: visibly skipped symbol/failed scan on unknown data; preserve history, no
eligible signal. Rollback code and profile/schema changes together; retain logs.


## Verification and completion evidence

- Focused data/calendar/provider/scanner/EP/indicator suite: **96 passed in 0.86s**.
- Full `.venv/bin/python -m pytest -q -W error`: **326 passed in 1.56s**.
- `git diff --check` passed. Dependency check: no broken requirements. Calendar
  dependency is pinned to exchange-calendars 4.13.2. No indicator formula changes.
- NYSE reference dates checked against the library: 2026-07-02 regular close;
  2026-11-27/12-24, 2027-11-26 and 2028-07-03/11-24 early closes; July 3, 2026
  closed; December 31, 2027 open. Spring/fall DST changes preserve 09:30 ET.
- Early-close slot generation, actual runner dispatch and the entry boundary tested.
  A 13:00 close moves near-close/close/Friday leader jobs to 12:45/13:10/13:40.
- General entries now share M15 completion, session alignment, gap and freshness
  checks with EP. A forming/future high cannot produce an earlier crossing.
- Daily data uses the last completed exchange session, including pre-open weekends.
  Internal missing sessions and stale prior sessions fail. Synthetic scanner fixtures
  now use actual exchange sessions rather than a weekday-only index.
- Hourly aggregation is anchored at 09:30, with the explicit shortened final session
  bar, and works across historical sessions. Weekly bars wait for the final scheduled
  session, including holiday weeks. Missing constituents cannot fabricate a bar.
- Near-close daily snapshots are composed from completed M15s and timestamped in the
  signal evidence. Changing the eventual provider daily close cannot change that
  earlier snapshot. The stored developing snapshot cannot later become a final bar.
- Review found/fixed cached forming bars aging into apparent completeness and an
  evaluation clock preceding a live fetch's receipt. Regression cases cover both;
  the live CLI samples its decision clock after fetching, while historical tests use
  explicit injected times. Historical aggregation uses receipt time correctly.
- Missing/negative/fractional/bool provider delay and duplicate symbol responses fail.
  Numeric timestamps require explicit units; naive/invalid indices fail. A verified
  synthetic profile succeeds, while raw parsing with unknown semantics cannot qualify.
- Split/adjustment scale mismatch rejects the old signal and requires rebuilding.
  Full split-factor/provider normalization remains an adapter acceptance obligation;
  no guessed rescaling is performed.
- Source branches remained at the baseline heads before publication. No live market
  data requests, broker orders, runner activation or deployment. Self-review only.

## Remaining limitations and state compatibility

The profile boundary requires actual per-symbol price-scale/adjustment evidence; a
string in metadata is not authentication. Webull's start/end timestamps, timestamp
units, regular-session content and corporate-action behavior must be verified on
actual read-only responses in 20. Without a checked profile, the adapter returns raw
parsed data but the decision path fails closed. No live-ready claim is made here.

Calendar coverage is bounded from the first 2000 session through the last 2028
session; this is warm-up/session handling, not a backtest. Unscheduled future market
closures require a maintained calendar update and live trading-status checks. Option
expiration/exercise calendars are separate from this cash-equity session calendar.

Old armed JSON records without price_scale_id remain readable but cannot trigger;
rebuild them from validated data. Existing logs are not rewritten. Snapshot freshness
cannot replace the still-pending event lifecycle/re-entry/reapproval work in 07/13.
Hourly/weekly helpers are available; switching each relevant setup to them remains
its planned setup-repair step. Near-close snapshots are evidence, not filled orders.

- [x] Calendar, timeframe completion and provenance contracts implemented.
- [x] Scanner/EP/near-close/provider paths integrated and limitations explicit.
- [x] Positive/negative/calendar/cached-data and integration tests pass.
- [x] Documentation/tracker updated. Push authorized by Taz for Steps 01–06.

Follow-up: Taz accepted fresh qualifying setups with fresh approval for re-entry.
He then required six Webull verification items before Step 07; those remain active.
See [06a-resource-cleanup.md](06a-resource-cleanup.md) for the iMac installation
acceptance repair and the remaining verification scope. Step 07 has not started.
