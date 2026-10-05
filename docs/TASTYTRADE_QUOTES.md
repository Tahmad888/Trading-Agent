# Current quotes through tastytrade

G5 → G5a parent Checkpoint 3 → child Step 2. This is a read-only quote adapter and
diagnostic, with offline ticket integration. It does not activate a runner or
provide a complete live `RiskInputs` factory. Current stock/option timing and
entitlement acceptance await child Step 5 on the reviewed commit.

## Source responsibilities

| Data | Source | What this change does |
| --- | --- | --- |
| Historical/chart prices | Existing validated Webull pipeline | Unchanged |
| Liquidity, VCP/cup and EP decision volume | Existing historical Alpaca SIP pipeline | Unchanged; free-SIP delay/finality guards remain |
| Current equity last-trade price/time | tastytrade DXLink `Trade` | Independent signal revalidation through `TastytradeRiskSource` |
| Stock/option bid/ask and sizes | tastytrade DXLink `Quote` | Separate bid/ask timestamps, identity and freshness reads |
| Immediate consolidated intraday volume | Unresolved | No Candle/dayVolume substitution or threshold change |

The accepted saved-volume audit found large differences between tastytrade first30
volume and the reported SIP first30 volumes. Their cause remains unknown. This
adapter never requests Candle, Summary or dayVolume and cannot supply bars,
decision volume or VWAP. The EP approved card, 50-session denominator and 0.5
threshold are unchanged.

## Evidence and protocol

Primary sources, checked 2026-10-04:

