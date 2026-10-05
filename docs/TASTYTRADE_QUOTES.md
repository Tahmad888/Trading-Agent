# Current quotes through tastytrade

G5 → G5a parent Checkpoint 3 → child Step 2 (adapter, Astra) and child Step 3 (review
repairs, Claude; record in `checkpoints/G5a-cp3-child3-quote-repairs.md`). This is a
read-only quote adapter and diagnostic, with offline ticket integration. It does not
activate a runner or provide a complete live `RiskInputs` factory. Current stock/option
timing and entitlement acceptance await child Step 5 on the independently reviewed commit.

## Source responsibilities

| Data | Source | What this change does |
| --- | --- | --- |
| Historical/chart prices | Existing validated Webull pipeline | Unchanged |
| Liquidity, VCP/cup and EP decision volume | Existing historical Alpaca SIP pipeline | Unchanged; free-SIP delay/finality guards remain |
| Current equity last-trade price/time | tastytrade DXLink `Trade` | Independent signal revalidation through `TastytradeRiskSource`, only after a reviewed identity mapping is verified |
| Stock/option bid/ask and sizes | tastytrade DXLink `Quote` | Separate side-change times, identity and freshness reads |
| Trading status | tastytrade DXLink `Profile` (optional, separate channel) | Known HALTED refuses; UNDEFINED/missing is unknown, never ACTIVE |
| Immediate consolidated intraday volume | Unresolved | No Candle/dayVolume substitution or threshold change |

The accepted saved-volume audit found large differences between tastytrade first30
volume and the reported SIP first30 volumes; their cause remains unknown. This adapter
never requests Candle, Summary or dayVolume and cannot supply bars, decision volume
or VWAP. The EP approved card, 50-session denominator and 0.5 threshold are unchanged.

## Evidence and protocol

Primary sources, checked 2026-10-04 (child 2) and 2026-10-05 (child 3):

