# G3 follow-up: adjustment rebuild and automatic volume evidence

Implementation is on `codex/g3-revision-followup`, developed on `bb75602` and
integrated on Claude's committed G4 `d1e4514` without modifying his repair branch. **Local tests are not provider acceptance. Neither requested
fix is operationally closed until the new source/host check passes.** No scheduler,
orders, purchases, account creation, or activation occurs in this change.

## What changed

A validated change to the original armed signal's daily OHLCV retires its events
and queues its exact symbol/setup/direction. The next bounded detector batch in
that scan recomputes indicators and setup levels; it does not scale old levels.
Successful replacement waits for a new crossing after detector completion. An
outage or missing required evidence stays pending; a setup that genuinely no
longer qualifies is removed. Removal from the watchlist, restart and concurrent
withdrawal cannot resurrect it. Unrelated armed signals remain intact.

A real ex-dividend day can have adjusted daily history and unchanged raw minute
history. The optional action source can explain the latest completed raw anchor
using **one** ordinary USD cash dividend effective today (raw close minus original
cash amount), or **one** split (raw close divided by new/old share ratio). The
observed adjusted daily close must match within 0.000001. Unsupported, combined,
missing or contradictory terms stay unavailable for that ticker. Both original
closes, event economics, receipt and source snapshot remain in price evidence.
Native bars are never rewritten. A pre-action raw anchor cannot then be used as
current-basis historical trading data.

For volume, one market-wide split snapshot can serve 30–50 names; no per-ticker
review file is required. Fresh Webull security identity pins bind each annotation.
An explicit host-level inference accepts provider-reported Webull share counts
only within the queried interval and on/after the latest reported share-change
event, including `stock_dividend`. Calculations outside this window stay
unavailable; no historical volume multiplier or shortened lookback is invented.
Native daily and RTH volume keep distinct definitions and the existing
`webull-rth30/native-daily50-v1` comparison. The 50-session / 0.5 rule is unchanged.

## Research and limits

