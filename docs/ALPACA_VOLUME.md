# Alpaca SIP volume producer (G5 checkpoint 1)

Trader-day step: **watch** (volume evidence for the watchlist and the EP volume test).
Status: implemented, opt-in and **inactive**. No scanner, watchlist, trigger, ticket or
iMac setting uses it. Scanner integration is checkpoint 2, after Astra's audit.

Code: `src/desk/alpaca_volume.py` (client, evidence, cache, calculation) and
`src/desk/alpaca_probe.py` (bounded acceptance probe). Tests:
`tests/test_alpaca_volume.py` (synthetic fixtures only, no network).

## Source contract

| Item | Contract | Label |
| --- | --- | --- |
| Route | `GET https://data.alpaca.markets/v2/stocks/bars` only; redirects refused; default TLS context (certificate and hostname checks on) | Sourced: [stock bars](https://docs.alpaca.markets/us/reference/stockbars) |
| Credentials | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` from the environment, sent as `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers. Never logged, saved or put in a URL | Sourced (same page) |
| Feed | `feed=sip` on every request. An auth or entitlement error is unavailable evidence; no fallback to IEX, Webull or ranking-list volume | User policy (handoff) |
| Adjustment | `split` for the paired EP channels; `raw` only for diagnostics. Volumes kept exactly as returned; no multipliers | Sourced: `split` adjusts price and volume for splits, `raw` applies none |
| Paging | `limit=10000`, `sort=asc`; follow `next_page_token` until null. The limit counts across symbols. Remaining pages when the budget runs out make the whole request `INCOMPLETE_PAGINATION`, never a truncated success | Sourced |
| End bound | Historical SIP request `end` must be at least 15 minutes old, otherwise nothing is sent (`END_NOT_15_MINUTES_OLD`) | Sourced: [market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq) |
| Stop | 401/403 (`AUTH_OR_ENTITLEMENT_FAILURE`) and 429 (`RATE_LIMITED`) stop the run; later calls are refused without a request. No retry, key rotation or IP workaround. Other HTTP or transport errors make that request unavailable | User policy |
| Budget | `RequestBudget` shared by one run; the probe allows at most 6 HTTP requests in total, pages included | User policy |
| Prices | Not retained. Webull stays the price source; no VWAP from Webull prices with Alpaca weights | User policy |
| Identity | Symbol as requested. `asof` is not sent, so Alpaca's default symbol mapping applies; no independent provider identity is resolved (`provider_identity: null`, recorded in `mapping_provenance`). The cache pins each symbol's identity record; a change makes that ticker unavailable (`IDENTITY_CHANGED`) | Sourced (`asof` maps ticker identity, not adjustment versions); the missing independent identity is a known limit |
| Plans | Basic is free; live Basic equities are IEX only; real-time consolidated coverage needs the paid tier. No purchase is made or implied | Sourced: [plans](https://docs.alpaca.markets/us/docs/about-market-data-api) |

### Evidence record (`VolumeObservation`, one per ticker per complete request)

Requested and returned symbol, provider identity (null) and mapping provenance, feed,
adjustment, timeframe, channel, definition id, share-basis id, requested bounds, first
send and final receipt times, the (bar start, volume) pairs, the SHA-256 of every page
and a content digest. The content digest excludes receipt times, so the same content
gives the same digest. `observed_through()` is the end of the latest bar, kept apart
from the receipt time: a fresh receipt does not make an old bar current.

### Channels and definitions

| Channel | Timeframe | Definition id (split) | Coverage |
| --- | --- | --- | --- |
| `native-daily` | `1Day`, stamped at ET midnight | `alpaca:sip:split:native-daily:provider-reported` | Provider's session aggregate; Checked on 5 of 6 SPY/QQQ September sessions to include 04:00–20:00 ET within 0.01% (`research/ai-trading/alpaca-sip-volume-2026-10-04/`) |
| `rth-m15` | `15Min`, labelled by bar start, inside the exchange session on the 15-minute grid | `alpaca:sip:split:rth-m15:provider-reported` | Regular-hours 15-minute bars |

Share basis: `alpaca:sip:adjustment=split` or `alpaca:sip:adjustment=raw`. The two never
qualify as the same basis. Rows outside the requested bounds, weekend or holiday rows,
daily rows not stamped at ET midnight, and minute rows outside the session or off the
grid fail that ticker (`WRONG_CHANNEL_ROW`). Duplicates, unsorted rows, non-finite or
negative volume, and malformed bars fail that ticker only.

### Comparison policy `alpaca-sip-split:rth30/native-daily50-v1`

Directional and explicit: numerator is the sum of the two completed `rth-m15` bars
starting 09:30 and 09:45 ET on the entry session; denominator is the mean of the
`native-daily` volumes of the 50 exchange sessions before the entry session. Both must
be `split`, same identity record, same share basis. The two definitions stay distinct;
this is not a claim that they cover the same trades. The threshold is the EP card's own
`early_volume` parameter (0.5), compared exactly as `first30 × 50 ≥ 0.5 × total`. It is
read from the card at every calculation and revalidation (`approved_rule()`); callers
cannot pass a threshold (F3). Each result records the card fingerprint as
`rule_version`, and a result whose threshold is not the approved one cannot be built
under this policy id. An approved card change requires fresh qualification.

Unavailable when: fewer than the 50 required sessions (no zero fill, no carry-forward),
either 09:30/09:45 bar missing, the RTH receipt earlier than 15 minutes after 10:00 ET,
the daily receipt earlier than the ET midnight after the last required session
(Assumption: the extended-hours daily aggregate is final by then), a zero baseline,
raw evidence (`RAW_DIAGNOSTIC_ONLY`), mixed raw/split (`SHARE_BASIS_MISMATCH`), wrong
channel or identity. The entry session's own daily row is never in the baseline. A
threshold pass is a volume component only, not setup qualification, signal activation
or profitability evidence.

### Cache and revisions (`VolumeCache`, SQLite)

Keyed by feed, adjustment, symbol, timeframe and exact bounds. Same content is
`UNCHANGED` (only the last-received time moves); changed content is `REVISED`, with
the changed bar timestamps and a revision counter. A receipt older than the stored one
is refused. Snapshots are never deleted; they stay as audit history.

Eligibility (repaired after Astra's audit of `2d97ea6`, findings F1 and F2) comes from an
append-only `events` log ordered by sequence, not by receipt time:

- every successful refresh of a ticker adds `OK`, even when the content is unchanged;
- every per-ticker failure (malformed or missing ticker, identity change, clock
  regression) and every request-level failure (HTTP, transport, envelope, pagination,
  budget) adds `FAILED` for each affected ticker;
- an authentication, entitlement or rate-limit stop adds `STOP` as well.

A cached snapshot is eligible only when that ticker's latest event for that request key
is `OK` and no `STOP` was recorded after it. A failed latest refresh therefore stays
unavailable across restarts until a later successful refresh; healthy tickers in the
same request stay usable. `fetch(reuse=True)` is cache-only: it never sends a request
and returns each ticker's snapshot or its reason (`NOT_CACHED`, the failure code, or
`PROVIDER_STOP_AFTER_LAST_SUCCESS:<code>`). While a client has an active run stop, every
fetch, the cached path included, returns `RUN_STOPPED_AFTER_<code>`. There is no
archival replay mode. A cache written by `2d97ea6` has no events, so nothing in it is
eligible.

`revalidate()` refuses an earlier `EPVolumeComponent` whose input digests no longer
match (`VOLUME_EVIDENCE_REVISED`), whose threshold or rule version differs from the
current EP card (`VOLUME_RULE_CHANGED_REQUALIFY`), or whose stored result differs from a
fresh calculation (`VOLUME_RESULT_MISMATCH`). Accepting `adjustment=split` is not taken
as complete, independent corporate-action coverage.

## Planned consumers (checkpoint 2, not wired)

Discovery's 50-day liquidity average, daily relative volume, setup volume comparisons
(VCP, cup, Darvas, breakout), EP RTH30/native-D50, stored qualification evidence,
scanner refresh/revalidation, revision rebuild and ticket consumption. Each comparison
must use one source end to end. `data_basis.VolumeBasis` has no Alpaca policy yet;
checkpoint 2 adds it with its own tests instead of reusing the Webull policy string.

## Plan B

Unavailable volume for the affected ticker (current behaviour). Price-only checks are
unaffected. Massive (G3a) remains the other planned volume path.

## Rollback

Remove `src/desk/alpaca_volume.py`, `src/desk/alpaca_probe.py`,
`tests/test_alpaca_volume.py` and this document, and revert the doc and
`.env.example` edits of the checkpoint commit. Nothing else imports the module, so
no runtime behaviour changes.

## Acceptance run

`python -m desk.alpaca_probe --symbols NVDA,SPY,QQQ,AAPL --entry-session 2026-10-02
--daily-start 2026-07-01 --out DIR --cache DIR/volume-cache.sqlite`. Results and
limits: `checkpoints/G5a-alpaca-volume.md`.
