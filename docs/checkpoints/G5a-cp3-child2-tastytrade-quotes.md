# G5a parent Checkpoint 3, child Step 2 — tastytrade quote adapter

Status: implementation and offline verification; pending child Step 3 review. Trader-day
functions: watch, analyze and approve through independent quote revalidation.
Implementer: Astra, taking over from Claude at Taz's explicit request. Claude read
the interfaces and wrote requirements but pushed no code. Base:
`b747904ac13c5f95c89bcc3cd6b9e40519c2b573`. Child Step 1's saved-volume audit and
standalone repairs are accepted; parent Checkpoint 3 and G5 remain open.

## Requirements and evidence

- User policy: use the best accepted source for each purpose. Retain Webull
  historical prices and Alpaca historical SIP volume. Do not use tastytrade Candle
  volume for EP, mix volume into VWAP, relax delay guards or change trading rules.
- Sourced: [tastytrade OAuth](https://developer.tastytrade.com/docs/authentication/oauth2/)
  specifies the production host, required User-Agent, personal refresh grant and
  optional read scope. Client ID is omitted, consistent with the successful reported
  check. Credential grants do not establish live timing acceptance.
- Sourced: [streaming protocol](https://developer.tastytrade.com/docs/guides/stream-market-data/)
  specifies quote-token exchange, returned WSS endpoint, DXLink handshake,
  negotiated fields, keepalive and streamer symbols.
- Sourced: [Quote](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Quote.html)
  has separate bid/ask change times; [Trade](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Trade.html)
  carries regular-session last-trade price/time. Receipt/heartbeat/zero eventTime
  cannot supply fresh market timestamps. Trade is not an ETH last-price feed.
- Checked, user-relayed: production grant and historical DXLink access worked on
  the unfunded account. Raw decoding and current stock/option freshness remain
  unverified. Tastytrade first30 volumes differ materially from reported SIP values.
- Engineering assumptions: configurable transport deadlines/reconnect bounds and
  metadata validity are explicit; existing risk quote age is unchanged. Missing
  provider fields remain unavailable, not fabricated.

## Producer/consumer dependency table

| Producer/interface | Consumer | Change and boundary |
| --- | --- | --- |
| Read-only OAuth, instrument metadata, quote token | Single DXLink session | Explicit environment; TLS/hostname checks; no account/order routes or raw secret logging |
| FEED_CONFIG + Quote/Trade events | Typed immutable quote observations | Canonical/provider/streamer identity; Decimal and source times; per-symbol faults |
| Connection/identity health + validated Trade | EventRiskSource bridge | Trusted tuple comes from service, never analyst/proposal fields; fresh generation required |
| Bridge provenance | Risk terms and ticket binding | Provider/environment/instrument identity independently bound; caller source claims checked; timestamps excluded from semantic binding |
| Local health fence | Final ticket transaction | Signal → volume/identity → quote health → account lock order; no network under locks; disconnected/replaced evidence refuses commit |
| Quote observations + metadata | Read-only diagnostic | Scratch evidence only; separate stock/option statuses; off-hours is NOT_TESTED |
| Webull/Alpaca providers | Existing scanner, indicators, EP | Unchanged; no automatic runtime factory, scheduler or trading activation |

## Acceptance examples recorded before implementation

1. A correct negotiated field map delivers SPY Trade price/time to scanner
   revalidation, while distinct bid/ask times remain observable.
2. An old provider timestamp received now stays old; zero/missing/future times,
   booleans/nonfinite data and out-of-order updates do not refresh evidence.
3. One malformed ticker/option row cannot discard healthy peer observations.
4. Disconnect, token expiry or failed identity refresh makes affected evidence
   unavailable. Reconnect/restart cannot revive prior-generation cached prices.
5. A proposal claiming another source is refused. Identity/environment/generation
   changes cannot reuse a ticket; unchanged fresh prices do not force version churn.
6. Disconnect/identity failure after recheck and before the final write is fenced
   locally. No API call is made inside transaction locks.
7. Quote-only capability does not fabricate account state, open interest,
   option deliverables/increments/valuation or the whole live RiskInputs factory.
8. Closed-session diagnostics show historical snapshots without claiming live PASS.
   The future regular-session command resolves real SPY call/put metadata and reports
   their quote freshness separately from equity trades.

## Tests, unresolved acceptance and rollback

Use deterministic transport/clock fixtures for protocol, identity, timestamp,
disconnect/recovery and final ticket integration; strict combined suite; actual
Python and dependency versions recorded. No provider calls in tests. Reuse accepted
unaffected G5 tests/evidence; no long backtest or profitability claim.

Child Step 3 code audit, child Step 4 reviewed-commit iMac verification and key setup,
child Step 5 regular-session timing, and child Step 6 remaining G5 acceptance stay
open. Immediate consolidated volume remains unresolved. Option quotes alone cannot
qualify options without other independently verified metadata/liquidity/account
inputs. A deterministic Friday replay is not an actual scheduled-Friday build.

Plan B: retain historical scanning and mark fresh quote-dependent decisions
unavailable. No guessed data or REST receipt timestamp fallback. Rollback: return
code to the recorded base without reinterpreting quote-backed tickets as legacy;
new provenance does not rewrite saved approvals. Restore a pre-upgrade database
backup if reverting an actual host; this implementation does not touch host state.

## Implementation and review record (2026-10-05)

Changes: `tastytrade_quotes.py` (identity, immutable observations, negotiated
decoder, per-symbol health); `tastytrade_transport.py` (read-only REST, TLS,
handshake and bounded capture); `quote_risk.py` (independent event bridge);
`quote_check.py` (explicit scratch diagnostic). Additive `QuoteProvenance`/held
quote snapshot in `risk_terms.py`, source-claim check in `risk.py`, provenance
binding/display and latest-quote final risk rerun in `tickets.py`. Four new test
files cover quotes, risk/ticket integration, diagnostic evidence limits and hidden
env setup. `tools/setup_tastytrade_env.py` is manual, terminal-only and makes no
network calls; it preserves other settings, writes atomically with mode 0600 and
refuses invalid values/symlinks. `pyproject.toml` adds optional pinned websockets
17.1; `.env.example`, `CLAUDE.md`, quote documentation and G5 matrix are updated.

Self-review discoveries, fixed before delivery:

- A second signal-store transaction inside the held fence caused lock failure.
  The fence now supplies the symbol through its existing locked view.
- Checking only quote generation/identity would miss a price crossing the stop
  after recheck. Rejecting every changed price would also obstruct normal quotes.
  The final local snapshot supplies latest Trade price/time to the normal risk
  rerun, retaining the same structural terms/provenance. Stop/entry/chase checks
  fail closed; ordinary permitted movement needs no new version.
- Protocol/denial/invalid-JSON/socket failures invalidate before teardown waits;
  malformed endpoint ports produce a fixed safe error and no repeated calls.

Test fixtures are labelled synthetic: no provider/account data call, no human
approval or order. The complete signal → quote → stop sizing → typed budget →
approval → single-use consumption path passes with ten shares and $36 total stop
risk in its fixture; another request cannot consume the same approval. Identity,
reconnect, expiry, disconnect, stop-crossing and final writer races are covered.
Only the independent same-generation current price/time can refresh in the final
rerun; source/identity changes require a new version. QuoteService has no bars or
decision-volume interface. Existing G5 tests cover historical Thursday/Friday
cutoffs, short sessions/calendar, revisions/dividend versus genuine gaps, provider
recovery/restart, optional earnings isolation and volume fences.

Runtime tested: macOS arm64 Python 3.12.14; websockets 17.1, pydantic 2.13.5,
numpy 2.5.3, pandas 3.0.6, TA-Lib 0.8.1, exchange-calendars 4.13.2, pytest 9.1.1.
No other supported stable Python was available locally (installed system 3.15 is
development Python). The iMac Python 3.14.7 run remains child Step 4, not assumed.
Final strict command/result and diff verification are recorded below. Earlier runs
(1467 full tests; 104 affected tests before two URL
cases; 65 final quote/setup tests) are development evidence, not host acceptance.

Child Step 2 code is usable for testing, without a complete operational factory.
No actual API quote timing, options entitlement or reconnect was tested. This is
Astra's implementer/self-check record, not independent child Step 3 sign-off.

| Command | Final result | Evidence type |
| --- | --- | --- |
| `python -m pytest -q -W error` | **1475 passed in 119.70s** | [S], Python 3.12.14, exact final code |
| Four new quote/risk/diagnostic/setup files | **65 passed in 1.14s** | [S], zero provider calls |
| Existing scoped history/partial discovery/vendor basis/earnings isolation plus new quote suites (earlier development run) | 175 passed in 29.63s | [S]; included again in final full suite |
| Ticket/stop-risk/volume lifecycle and CP2 audit suites plus quote suites (earlier development run) | 234 passed in 19.14s | [S]; included again in final full suite |
| `git diff --check` | Clean | Formatting check |
| `python -m desk.quote_check --help` | Successful, committed CLI names/options | No provider call |

The full run includes legacy approval/budget/warnings/options safeguards and all
existing calendar/revision/dividend/volume acceptance tests. No profitability,
scheduled build, actual current-session timing or iMac result is inferred. The
new code's parent is b747904; the delivered commit/remote SHA and host command block
are provided in the handoff rather than embedding a self-referential SHA here.
