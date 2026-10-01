# Bounded native-channel acceptance — 2026-10-01

Scope: NVDA common shares and SPY ETF, Webull retail sandbox only. Configuration
is for a read-only integration check; no scan/order activation. Review expires
2026-10-01. Continuing on a later date requires renewed review and fresh action
snapshots, not a timestamp edit on old evidence. Other symbols/hosts are not
implicitly accepted. Taz's iMac passed the final 508-test suite in 5.10s.
The [integrated results](step06-integrated-results.md) close this scoped check.

## Instrument crosswalk

**Checked, user-supplied Claude reports, not fetched again by Codex:**

| Symbol | Webull instrument | Webull exchange | Tastytrade MIC / CUSIP | Instrument |
|---|---|---|---|---|
| NVDA | 913257561 | NSQ | XNAS / 67066G104 | NVIDIA Corporation common stock |
| SPY | 913243251 | PSE | ARCX / 78462F103 | State Street SPDR S&P 500 ETF Trust |

USD instruments. The Webull connector lookup returned name, currency and
subcategory alongside ticker/exchange; the Tastytrade instruments lookup returned
common-stock/ETF description and MIC. The sandbox bar replies carry instrument
IDs. Alpha Vantage requests/responses identify the corresponding US tickers.
This is an explicitly reviewed name/listing/symbol crosswalk, not an assertion
that Webull shares CUSIP/FIGI fields. The runtime must match the observed Webull
instrument ID on every fetch. A rename, listing/share-class conflict or changed
ID requires reconciliation; ticker reuse is not automatically accepted.

Sources: user-supplied `step06-provider-check-dffb60f-2026-10-01` report (05:54–05:55Z),
Tastytrade report (05:56–05:58Z), and `step06-offhours-provider-checks-2026-10-01.md`.
These are reported observations. Source payloads were not all attached here.

## Action coverage procedure

Scope begins 2022-01-01, before the approximate 1,000-session current warm-up.
Alpha Vantage supplies full historical splits and historical/declared dividends:
https://www.alphavantage.co/documentation/#splits
https://www.alphavantage.co/documentation/#dividends

Both successful, dated source responses must be imported together for the current
ET date. Records are kept in the ledger; effective dates and numeric economics
are compared across refreshes, and removals require reconciliation. Empty SPY
split history is acceptable within this reviewed procedure. It is not universal
proof that a provider can never omit an action. No unsupported economic action
was identified in the supplied NVDA/SPY evidence; newly identified exceptional
actions require an unresolved review and prevent publication for that instrument.

Reported source checks: NVDA splits 6 records (2000–2024, latest factor 10 on
2024-06-10); NVDA dividends 57 (2012–2026); SPY splits empty; SPY dividends 112
(1999–2026). NVDA amounts include original 0.04 before the 2024 split and 0.01
afterward, rather than the Tastytrade series' later split-restated amounts.
User-authorized zero-dollar dividend handling: retain and ignore economically.
Missing auxiliary declaration/payment dates do not change the ex-date/amount.

NVIDIA's issuer confirmation of 10:1 trading on June 10 and the June 11
post-split 0.01 cash dividend:
https://investor.nvidia.com/files/doc_downloads/2024/06/nvidia-2024-stock-split_faq_investors.pdf

viaNexus is corroborating historical evidence, not the runtime source. Its SPY
free-text ex-date conflicts are not imported. The Alpha Vantage/tastytrade
ex-dates agree with the structured viaNexus dates and sampled Webull adjustments.
Do not mix conflicting free-text dates into an otherwise coherent event ledger.

## Price channels

**Sourced:** Webull daily-and-above prices are adjusted; minutes are raw:
https://developer.webull.com/apis/docs/reference/historical-bars/

**Checked by Claude:** NVDA June 7/10/11 2024 and SPY March 19/June 17/September
17 2026 daily closes fit the multiplicative split/cash-dividend adjustments
against raw RTH closes (reported residuals within daily rounding). We accept
native D as split/dividend adjusted for this bounded read-only check, and M15
as raw. The producer leaves prices untouched. This is an engineering acceptance
inference for ordinary events, not proof of special-action treatment or a claim
that every native daily open/high equals the aggregated minute value.

The RTH check at dc2ae04 verified start-labeled M1/M15 bars, current completed
bars, and forming-bar behavior for AAPL/SPY. The timestamp/session convention is
applied to NVDA on the same documented stock-bar interface and validated again
by the integrated command. The NVDA integrated result subsequently passed on current RTH data at
2026-10-01T16:00:30.120626+00:00 in Claude cloud. Premarket/overnight are excluded.

Daily levels/indicators use native daily OHLC. EP's current-session opening price
is explicitly the first RTH minute-series open; no assumption of equality with a
native daily auction/open value is made. Developing daily bars retain their
existing completed-M15 composition labels.

## Volume comparison policy

**Checked:** ten AAPL/SPY sessions (September 24/25/28/29/30) had complete RTH M1
and M15 constituents, with matching M1/M15 aggregates. Native daily/RTH volume
ratios ranged about 1.08–1.35. Native daily volume matched viaNexus unadjusted
volume in those samples; upstream independence is unknown. No correction factor
is justified by those varying ratios. Webull sale-condition filters remain opaque.

**Sourced:** different intraday and end-of-day feeds can legitimately include
different trades and produce different OHLCV:
https://www.tradingview.com/pine-script-docs/faq/other-data-and-timeframes/
Qullamaggie compares opening activity to average daily volume:
https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/

**Acceptance policy / inference, not a vendor guarantee:** retain the approved
native-daily denominator and distinct RTH numerator as the explicit metric
`webull-rth30/native-daily50-v1`. The feeds keep different definition IDs. Their
trade sets are not declared identical, and lower intraday coverage can reduce the
ratio. The 50-day/0.5/30-minute combination remains the desk's existing assumption.
This policy is not evidence of profitability or calibration to another vendor.

Units are reported share counts. Since historical volume adjustment across a
split is not established, only windows on/after the latest effective split are
accepted as current-share comparisons. Older windows are unavailable; no guessed
volume multiplier or shorter EP lookback is introduced. This applies per metric,
so an old split outside a current 50-day window does not disable that window.

Native daily same-series ratios use native daily volume; RTH calculations use
RTH volume. EP alone uses the explicit directional pair. Current instrument IDs,
share basis and evidence dates must match. The configuration applies this
provider-channel policy to NVDA/SPY; it does not claim an NVDA volume study was
performed. The integrated diagnostic subsequently returned PASS for both symbols; see
the linked results for timestamps and host separation.