- [OAuth2](https://developer.tastytrade.com/docs/authentication/oauth2/): personal
  refresh grant, required User-Agent, production/sandbox hosts, optional read scope.
- [DXLink guide](https://developer.tastytrade.com/docs/guides/stream-market-data/):
  quote token, handshake, negotiated field map and compact arrays, subscriptions,
  keepalive. The returned WSS endpoint is used; the accepted domain family is
  `*.dxfeed.com`. An unrecognized family requires review rather than forwarding a token.
- [Quote-token reference](https://developer.tastytrade.com/reference/accounts-and-customers/getApiQuoteTokens/):
  explicit ISO expiry and entitlement label. `api` is recorded without inferring a
  verified live-data level. The documented 24-hour lifetime is an upper bound;
  the actual returned expiry controls eligibility.
- [Instruments](https://developer.tastytrade.com/openapi/instruments.json): actual
  equity ID/CUSIP, streamer symbol, OCC option symbol and explicit contract fields.
  The nested option chain supplies actual call/put symbols; individual instruments
  are fetched and checked before subscribing.
- [dxFeed Quote](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Quote.html)
  and [Trade](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Trade.html):
  bid/ask change times and regular-session last-trade time are distinct. Trade is
  not an extended-hours last-price feed.

Engineering assumptions, not retail trading rules: metadata lifetime 24 hours;
REST timeout 15 seconds; WS connect/handshake 10 seconds, close 2 seconds;
bounded capture 1–600 seconds, 0–2 reconnects, 1/2-second reconnect backoff;
default 20 REST requests including OAuth/metadata/token refreshes, configurable
1–100. These bounds constrain the diagnostic's work, not which trades may be seen.
The existing risk quote-age policy supplies the default freshness limit; no trading
threshold or budget is changed.

## Interfaces and refusal behavior

`ReadClient(Credentials(...), environment="production")` allows only OAuth refresh,
quote-token exchange and read-only equity/option/chain metadata routes. It sends
`scope=read`, omits client ID, refuses redirects, and retains secrets only in memory.
Provider bodies, headers, auth frames and credentialed URLs are never reported.
`SSL_CERT_FILE` is honored by default verified TLS with hostname verification.
Install the optional transport dependency with `python -m pip install -e '.[dev,quotes]'`.

`QuoteService` contains immutable `Instrument`, `Quote` and `Trade` observations.
JSON decimals are preserved. Source times must be positive integer milliseconds
and cannot be future times. Missing/zero times, bool/nonfinite/negative numbers and
unknown identity do not qualify. Older events are ignored without advancing time;
malformed events for a known symbol withhold that component until a valid update.
Healthy peers remain usable. Quote reads require both sides fresh, positive prices,
nonzero sizes and an unlocked, uncrossed market. Locked/crossed/zero-size failures
are reported separately. A stale historical observation can appear
only as `last_observation_not_eligible` in diagnostic evidence.

FEED_CONFIG supplies actual field ordering/stride. Missing required fields or a
malformed schema stops this session. Each new connection gets a unique generation;
disconnect, denial, expiry and protocol failure invalidate observations before
socket teardown. Reconnect fetches a new token and requires new valid events.
No live cache survives process restart. Receipt times and KEEPALIVE never freshen
market evidence. Transport recovery is bounded; denial/schema/rate-limit errors
are not retried. The diagnostic reports recovery attempts and stops, but a real
provider reconnect/recovery check remains future acceptance.

To connect to independent risk terms, pass the actual historical source, `ScanLog`
and active `QuoteService` into `TastytradeRiskSource`. Its `resolve(event_id, now)`
uses the underlying's validated Trade price/time and normal scanner revalidation.
It adds independently derived source/environment/instrument/digest/generation
provenance. A caller's `TicketRequest.quote_source` must equal `tastytrade-dxlink`;
the risk engine verifies that claim. Ticket binding includes actual provenance,
not varying receipt timestamps. Identical fresh prices in the same generation do
not require a new ticket. A changed identity, environment or generation does.

The final transaction holds ticket → signal → volume/identity (when required) →
local quote health → account locks until COMMIT. The signal is read through the
already held view. No network call occurs under these locks. An intervening
disconnect, invalid identity, stale trade or generation change refuses the final
write. The latest same-generation Trade price/time reruns the existing risk rules:
ordinary allowed movement needs no new version, while stop/chase/entry failures
refuse the write. Writers arriving after the fence wait until COMMIT.
This in-process service is not a cross-process shared quote cache: another process
has a different generation and cannot reuse quote-backed approval evidence.

## Options and missing runtime inputs

`service.quote(canonical_occ, now, max_age)` supplies actual option bid/ask, sizes
and side timestamps. `Instrument` checks the supplied underlying, expiry, right
and strike against the OCC symbol. It is partial identity metadata, **not** a
`ContractBook`. `shares-per-contract` or `settlement-type=PM` alone does not attest
deliverables, adjusted status, cash settlement or a price increment.

This adapter does not fabricate account/risk snapshots, market regime, full option
deliverables/increments, open interest, exit valuation or broker funding. A caller
must supply other independent `RiskInputs` adapters. Missing required option
inputs remain blocking under existing risk checks. Bid/ask are available for a
later planner/observer integration; quote-only installation does not qualify an
option ticket or authorize an order.

## Read-only host command after child Step 3 review

From the updated clean iMac checkout, activate its venv and source the existing
private env file. Credentials must be entered privately in child Step 4; do not
paste keys into chat or put them in a command line. Preserve Webull, Alpaca, Massive
and `SSL_CERT_FILE`. The command writes only an explicit scratch report:

```sh
cd "$HOME/Trading-Agent"
source .venv/bin/activate
source "$HOME/.config/trading-desk/env"
python -m pip install -e '.[dev,quotes]'
python -m pytest -q -W error
desk_quote_report_dir=$(mktemp -d "$HOME/Desktop/g5-quotes-XXXXXX")
python -m desk.quote_check --environment production --symbols SPY QQQ NVDA \
  --option-underlying SPY --seconds 120 --reconnects 1 --max-requests 20 \
  --output "$desk_quote_report_dir/quotes.json"
```

The pair is the nearest non-expired Standard expiry and median listed strike;
`--option-strike 770` instead requests the nearest listed strike to that number.
This is diagnostic selection, not a recommendation. Stock and option results are
separate. Commit, tracked-tree cleanliness, Python, request count, negotiated-schema
acceptance, authentication observations, entitlement label, prices, side/trade ages,
event counts and generations are reported. Exit 0 means observations were recorded
in a completed capture; it is never a live-trading sign-off. No scan, scheduler,
ticket, broker account operation, dry-run or order endpoint is invoked.

Outside the XNYS regular session (including holidays/early closes), output says
`LIVE_TIMING=NOT_TESTED_MARKET_CLOSED`. During the session it says
`OBSERVATIONS_REQUIRE_REVIEW`. Provider source times/updates, missing fields,
identity and recovery evidence must be reviewed against independent simultaneous
observations before child Step 5 acceptance. No closed-market comparison proves
real-time latency.
