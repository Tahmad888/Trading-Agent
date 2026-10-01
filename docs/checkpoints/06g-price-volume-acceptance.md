# Step 06 follow-up G — Explicit native price/volume channels

Status: implemented; local verification below, credentialed integration result pending. Base e86f3d4. Serves watch/analyze. One implementer;
self-review. Taz authorized finishing remaining Step 06 work; the iMac passed
492 tests in 5.05s. No Step 07, orders, scheduler or production activation.

## Impact record before changes

Sourced: Webull historical-bars documentation distinguishes adjusted D and raw
minute bars. TradingView documents legitimate differences in daily vs minute
OHLCV; https://www.tradingview.com/pine-script-docs/faq/other-data-and-timeframes/.
Qullamaggie's EP description compares opening volume to average DAILY volume,
not a same-time historical baseline:
https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/.
These sources do not document Webull's specific sale-condition filters.

Checked by Claude, supplied to Taz: ten AAPL/SPY sessions have complete M1/M15
RTH data; their aggregates agree, while native daily volume differs. Native
daily volumes also match viaNexus unadjusted daily volumes in those samples
(upstream independence unknown). Six NVDA/SPY historical closing-price samples
fit split/dividend-adjusted daily prices against raw intraday closes. This
supports bounded testing, not every action type or universal OHLC equality.

Decision retained from 06d: first 30 RTH minutes / previous 50 native daily
volumes, threshold 0.5. Do not substitute a smaller RTH-only denominator, change
thresholds, infer identical inclusion filters or guess a correction multiplier.
Implementation correction: permit an explicitly reviewed directional source-pair
comparison instead of requiring identical channel definition IDs. This is a
source-pair acceptance policy, not a vendor definition of every trade included.

Native volume share adjustment across a split remains unverified. Avoid guessing:
attach bounded current-share evidence from the latest effective split through the
ledger basis date, and validate the actual calculation window, not all 1,000
price warm-up bars. Windows crossing an unresolved split stay unavailable. Mask
relative-volume values until their full rolling window is covered. VCP/cup/EP,
anchored VWAP and leader liquidity must validate their exact consumed rows.

Affected producers/consumers: VolumeBasis, action-backed source/channel reviews,
feature generation, setup volume windows, watchlist, read-only acceptance CLI,
tests and docs. Existing legacy synthetic same-definition profiles remain valid.
Native daily prices remain vendor adjusted; do not apply factors a second time.
EP uses first RTH M15 open as its defined intraday entry-session observation, not
a claim of equivalence to official native daily open. Daily setup levels use D.

Add a diagnostic command for the credentialed host to load the real reviewed
ledger/config, fetch D/M15, exercise completion/identity/price/volume checks and
report each stage separately. It creates no signals/orders or schedules. Record
candidate NVDA/SPY channel configs and identity evidence; do not fabricate missing
coverage or enable profiles on behalf of unavailable provider results.

Acceptance: mixed native pair succeeds only with explicit policy, identity,
current share units and evidence dates; unrelated/swapped/mismatched profiles
reject; split-crossing windows reject and post-split windows recover; exact
50-day/0.5 EP rule unchanged; no OHLCV rewriting; actual payload replay; full strict
Python 3.12/3.14 suites; read-only iMac/Claude check remains actual operational
acceptance. No further generic source-connectivity or RTH timing request.

Plan B: report the affected metric/setup unavailable with reason; keep price-only
calculations available. Rollback additive config/diagnostic and coordinated volume
producer/consumer changes together; preserve ledger/history and existing logs.

## Implemented behavior

- VolumeBasis carries optional instrument identity, date bounds and an explicit
  directional EP comparison policy. Native daily and minute definitions differ;
  unrelated providers, reversed channels, share units/identities and evidence
  dates cannot pass merely because both fields say volume.
- The ledger-backed source creates current-share volume evidence only on/after
  the latest effective split. It preserves native OHLCV values. Actual consumer
  windows determine eligibility: 50 sessions for EP/VCP, handle+50 for cup,
  anchor-to-current for anchored VWAP, configured liquidity window for leaders.
  Relative volume is unavailable until every row in its rolling window is covered.
- First RTH opening-volume rows are validated before constructing EP context.
  EP's native daily 50-session baseline and 0.5 threshold are unchanged.
- Sandbox NVDA/SPY mapping/channel config is now concrete and source-reviewed,
  with the inference/limits recorded in docs/evidence/step06-native-channels.md.
  It is date-bounded to 2026-10-01 and not activated automatically. AAPL/QQQ and
  general discovery symbols are not silently accepted by these sample profiles.
- `python -m desk.data_acceptance` performs one bounded read-only integrated
  check (D/M15 for selected NVDA/SPY), reports failures by stage, stops on a fetch
  failure, suppresses transport details, and creates no orders/signals/schedules.
  After close it uses an explicit historical session and excludes that session's
  daily bar from its previous-50-session EP denominator. That is historical
  integration evidence, not a new claim of live regular-session freshness.

## Remaining external acceptance

The implementer has no credentialed iMac/Claude shell in this chat. A real
ledger import plus `desk.data_acceptance` run must still be returned from that
host. Existing same-day dated source reports should be reused; never retimestamp
fixtures. No fresh source-connectivity or generic timing study is requested.
If a saved report lacks its original receipt time, recover the timestamp from
its request log or fetch just that missing source response; do not invent it.

No local credential file was read and no authenticated API call was made here.
The reviewed configuration is not a live deployment. Step 06 cannot be marked
fully accepted until the real integrated result is supplied. Step 07 stays paused.

## Verification and publication

Actual local verification on 2026-10-01:

```
.venv/bin/python -m pytest -q -W error
# 508 passed in 2.14s (Python 3.12.14)
../verify-python314/bin/python -m pytest -q -W error
# 508 passed in 2.19s (Python 3.14.6)
git diff --check
# passed
```

16 new tests include allowed/forbidden mixed source pairs, instrument/share/date
mismatches, split-window rejection and rolling recovery, unchanged numeric bars,
exact EP threshold, integrated diagnostic success/failure, and after-close
exclusion of the tested session from its daily baseline. Scoped config files also
validated against their actual models. An initial new after-close fixture used a
pandas chained assignment; corrected to `.loc` before final passing suites.
Self-review only. Upstream matched e86f3d4 before implementation; rechecked before
publication. Push to the authorized repair branch only, without merging.
