# Step 06: closing price and volume evidence gaps

Protective code is implemented. Real provider acceptance is still open. This
document does not enable a profile, change a trading rule, or complete Step 06.

## Contract and operating boundary

`data_basis.PriceBasis` records symbol, stable provider security ID, currency,
covered interval, normalization date, verification time, explicit complete
coverage, normalization type and action IDs/revisions/effective sessions. An
empty action list requires an explicit complete-coverage attestation from a
trusted producer; a successful empty response or dividend-only endpoint is not
enough. Other actions (spinoffs, mergers, identifier changes, etc.) must map to
unsupported until their treatment is implemented and checked.

Completed data needs `BarProvenance.price_basis`; `price_scale_id` is now a legacy
display label. Webull checks an attached profile against the returned symbol and
`instrument_id`. Normalization describes the supplied prices: no guessed factor
or double adjustment is applied. Raw history crossing an action is rejected;
split-only adjustment cannot stand in for dividend-adjusted historical levels.

Signals persist the complete evidence. Entry revalidation requires current-session
coverage of the signal's entire action interval, matching security/currency and
unchanged effective events/revisions. New, corrected or removed actions require
rebuilding affected history/indicators/signals. Different fetch times alone do not
invalidate levels. Old label-only signals remain readable but cannot trigger.
The wider lifecycle/approval implementation remains Step 07 and later.

The trusted producer must refresh/reconcile evidence before publishing a profile.
The contract validates its attestation; it cannot discover an action absent from
every supplied source. No real action producer or normalization calculator is
connected yet. Do not set `coverage_complete=True` merely to pass validation.

`VolumeBasis` separately identifies source channel, evidence, trade-inclusion
definition, share units and share-adjustment basis. Price adjustment is not proof
of volume adjustment. Webull defaults to unknown definitions; `volume_profile`
is an optional trusted-producer hook, not an enabled CLI default. Same-series
volume checks require an attested consistent definition/share basis. EP requires
daily and early-volume compatibility too. Its 50-day baseline and 0.5 threshold
are unchanged; no same-time-of-day or shorter baseline has been substituted.

Affected consumers include VCP/cup volume, Luk's anchored VWAP, leader liquidity
and EP. Setup-specific failures are logged without discarding unrelated eligible
setups. Developing daily open/volume are explicitly RTH M15-derived and are not
claimed equivalent to provider daily values. Developing relative volume is
unavailable; historical compatible ratios and price-only RSI(2) remain usable.
The price-only screen diagnostic checks only fields it actually displays.

## Evidence and limits

**Sourced:** Webull's [historical bars reference](https://developer.webull.com/apis/docs/reference/historical-bars/)
distinguishes forward-adjusted daily-and-higher bars from unadjusted minute bars.
It does not explain the observed daily open/volume reconciliation.

**Checked by Claude, reported to Taz, not repeated live by Codex:**
`webull-data-verification-2026-10-01-adapter-addendum.md` reports:

| 2026-09-30 | Daily open | First RTH M1 open | Daily volume | RTH M1 sum |
| --- | ---: | ---: | ---: | ---: |
| AAPL | 330.80 | 330.43 | 49,988,558 | 44,615,782 |
| SPY | 766.45 | 766.37 | 62,110,041 | 46,045,438 |

Closing-print spikes were already inside those RTH sums. Unequal totals do not
prove extended-hours, auction or correction inclusion. The report also shows
NVDA raw M60 closes 1,208.88 on 2024-06-07 and 121.79 on June 10, versus June 7
daily close 120.54428; SPY September 17 daily close 760.711166 versus raw M60
762.60. These establish a compatibility problem, not a universal factor. Current
adjusted historical data is not historical point-in-time evidence.

**Sourced, candidate only:** Webull's [corporate-action events](https://developer.webull.com/apis/docs/reference/fd-events/ca-events/)
are documented under Broker API. Retail/sandbox access and historical coverage
are unverified; do not assume the current keys have that entitlement.

[Alpaca's corporate-actions endpoint](https://docs.alpaca.markets/us/reference/corporateactions-1)
is an alternative to investigate, not an installed fallback. Its documentation
warns of publication delays. `data_quality=complete` does not guarantee the event
universe is complete. Date filters use process dates and pagination must be
exhausted. No source switch or subscription is authorized by this document.

## Unsent Webull support question

Subject: Retail OpenAPI daily/intraday definitions and corporate-action coverage

We use sandbox `/market-data/stocks/bars/list` for read-only testing, with explicit
RTH M1/M15 requests. The table above contains reported September 30 examples.
Could you provide documentation or a worked reconciliation for:

1. Daily open versus first RTH minute open: opening auction, primary exchange vs
   consolidated first print, sale-condition filters and corrections?
2. Daily volume versus RTH minute sum: session bounds, auction/odd-lot/off-exchange/
   late/corrected prints, finalization timing and historical split adjustment?
3. Whether these channels support a comparable partial-RTH versus completed-day
   volume baseline, and how to reproduce that baseline?
4. A retail-accessible complete stock/ETF action source: splits, distributions,
   symbol/security changes, revisions, identity mapping, history and pagination?
5. Daily OHLC adjustment policy/effective-date convention for dividends, splits,
   spinoffs and revisions, including sandbox/production differences?

No credentials are needed in the response. This draft has not been sent; Taz can
send it through Webull support.

## Read-only Claude evidence request

Keep the existing regular-session timing task separate. Its report on `76298b6`
remains useful; do not overwrite it or create another schedule. For this additional
request, record the exact checkout commit and read this document and checkpoint
06c. Do not edit code, enable profiles, change rules, place orders or expose keys.

- Locate a documented, entitled corporate-action source. Record endpoint/tool,
  host/environment, security identity, interval, pagination, revisions and
  effective-date semantics. Name missing types; dividends alone do not cover splits.
- Use the observed NVDA split and SPY dividend for focused checks. Capture sanitized
  source events and raw daily/intraday replies, request bounds and UTC receipt
  timestamps. Distinguish issuer-confirmed events from exhaustive coverage. Do
  not invent a historical verification time for data fetched today.
- Reconcile AAPL/SPY native daily O/H/L/C/V with complete-session M1 and M15 sums.
  Check unique timestamps, XNYS coverage, session tags and window over-return.
  Report whether repeated after-close fetches revise either series. Attribute
  each difference; missing provider definitions remain UNKNOWN.
- Separate sandbox HTTP, connector and production findings. A one-day matching
  total does not prove a stable shared definition. Return pass/fail/unknown plus
  sanitized evidence. Fabricated profiles/factors cannot replace missing evidence.

If Webull cannot document compatibility, a possible later design is to construct
both the full-day baseline and early numerator from the same verified RTH feed,
with enough completed history and checked share adjustment. That changes the
explicit baseline data definition: present consequences to Taz before adopting it.

## Remaining exit criteria

1. An accepted real action producer populates the contract and rebuilds affected
   data/signals; coverage and revisions are demonstrated.
2. Volume/open definitions are reconciled/documented, or Taz approves an explicit
   alternative whose producer and consumers are implemented and verified together.
3. The separate RTH freshness/completion check resolves delayed-sandbox timing.
   Funding alone is not evidence of real-time entitlement.

Offline tests may use clearly fictional profiles. Unknown real data stays
ineligible. Step 07 remains paused under Taz's instruction.
