# Alpaca SIP volume producer and consumers (G5a checkpoints 1 and 2)

Trader-day step: **watch** (volume evidence for the watchlist and the EP volume test).
Status: checkpoint 1 (producer, probe) audited and closed by Astra on `df99472`.
Checkpoint 2 (consumers, identity, partial discovery) is implemented and **pending
Astra's audit**; it is opt-in (`DESK_ALPACA_VOLUME_CACHE`) and no iMac setting or runner
enables it. Checkpoint 3 (scoped history validation) has not started.

Code: `src/desk/alpaca_volume.py` (client, evidence, cache, calculation) and
`src/desk/alpaca_probe.py` (bounded acceptance probe). Tests:
`tests/test_alpaca_volume.py` (synthetic fixtures only, no network).

## Source contract

| Item | Contract | Label |
| --- | --- | --- |
| Route | `GET https://data.alpaca.markets/v2/stocks/bars` and (checkpoint 2) `GET https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity`, each allowlisted separately; no account, order or position route; redirects refused; default TLS context (certificate and hostname checks on) | Sourced: [stock bars](https://docs.alpaca.markets/us/reference/stockbars) |
| Credentials | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` from the environment, sent as `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers. Never logged, saved or put in a URL | Sourced (same page) |
| Feed | `feed=sip` on every request. An auth or entitlement error is unavailable evidence; no fallback to IEX, Webull or ranking-list volume | User policy (handoff) |
| Adjustment | `split` for the paired EP channels; `raw` only for diagnostics. Volumes kept exactly as returned; no multipliers | Sourced: `split` adjusts price and volume for splits, `raw` applies none |
| Paging | `limit=10000`, `sort=asc`; follow `next_page_token` until null. The limit counts across symbols. Remaining pages when the budget runs out make the whole request `INCOMPLETE_PAGINATION`, never a truncated success | Sourced |
| End bound | Historical SIP request `end` must be at least 15 minutes old, otherwise nothing is sent (`END_NOT_15_MINUTES_OLD`) | Sourced: [market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq) |
| Stop | 401/403 (`AUTH_OR_ENTITLEMENT_FAILURE`) and 429 (`RATE_LIMITED`) stop the run; later calls are refused without a request. No retry, key rotation or IP workaround. Other HTTP or transport errors make that request unavailable | User policy |
| Budget | `RequestBudget` shared by one run; the probe allows at most 6 HTTP requests in total, pages included | User policy |
| Prices | Not retained. Webull stays the price source; no VWAP from Webull prices with Alpaca weights | User policy |
| Identity | Checkpoint 1: symbol as requested, `provider_identity: null`. Checkpoint 2: `alpaca_assets.IdentityStore` joins each desk symbol's Webull metadata to one row of the paper asset list (one cached GET per run, shared budget) and pins a versioned record (see below). `asof` is not sent, so Alpaca's current entity mapping applies | Sourced (`asof` maps ticker identity, not adjustment versions; the asset routes do not prove a historical mapping) |
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

## Checkpoint 2: identity, consumers and gates

Record before coding, consumer matrix, timing decisions and test map:
`checkpoints/G5a-cp2-volume-consumers.md`. Code: `src/desk/alpaca_assets.py`,
`src/desk/alpaca_source.py`, `data_basis` (decision volume), and the wired consumers.

**Identity** (`alpaca_assets.py`). Per desk symbol: Webull symbol and instrument ID,
Alpaca symbol and asset ID (separate fields), class, exchange, names, status, method
(`exact-symbol`, or `explicit-class-share-alias` from a written alias table: BRK.B only;
punctuation is never stripped), asof policy, asset-list receipt and digest. Unresolved
(`WEBULL_IDENTITY_MISSING`, `ASSET_NOT_FOUND`), ambiguous, unsupported (inactive, non-US
equity), conflicting, reused or alias-mismatched identities make that ticker's volume
unavailable with the code kept. A changed identity gets version n+1 with
`valid_after_session` and a new cache namespace; the old version stays as history. A
window that reaches back to or before `valid_after_session` is
`UNSUPPORTED_HISTORICAL_MAPPING`. A first pin relies on Alpaca's current entity mapping,
corroborated only by the Webull instrument's own price history covering the window
(Assumption: not independent proof of a historical mapping).

**Decision volume** (`data_basis.AlpacaDecisionVolume`, policy ids
`alpaca-sip-split:native-daily-v1` and `alpaca-sip-split:rth-m15-v1`): attached as
`frame.attrs["decision_volume"]`. The Webull OHLC, `volume` column, bar provenance and
`volume_basis` are unchanged. `decision_window(df, n, final_only)` is the one consumer
view: with an attachment it reads only Alpaca values joined by NY session date (M15 by
exact interval start), refuses IEX, raw, mixed or misaligned input, and never falls back
to Webull; without one the old Webull path runs unchanged.

**Consumers** (all comparisons exact in Decimal, one source each): discovery liquidity
(mean of the latest 50 final sessions ≥ 1,000,000), VCP dry volume (10-day mean < 0.7 ×
50-day mean), cup handle volume (handle mean < 50-day mean), EP through
`ep_volume_component` (the approved-card calculation above), and the display-only
`rel_volume` feature (same denominator semantics, float once per point, no decision
consumer). Anchored VWAP (Luk) keeps its existing Webull price × Webull volume path gated
by `volume_basis`; it never reads Alpaca weights. Card volume conditions with no entry
check today (VCP rising entry volume, cup 1.4× and Darvas 1.5× breakout volume) are
listed in `alpaca_source.NOT_IMPLEMENTED`, never treated as satisfied.

**Evidence and gates.** Each qualifying signal carries `volume_evidence`: used values and
sessions/intervals, definitions, share basis, both identities and mapping version,
request fields, snapshot digests, receipts, card fingerprint, result, and a dependency
digest. `signal_state.candidate_id` hashes only the dependency terms (not receipts or
snapshot digests), so an identical later receipt is not a new candidate. The gate
`scanner.volume_status` runs from the cache before trigger observation, with a fresh
refresh at signal revalidation, and from the cache at ticket preparation, approval,
consumption and the final fence (`risk_terms.EventRiskSource.held_event`).
UNAVAILABLE suspends; CHANGED (used volume, source, share basis, identity version or
card fingerprint) invalidates and queues the existing rebuild. Revoked or consumed
tickets are never revived.

**Identity health (audit F1).** Mapping history (`mappings`) and current eligibility
are separate. `identity_health` is an append-only, ordered log (`sequence`, scope
`symbol:<desk>` or the shared `source:alpaca-assets`, `OK`/`FAILED`, reason, mapping
version and digest, asset-list digest, UTC receipt time). Every resolution attempt
appends an outcome in the same transaction as any mapping write; an attempted
asset-list request that fails appends a source FAILED (401/403/429 also keep the cache
STOP). Requests never sent (budget exhausted, run already stopped, guard held) record
nothing new. `IdentityStore.current` is one read: eligible only when the ticker's latest
event is OK for exactly the latest mapping version and digest and no source FAILED is
newer. Otherwise the gate returns the recorded reason, `IDENTITY_SOURCE_FAILED:<code>`,
`IDENTITY_HEALTH_NOT_RECORDED` (a pin from before this table) or
`IDENTITY_HEALTH_INCONSISTENT`. Recovery order: a failure is cleared only by a later
successful resolution against a list received after it; a list not newer than a recorded
failure for that ticker or the source writes nothing (`IDENTITY_STALE_ASSET_LIST`). An
unchanged identity recovers the same pin and signal without new terms; a changed one
takes the version/namespace/requalification path. A ticker failure affects that ticker;
a source failure withholds every identity-dependent volume input; price-only setups are
not consulted. A store read or write error fails closed.

**Final guard and lock order (audit F2).** `AlpacaVolumeProvider.held()` takes
`BEGIN IMMEDIATE` on the volume/identity file (sorted unique paths, 5 s busy timeout)
and blocks the provider's network until release. `EventRiskSource.held_event` holds the
signal store and then this reservation; the ticket's final transaction keeps both
through its COMMIT, and the final clock is read only after every guard is held. One
order everywhere: ticket (EXCLUSIVE) → signal (IMMEDIATE) → volume/identity (IMMEDIATE)
→ account (IMMEDIATE, last). Volume/identity writers are single-file transactions and
never wait on another store. A disqualifier committed before the guard is read and
refused; a writer arriving later waits and commits after the ticket. Acquisition failure,
an exception or rollback refuses without spending the approval and releases every
guard. With a run stop active, the cache-only reuse path is read-only.

**Migration.** Additive: the `identity_health` table and index are created on open. Older
pins remain history and are ineligible until a fresh successful resolution; the previous
code ignores the table.

**Configuration.** `DESK_ALPACA_VOLUME_CACHE` (path; unset = unchanged Webull path) and
`DESK_ALPACA_VOLUME_BUDGET` (HTTP requests per run, pages and the asset list included).
A malformed budget or missing keys gives an unavailable provider: volume-dependent checks
report the reason, prices and price-only setups keep working.

**Timing.** Native daily is treated as final at the next ET midnight (checkpoint 1
Assumption): the 16:10 close scan cannot qualify that day's VCP/cup volume, so those
names are re-prepared at the next session's first slot; the Friday 16:40 build's
liquidity window ends Thursday. EP's 09:30/09:45 bars need a 15-minute-old end, so EP
volume is available from 10:15 ET; the 10:00 mover check retries at the next slot.

**Partial discovery.** `leader_scan_job` gives every candidate one outcome (selected,
criterion, source failure, limit) and publishes READY, PARTIAL or EMPTY as one atomic
bundle (`watchlist-build.json`: generation, list, digest, report); FAILED (universe list
or SPY benchmark) and INCOMPLETE (source failures, no leaders) keep the previous list.

**Checked results.** 1282 strict tests on Python 3.12 and 3.13 at `b5dab2c`; 1319 after
the F1/F2 repair (fixtures only). One
bounded live preview at `14aeb35` (cloud, sandbox Webull, 3 of 6 Alpaca calls): PARTIAL,
283/283 identities and volume windows complete, 45 leaders from a ranked population of
213, 96 disclosed source failures. Evidence:
`/mnt/project-files/research/ai-trading/alpaca-volume-checkpoint2-2026-10-04/REPORT.md`.
Not established: live EP RTH SIP at 10:15, real-time entitlement, BRK.B alias on live
data, iMac acceptance.

## Plan B

Unset `DESK_ALPACA_VOLUME_CACHE`: every consumer takes the unchanged Webull path (whose
volume basis is currently unaccepted, so volume-dependent checks stay unavailable).
With Alpaca configured, any provider fault makes only the affected ticker's volume
unavailable; price-only checks are unaffected. If the final guard cannot take the volume/identity
reservation (another writer holds it past 5 s), the approval or consumption is refused
unspent and can be retried. Massive (G3a) remains the other planned
volume path.

## Rollback

Checkpoint 2: unset `DESK_ALPACA_VOLUME_CACHE`, or revert its commit. The identity
tables, `watchlist-build.json` and `pending-*.json` files are additive; the previous code
ignores them and keeps reading `watchlist.json`.

Checkpoint 1: remove `src/desk/alpaca_volume.py`, `src/desk/alpaca_probe.py`,
`tests/test_alpaca_volume.py` and this document, and revert the doc and
`.env.example` edits of the checkpoint commit. Nothing else imports the module, so
no runtime behaviour changes.

## Acceptance run

`python -m desk.alpaca_probe --symbols NVDA,SPY,QQQ,AAPL --entry-session 2026-10-02
--daily-start 2026-07-01 --out DIR --cache DIR/volume-cache.sqlite`. Results and
limits: `checkpoints/G5a-alpaca-volume.md`.
