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
rate-limit (429) and token-expiry faults stop. The report lists `faults` and
`recoveries` (fresh events per recovered attempt) and keeps a capture deadline
(`CAPTURE_COMPLETE`, `deadline_reached`) distinct from a provider fault. Token renewal
for an all-day service is **not implemented** (`token_renewal` says so).

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
reviewed records in one JSON file (`DESK_QUOTE_MAPPINGS`, mode 0600, atomic writes):

- Bound (digest): desk symbol; Webull host, instrument ID, currency; tastytrade
  environment, provider symbol, streamer symbol and identifier (the CUSIP when supplied).
- Supporting evidence (not bound): names, exchange/listed market, capture digests.
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
`QUOTE_MAPPING_WEBULL_HOST_UNKNOWN`) → Profile not HALTED → fresh same-generation trade.
Failures are per-symbol `QUOTE_MAPPING_*`/`SECURITY_HALTED`/quote codes, shown on the
ticket; the signal's state, levels, expiry and revision evidence are untouched. Only a
verified quote reaches scanner revalidation, where the unchanged rules apply (a verified
price through the stop still invalidates the signal). No price-gap threshold, Webull
close comparison or name similarity is used as issuer proof.

**Operational mappings: none exist.** Tests use labelled fixture records only.

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
- Final transaction lock order: ticket → signal → volume/identity (when required) →
  local quote health (in-process lock; the mapping file is read here, no network) →
  account. The fence re-verifies identity, mapping, halt status and the latest
  same-generation trade, and reruns the existing risk rules on that price: normal
  allowed movement needs no new version; a stop/chase/entry failure refuses.

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

## Read-only host commands — run only after Astra's review of this commit

From the updated clean iMac checkout, with the existing private env file. Credentials
are entered privately (`python tools/setup_tastytrade_env.py`, hidden input, mode 0600,
other settings preserved); never paste keys into chat or a command line.

```sh
cd "$HOME/Trading-Agent"
source .venv/bin/activate
source "$HOME/.config/trading-desk/env"
sntp time.apple.com                      # read-only offset check; record the result
python -m pip install -e '.[dev,quotes]'
python -m pytest -q -W error
desk_quote_report_dir=$(mktemp -d "$HOME/Desktop/g5-quotes-XXXXXX")
python -m desk.quote_check --environment production --symbols SPY QQQ NVDA \
  --option-underlying SPY --seconds 120 --reconnects 1 --max-requests 20 \
  --output "$desk_quote_report_dir/quotes.json"
python -m desk.quote_check --environment production --symbols SPY --profile \
  --seconds 30 --reconnects 0 --max-requests 6 \
  --output "$desk_quote_report_dir/profile.json"
```

Option pair: the nearest non-expired Standard expiry on the ET market date (today's
only before today's close), at the listed strike nearest the observed SPY trade
(`NEAR_OBSERVED_UNDERLYING`, with the reference price and its age); `--option-strike N`
picks the strike nearest N instead. Off-hours, `--option-median-fallback` picks the
median listed strike, labelled `NOT_REPRESENTATIVE_MEDIAN_STRIKE`. This is diagnostic
selection, not a recommendation. The report separates stocks and options, socket
liveness, identity capture (for a later mapping review), receipt times, signed
receipt-minus-source lag, trade age, side-change ages, trading status, faults and
recoveries. `lag_evidence` is `AVAILABLE_WITHIN_QUOTE_POLICY`,
`INCONSISTENT_WITH_QUOTE_POLICY`, `UNAVAILABLE` or (closed session)
`CLOSED_SESSION_AGES_ONLY_NOT_LIVE_EVIDENCE`. Exit 0 means observations were recorded;
it is never a live-trading sign-off. No scan, scheduler, ticket, mapping write, broker
account operation, dry-run or order endpoint is invoked.

Outside the XNYS regular session the output says `LIVE_TIMING=NOT_TESTED_MARKET_CLOSED`;
during it, `OBSERVATIONS_REQUIRE_REVIEW`. Source times must be reviewed against
independent simultaneous observations before child Step 5 acceptance.

Rollback: revert the child-3 commit to return to `78da588`; no host state is changed by
the code. A mapping file, if one is later created, is ignored by older code.