- [OAuth2](https://developer.tastytrade.com/docs/authentication/oauth2/): JSON refresh
  grant with `client_secret` and `refresh_token`; `client_id` optional (not sent);
  `scope` optional (`read` is sent); `User-Agent` required; access tokens last 15 minutes.
- [DXLink guide](https://developer.tastytrade.com/docs/guides/stream-market-data/):
  quote token, returned WSS endpoint, handshake, ~30 s keepalive, streamer symbols.
- [dxLink specification](https://github.com/dxFeed/dxLink/blob/main/dxlink-specification/asyncapi.yml):
  `FEED_CONFIG` "can send … after receiving the `FEED_SETUP` message", again "if the
  `FEED` service configuration has changed", and is "lazy therefore the server may not
  send the notification immediately, but before the first `FEED_DATA` is sent".
  `eventFields` is optional and keyed by event type; the specification's example
  subscribes first and receives a Quote-only map just before Quote data. Each side must
  send a message before the other's keepalive timeout. ERROR "is for informational
  purposes only"; this desk still withholds evidence when one arrives (below).
- [Quote-token reference](https://developer.tastytrade.com/reference/accounts-and-customers/getApiQuoteTokens/):
  explicit ISO expiry and entitlement label (`api` recorded without inferring a level).
- [Instruments](https://developer.tastytrade.com/openapi/instruments.json): equity
  `cusip`, `description`, `listed-market`, `is-etf`, `streamer-symbol`; OCC option fields.
- [dxFeed Quote](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Quote.html):
  bid/ask times are the last *change* times, normally seconds precision. An old
  side-change time is not evidence of a delayed feed.
- [dxFeed Trade](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Trade.html):
  `time` is the last regular-trading-hours trade; extended-hours trades do not move it;
  after the daily reset it keeps the last RTH trade until a new one.
- [dxFeed Profile](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Profile.html)
  and [TradingStatus](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/TradingStatus.html):
  ACTIVE, HALTED, UNDEFINED ("undefined, unknown or inapplicable"); halt start/end times.
  Documentation does not prove this account receives Profile.

### Handshake and schema (child 3, F1/F2)

`SETUP` → `AUTH_STATE`/`AUTH` → `CHANNEL_REQUEST` (channel 3). On `CHANNEL_OPENED` the
session is transport-ready: it sends `FEED_SETUP` and one `FEED_SUBSCRIPTION` (reset, the
initial identities) without waiting for a field map. Schema availability is separate:
each event type is decoded only with the server's accepted map for that type.

- Maps merge per type (Assumption E1): a later `FEED_CONFIG` replaces only the types it
  names. A fieldless config is accepted and waited on.
- An identical repeated config changes nothing. A changed order is adopted for later
  data and withholds that type's earlier values (`FEED_SCHEMA_CHANGED`) until new events.
- A map for one type that misses a required field or repeats a name withholds that type
  only (`FEED_SCHEMA_UNSUPPORTED`; its data is then undecodable). A non-`COMPACT` format,
  a malformed `eventFields`, or no remaining usable Quote/Trade map withholds and stops
  the session. Trade `size` is optional; Quote needs every requested field.
- Data of a type with no accepted map is counted as `undecodable` and never decoded.
- Bounds (engineering): 10 s from SETUP to channel open (`DXLINK_HANDSHAKE_TIMEOUT`); 10 s
  from subscription to the first accepted Quote or Trade map (`DXLINK_SCHEMA_UNAVAILABLE`).
- `ERROR` on channel 0 or 3 (`DXLINK_ERROR_<type>`) and `CHANNEL_CLOSED` stop the
  session. Profile runs on channel 5 (Assumption E3); an error, closure or invalid map
  there withholds Profile only. Profile is opt-in (`--profile`) until its delivery is seen.

### Recovery (F10)

A heartbeat timeout or socket failure clears observations at once. Within the same
bounded attempt count (0–2 reconnects), duration (1–600 s) and REST budget, the capture
gets a fresh token and generation and needs new events. Denial, protocol/schema,
rate-limit (429) and token-expiry faults stop. The report lists `faults`, an
`attempt_log` (each attempt's generation, outcome and per-symbol Quote/Trade/Profile
receipt), `final_attempt`, and `recoveries` by symbol and component. Each attempt also
keeps a **terminal view** (child 3, D1): the existing getters run once at the attempt's
end, before anything is closed on a deliberate finite end, and after the service has
withdrawn its caches on a fault (so a cleared value is never revived). Two facts stay
separate per symbol: `components` (received in this generation) and `usable_at_end`
(accepted by the existing age, BBO, schema, halt and identity rules at that moment), with
an aggregate `health`. A schema withdrawal, status change, identity failure or transport
fault after the last data message therefore shows. A recovery's `outcome` is
`USABLE_AT_ATTEMPT_END`, `RECEIVED_BUT_NOT_USABLE_AT_ATTEMPT_END`, `PARTIALLY_RECEIVED` or
`NOT_RECEIVED`, and `recovered` is true only for the first: every subscribed symbol's
Quote and Trade usable at the end. Profile alone or one healthy peer is not recovery
(re-audit R4). A capture deadline (`CAPTURE_COMPLETE`, `deadline_reached`,
`deliberate_end`) stays distinct from a provider fault.
Token renewal for an all-day service is **not implemented** (`token_renewal` says so).

## Interfaces and refusal behavior

`ReadClient(Credentials(...), environment="production")` allows only OAuth refresh,
quote-token exchange and read-only equity/option/chain metadata routes. It refuses
redirects and sends the quote token only to `wss://<sub>.dxfeed.com[:443]/…` with no
userinfo, query or fragment (`stream_endpoint_allowed`, checked again in `connect_ws`).
Provider bodies, headers, auth frames and credentialed URLs are never reported.
`SSL_CERT_FILE` is honored by default verified TLS with hostname verification. Install
the optional transport dependency with `python -m pip install -e '.[dev,quotes]'`.

`QuoteService` keeps immutable `Instrument`, `Quote`, `Trade` and `Profile`
observations. JSON decimals are preserved. Refusal codes:

| Situation | Code | Notes |
| --- | --- | --- |
| Source time after local receipt | `FUTURE_SOURCE_TIME` | Never clamped; diagnostic shows `lead_ms` and a clock-uncertainty explanation |
| Trade time 0/missing | `SOURCE_TIME_UNAVAILABLE` | Receipt time never substitutes |
| Trade price missing/NaN/0 | `TRADE_PRICE_UNAVAILABLE` | |
| Trade size NaN/missing | (none) | Price/time kept; `size_status: UNAVAILABLE` |
| Boolean, negative, infinite value | `INVALID_NUMBER` | Row rejected |
| Bid/ask NaN or missing | `QUOTE_BID_UNAVAILABLE` / `QUOTE_ASK_UNAVAILABLE` | |
| Bid/ask zero | `QUOTE_NO_BID` / `QUOTE_NO_ASK` | |
| Side time 0/missing | `QUOTE_TIME_UNAVAILABLE` | |
| Side change older than policy | `QUOTE_STALE` | `SIDE_CHANGE_AGE_EXCEEDS_QUOTE_POLICY_NOT_DELAY_EVIDENCE` |
| Size missing / zero | `QUOTE_SIZE_UNAVAILABLE` / `QUOTE_ZERO_SIZE` | |
| Locked / crossed | `QUOTE_LOCKED` / `QUOTE_CROSSED` | Never clamped |
| One side older, the other newer | `QUOTE_ORDER_AMBIGUOUS` | Earlier BBO withheld too (Assumption E5) |

Ordering: a purely older Quote or Trade is ignored and the retained value stays as it
was. A mixed-direction Quote withholds that symbol's Quote; recovery needs a snapshot at
or after both retained side watermarks, or a new connection generation (watermarks are
cleared on disconnect). Healthy peers stay usable throughout.

Each connection gets a unique generation; disconnect, denial, expiry and protocol
failure invalidate observations before socket teardown. KEEPALIVE and receipt times
never freshen market evidence; any message after the service was disconnected stops
that session (`SESSION_NOT_CONNECTED`) without reviving it, and a superseded session
cannot disturb a newer one. No live cache survives process restart.

## Identity mapping (child 3, F7)

Webull's metadata has its own instrument ID, name, exchange, sub-category and currency,
but no CUSIP/FIGI (`evidence/step06-native-channels.md`); tastytrade has a CUSIP. With
no shared identifier, no mapping is derived automatically. `desk.quote_mapping` keeps
reviewed records in one JSON file (`DESK_QUOTE_MAPPINGS`, schema
`desk-quote-mappings-v2`, mode 0600, atomic writes):

- Bound (digest): desk symbol; Webull host, instrument ID, currency and sub-category
  (COMMON_STOCK/ETF); tastytrade environment, provider symbol, streamer symbol,
  identifier (the CUSIP when supplied) and `is-etf` (re-audit R2).
- Supporting evidence (not bound): names, descriptions, exchange/listed market, review
  time and capture digests. Changing these never needs a new review.
- Classification is checked against the *current* identities on every verification:
  tastytrade `is-etf` (part of the instrument identity digest; only a real JSON boolean
  counts, so missing, `null`, `"false"` or other values are
  `QUOTE_MAPPING_CLASSIFICATION_UNAVAILABLE`) and the Webull identity the vendor path
  currently **verifies** for the host and symbol (`[instrument_id, currency,
  exchange_code, sub_category]`; read locally). Disagreement is
  `QUOTE_MAPPING_CLASSIFICATION_MISMATCH`. A pin alone is not current evidence (child 3,
  H1): see "Webull identity health" below. Limits: Webull has no CUSIP/FIGI;
  tastytrade `instrument-sub-type` values are not enumerated in its OpenAPI, so only
  `is-etf` is bound; a Webull reclassification shows at the next vendor fetch, not inside
  the no-network final fence.
- Migration: v1 records (no classification binding) are kept verbatim under
  `legacy_unverified` and refused (`QUOTE_MAPPING_REVIEW_REQUIRED`) until re-reviewed;
  invalid peer records are never erased by a write. None existed operationally.
- Fence (re-audit R1): verification takes a shared `fcntl.flock` on `<store>.lock`, a
  stable sidecar (the JSON itself is atomically replaced, so locking it would be
  bypassed by the rename); `add` (the review command) takes an exclusive lock after the
  reviewer confirms. Each acquisition opens its own descriptor, so threads and
  processes exclude each other ([flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html)).
  Waits are bounded (10 s); a busy, unreadable or unlockable store refuses
  (`QUOTE_MAPPING_STORE_BUSY`, `…_STORE_UNREADABLE`, `…_LOCK_UNAVAILABLE`). With no store
  file nothing is created and nothing verifies. There is no removal command; a review
  replaces that symbol's record.
- Automatic checks a review cannot override: both providers' symbols equal the desk
  symbol including share class (`BRK.B` ↔ `BRK/B`), the ETF flag agrees, the bound
  tastytrade identifier is the supplied CUSIP. They are necessary, not proof of issuer.
- Review: `python -m desk.quote_mapping review --store PATH --symbol X --webull-capture
  <desk.metadata_check report> --tastytrade-capture <desk.quote_check report> --reviewer
  NAME`, interactive terminal only, typed `MAP X`. The diagnostic never writes a mapping.
  A record stays valid while both bound identities stay unchanged; no daily retyping.

`TastytradeRiskSource(source, log, quotes, mappings)` verifies, before any signal-state
change: healthy tastytrade identity → reviewed mapping against the signal's own stored
Webull basis (`host`, `security_id`, `currency`, `symbol`; only the G3 vendor basis
records the host, so a legacy reviewed-ledger basis gives
`QUOTE_MAPPING_WEBULL_HOST_UNKNOWN`) and the Webull identity currently verified for it
→ no retained halt → fresh same-generation trade.
Failures are per-symbol `QUOTE_MAPPING_*`/`SECURITY_HALTED`/quote codes, shown on the
ticket; the signal's state, levels, expiry and revision evidence are untouched. Only a
verified quote reaches scanner revalidation, where the unchanged rules apply (a verified
price through the stop still invalidates the signal). No price-gap threshold, Webull
close comparison or name similarity is used as issuer proof.

**Operational mappings: none exist.** Tests use labelled fixture records only.

### Webull identity health (child 3, H1)

The vendor store (`VendorHistoryStore`, the G3 vendor-basis SQLite file) keeps each
host/symbol's first accepted pin, never overwritten, and an append-only
`identity_events` record. Every vendor fetch (`bars`, `discovery_bars`) commits a
`CHECK_OPENED` per symbol before the metadata request is sent; if it cannot, nothing is
requested. The check closes with exactly one outcome: `VERIFIED` (fresh metadata matched,
or first pinned, the identity), `FAILED` (`SECURITY_IDENTITY_CHANGED` for another
instrument ID, currency, exchange or common/ETF class, including metadata of the wrong
bar category or an unsupported type that contradicts the pin; `SECURITY_IDENTITY_ALIAS`),
with the contradictory observation kept beside the pin, or `NOT_OBSERVED` (metadata
missing, stale or unattributable). An outcome that cannot be committed leaves the check
open.

Current permission needs the latest outcome to be `VERIFIED` for exactly the current pin,
with no newer open check, and the pin to be the instrument the signal was armed on.
Otherwise a signal armed on Webull vendor evidence refuses, before quote lookup and
state-mutating revalidation and again in the final fence, with `WEBULL_IDENTITY_FAILED:
<reason>`, `…_REFRESH_UNRESOLVED`, `…_UNVERIFIED`, `…_HEALTH_INCONSISTENT`,
`…_NOT_PINNED`, `…_MISMATCH`, `…_STORE_UNAVAILABLE` or `…_STORE_BUSY`. This applies to the
generic event source and the quote bridge alike, per host/symbol only (peers are
unaffected). Bar observations, snapshots, scoped discovery evidence and history-row
defects never change identity health. Recovery is a later check whose fresh metadata
matches the original pin; the same reviewed mapping keeps working, no new review.

Migration: stores written before H1 have pins and no identity events; such a pin reads
`WEBULL_IDENTITY_UNVERIFIED` until the next normal vendor refresh of that symbol verifies
it (the scan already sends that metadata request). No manual entry.

## Ticket binding, reserved label and final fence

- `tastytrade-dxlink` is a reserved label (`risk_terms.RESERVED_QUOTE_SOURCES`). A request
  carrying it fails `quote_source_matches` unless the independently resolved terms hold
  provenance with that source, the ticket symbol, an allowed environment (`live` →
  production; `paper` → production or sandbox) and a mapping digest. The ticket is
  prepared visibly blocked, says the label is not backed, and cannot be approved or
  consumed. Honest legacy labels with legacy adapters work as before.
- Binding includes the provenance (source, environment, instrument, streamer, identity
  digest, mapping digest, generation), not receipt times. Fresh prices in the same
  generation do not create versions.
- Final transaction lock order: ticket (`BEGIN EXCLUSIVE`) → signal → Alpaca
  volume/identity (when required) → Webull vendor identity (`BEGIN IMMEDIATE` on the
  vendor store, when the event names a vendor host; child 3, H1) → mapping (shared
  sidecar lock, held until COMMIT) → quote health (in-process lock) → account. All waits
  happen before the final clock; no network under any of them. Every writer of these
  stores holds only its own lock (a vendor identity write is one transaction), so no
  cycle exists. A contradictory identity outcome committed before the fence refuses; a
  writer arriving during it waits until COMMIT (or times out and leaves its check open,
  which refuses later uses). A busy fence (5 s), a missing store (never created) or an
  unreadable one refuses without leaving a lock held. The fence re-verifies identity, mapping and classification, halt status
  and the latest same-generation trade, and reruns the existing risk rules on that
  price: normal allowed movement needs no new version; a stop/chase/entry failure
  refuses. A writer arriving after the final check waits until COMMIT.
- Halts (re-audit R3): a HALTED Profile is latched per symbol. A Profile channel error
  or closure, an invalid map or row, UNDEFINED, a reconnect, a fresh Trade or an accepted
  map never clear it; only an ACTIVE Profile for that symbol in the current session
  does. No timer. While latched, `trade()` and `quote()` refuse with `SECURITY_HALTED`, so
  no halted price serves as current evidence, and the bridge refuses before revalidation
  and in the final fence. A superseded session cannot clear it. No Profile ever received
  stays UNKNOWN (never ACTIVE) and does not block Quote/Trade; tradability still comes
  from the separate required status input, which has not been verified against this
  account. The latch is in-process: a restarted process starts with no quotes and no
  latch, and approvals never carry across sessions. Exchange halts have no fixed length
  and end on an explicit resumption, which is why no timer clears the latch; the latch
  itself is this desk's engineering contract, not something the dxFeed enum prescribes.

### Quote sessions and the ticket CLI (F8)

A generation is one connection session. `python -m desk.tickets` builds its adapters
per command, so `prepare`, `approve` and `consume` run in separate processes and cannot
share one session: a quote-backed ticket prepared in one command is refused in the next
with "the quote session changed … approvals never carry across quote sessions". This is
deliberate; generation binding is not removed to make separate connections reuse
approvals. Future design (pending, not built): one long-lived desk quote service owns
the DXLink connection and token renewal, and ticket commands query it locally (IPC);
a disconnected or restarted service stays ineligible until fresh events and
revalidation. This checkpoint demonstrates prepare → approve → consume through one
injected service only.

## Clock uncertainty

No tolerance is adopted (Assumption E6). Future-dated events stay ineligible and are
reported with their lead. Before the live run, measure the iMac offset (read-only):
`sntp time.apple.com`. A calibration mechanism, if proposed later, needs a trusted time
source, units and sign convention, measured uncertainty, a validity period, handling of
clock jumps, and the same adjustment in producer and risk checks; it is an open
engineering decision for Astra/Taz, not part of this change.

## Options and missing runtime inputs

`service.quote(canonical_occ, now, max_age)` supplies option bid/ask, sizes and side
times. `Instrument` checks underlying, expiry, right and strike against the OCC symbol.
It is partial identity metadata, **not** a `ContractBook`. This adapter does not
fabricate account/risk snapshots, market regime, option deliverables/increments, open
interest, exit valuation, broker funding or tradability. Missing required inputs stay
blocking under existing risk checks.

## Read-only host commands — run only after Astra's review of the reviewed commit

Credentials are a separate, earlier step, run on its own: `python
tools/setup_tastytrade_env.py` (hidden input, mode 0600, other saved settings
preserved). Never paste keys into chat or a command line.

The checks run as one script so that a failed guard really stops everything after it
(`set -e` plus an explicit `exit 1` per guard; printing STOP alone would not). Paste the
three lines below. The middle line writes the script to a temporary file; the last line
runs it with the exact commit Astra signed off in place of `REVIEWED_SHA`.

```sh
desk_checks=$(mktemp "${TMPDIR:-/tmp}/desk-child4-checks.XXXXXX")
cat > "$desk_checks" <<'CHECKS'
set -eo pipefail
test -n "${1:-}" || { echo "STOP: give the reviewed commit SHA"; exit 1; }
cd "$HOME/Trading-Agent"
test -z "$(git status --porcelain --untracked-files=no)" || { echo "STOP: tracked files have local changes"; exit 1; }
test "$(git branch --show-current)" = "codex/repair-step-01-baseline" || { echo "STOP: wrong branch"; exit 1; }
git fetch origin codex/repair-step-01-baseline || { echo "STOP: fetch failed"; exit 1; }
git merge --ff-only origin/codex/repair-step-01-baseline || { echo "STOP: fast-forward failed"; exit 1; }
test "$(git rev-parse HEAD)" = "$1" || { echo "STOP: HEAD is not the reviewed commit"; exit 1; }
echo "SHA_OK $1"
source .venv/bin/activate
source "$HOME/.config/trading-desk/env"
python --version
python -m pip install -e '.[dev,quotes]' || { echo "STOP: install failed"; exit 1; }
python -m pytest -q -W error || { echo "STOP: strict suite failed"; exit 1; }
sntp time.apple.com || echo "sntp failed: host clock offset not measured"
desk_quote_report_dir=$(mktemp -d "$HOME/Desktop/g5-quotes-XXXXXX")
python -m desk.quote_check --environment production --symbols SPY QQQ NVDA --option-underlying SPY --seconds 120 --reconnects 1 --max-requests 20 --output "$desk_quote_report_dir/quotes.json" || echo "quotes.json written; non-zero exit means status is not OBSERVATIONS_ONLY"
python -m desk.quote_check --environment production --symbols SPY --profile --seconds 30 --reconnects 0 --max-requests 6 --output "$desk_quote_report_dir/profile.json" || echo "profile.json written; non-zero exit means status is not OBSERVATIONS_ONLY"
echo "Reports in $desk_quote_report_dir"
CHECKS
bash "$desk_checks" REVIEWED_SHA
```

Expected: no STOP line; `SHA_OK` with the reviewed commit; the suite passes; `sntp` only
prints the offset (no clock-setting flags are used). Any STOP ends the script before the
install, the suite or any provider request. The script runs in its own shell, so it never
closes the Terminal window, and the diagnostics write only to the new scratch folder.

Option pair: the nearest non-expired Standard expiry on the ET market date (today's
only before today's close), at the listed strike nearest the observed SPY trade
(`NEAR_OBSERVED_UNDERLYING`, with the reference price and its age); `--option-strike N`
picks the strike nearest N instead. Off-hours, `--option-median-fallback` picks the
median listed strike, labelled `NOT_REPRESENTATIVE_MEDIAN_STRIKE`. This is diagnostic
selection, not a recommendation. The report separates stocks and options, socket
liveness, identity capture (for a later mapping review), receipt times, signed
receipt-minus-source lag, trade age, side-change ages, trading status, faults and
recoveries. Stock/option rows, coverage and `lag_evidence` come from the final attempt's
terminal view (`final_attempt`: `components` received, `usable_at_end`, `health`;
re-audit R4 and child 3 D1). The final attempt's last data message is kept as
`final_attempt.last_data_view` and earlier connections' observations as
`historical_observations`, both ineligible. `status` is `OBSERVATIONS_ONLY` (deliberate
end after final-attempt observations), `FINAL_ATTEMPT_FAILED_AFTER_OBSERVATIONS` (data,
then a fault), `NO_FINAL_ATTEMPT_OBSERVATIONS` (the last connection delivered nothing) or
`UNAVAILABLE`. `lag_evidence` is `AVAILABLE_WITHIN_QUOTE_POLICY`,
`INCONSISTENT_WITH_QUOTE_POLICY`, `UNAVAILABLE` or (closed session)
`CLOSED_SESSION_AGES_ONLY_NOT_LIVE_EVIDENCE`. Exit 0 means observations were recorded;
it is never a live-trading sign-off. No scan, scheduler, ticket, mapping write, broker
account operation, dry-run or order endpoint is invoked.

Outside the XNYS regular session the output says `LIVE_TIMING=NOT_TESTED_MARKET_CLOSED`;
during it, `OBSERVATIONS_REQUIRE_REVIEW`. Source times must be reviewed against
independent simultaneous observations before child Step 5 acceptance.

Rollback: revert the child-3 commit to return to `78da588`; no host state is changed by
the code. A mapping file, if one is later created, is ignored by older code, and so is
the additive `identity_events` table in the vendor store.