**Sourced:** [QuantConnect indicator reset guidance](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/key-concepts)
and [Algoseek's adjusted bar guide](https://us-equity-market-data-docs.s3.amazonaws.com/algoseek.US.Equity.Trades.Only.Adjusted.Minute.Bar.pdf)
support recalculating after corporate-action adjustments. They do not prove this
is the best trading strategy or the practice of every retail trader.

**Sourced:** [Webull historical bars](https://developer.webull.com/apis/docs/reference/historical-bars/)
documents adjusted daily prices and raw minute prices. **Checked, supplied report:**
`step06-offhours-provider-checks-2026-10-01.md` explains six NVDA/SPY closes from
original split/dividend terms to six-decimal rounding. Extending the ordinary
formula to the same API's other US shares is an engineering inference and still
requires a real new-source price check. It is not a special-action guarantee.

**Sourced:** [Massive splits](https://massive.com/docs/rest/stocks/corporate-actions/splits)
and [dividends](https://massive.com/docs/rest/stocks/corporate-actions/dividends)
are in its free Stocks plan, updated daily, with two years of history. Both have
up to 5,000 rows/page and pagination. [Free request limits](https://massive.com/stocks)
are five/minute. This makes batching more suitable for this watchlist than
[Alpha Vantage's 25 requests/day](https://www.alphavantage.co/support/).
Actual entitlement, response filters, event identity mapping and daily publication
latency remain unverified here. We do not use Massive adjusted OHLCV or cumulative
factors to change Webull data. Empty fully paged responses mean provider-reported
absence for a queried interval, not universal corporate-action completeness.

**Acceptance inference:** native Webull volume in a window without a reported
share change is usable as provider-reported shares under the explicit policy.
Sale-condition filters and historical cash-dividend volume treatment are still
not documented by Webull. The initial host check must inspect native volume on
both sides of a recent cash dividend; no-change observations alone are inadequate.
[TradingView](https://www.tradingview.com/pine-script-docs/faq/other-data-and-timeframes/)
explains legitimate EOD/intraday differences.
[Qullamaggie's EP discussion](https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/)
supports comparing opening activity with average daily volume, not changing the
desk threshold to force signals. This is a reasonable engineering design with
explicit limits; profitability or universal retail adoption is not established.

## Cache, outages and persistence

Successful batches are persisted for the current ET date; next-date use requires
fresh retrieval. Any validated OHLCV revision requires split evidence at/after that
revision before volume is annotated again. A price conflict refreshes shared action
batches if older than 15 minutes; that is a retry cadence, not a latency guarantee.
Four-page maximum and a shared persistent five-request rolling-minute budget bound
calls. No retry loops or sleeps are added. HTTP 429 stops this adapter for the ET
date conservatively; externally spent quota is not known. Incomplete pages, bad
envelopes, transport failures and stale evidence never become no-action evidence.
An attributable bad row blocks that ticker; an unidentifiable row makes the batch
unavailable. Prices that independently validate remain available to price-only
setups during volume failures. Invalid optional configuration is isolated likewise.
No raw error bodies, secret URLs or credentials are stored; authentication uses
headers, redirects are disabled, and pagination is restricted to the exact host/path.

## Provider acceptance — read only, no runner activation

Use a free Massive account/key if available. Do not send the key in chat or put it
in the repository. This branch needs the key only for the read-only check. Creating
an account is an external prerequisite, not something this patch performs.

On a machine with the repo and Webull credentials already configured, activate
its venv and load the existing private environment file. Capture these facts first:

```sh
git log -1 --format='%H %s'
python -m pytest -q -W error
```

Privately supply `MASSIVE_API_KEY` in the same shell if not already configured.
Use a fresh scratch directory; the following flags apply only to this probe and
do not save settings or activate the scanner:

```sh
mkdir -p "$HOME/Desktop/g3-followup-check"
python -m desk.vendor_check \
  --database "$HOME/Desktop/g3-followup-check/vendor.sqlite" \
  --auto-actions-database "$HOME/Desktop/g3-followup-check/actions.sqlite" \
  --accept-native-volume-policy --require-volume \
  --symbols NVDA SPY QQQ AAPL \
  --output "$HOME/Desktop/g3-followup-check/result.json"
```

Expected: all price checks PASS **and** all `volume_pair` fields PASS. A price PASS
with unavailable volume is INCOMPLETE under `--require-volume`. Confirm source
responses really cover the requested date bounds with all pages exhausted; retain
safe source snapshot IDs, row counts, current ET date and host. No scanner run.

Additional acceptance evidence before operational closure:

1. For a known split **inside the free two-year window**, confirm the source date,
   ratio and class against issuer evidence; show a 50-session window crossing the
   split is unavailable and a sufficiently long post-split window passes. NVDA's
   June 2024 split is outside this window in October 2026; do not use an empty free
   response to deny it. No paid history is required for a current post-split window.
2. For a current ordinary ex-dividend ticker returned by the batch, confirm identity,
   original cash amount and ex-date against issuer evidence. Save raw previous RTH
   close, native adjusted previous D close, and the action proof. Their difference
   must match within six-decimal rounding. If no such current event is available,
   keep real dividend reconciliation acceptance pending; local fixtures do not
   replace it. Do not backdate the clock on a live current-response adapter.
3. Compare native daily **volume** around a recent known ordinary cash dividend
   against independent raw share counts on at least the affected/preceding sessions.
   Confirm no proportional cash-dividend volume scaling; disclose trade-feed
   differences rather than forcing an equality with RTH totals. Existing AAPL/SPY
   samples support the channel policy but do not settle this new event/source check.
4. Repeat the fresh scratch probe with ~50 real watchlist names. Report every
   unavailable name, request count, date coverage and identity issue. Confirm
   ordinary repeats share cache and a new ET date refreshes. Do not claim coverage
   across all securities or real-time corporate actions.

A source account/access failure is reported separately from a parser failure.
Do not install an unverified policy into the normal environment, rename an old
snapshot to today's date, or treat a warning override as data verification.

## Handoff and rollback

Review/cherry-pick this branch onto Claude's branch after his G4/G5 checkpoint is
committed; resolve overlapping scanner/data contracts deliberately and rerun the
combined strict suite. Do not force-push or reset his work. Rollback: revert this
commit and unset the three new optional settings. SQLite tables are additive;
audit history can remain. Resuming Step 09 is a separate authorization/checkpoint.
