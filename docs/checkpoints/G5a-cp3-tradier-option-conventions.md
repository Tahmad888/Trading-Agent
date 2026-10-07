# G5a CP3 — Tradier option conventions (preflight Step 2) and the focused live comparison (Step 3)

2026-10-07. Taz authorized Astra's bounded implementation prompt as one batch (project
chat, 08:06Z), with his instruction to verify each fix independently and map every
affected consumer. Base `d539d97dcc1cae6c62568bef5dc650b6c175d227`; local checkout,
local branch and `origin/codex/repair-step-01-baseline` agree, working tree clean.
Hierarchy: G5 → G5a → parent Checkpoint 3 → the existing six-child outline. No new G
number, the parent stays open and Step 09 stays paused. Trader-day step: **analyze**
and **plan**, with correctly identified quotes and correctly labelled option analytics.
This is market-data development and read-only verification, not trading activation.

## Requirements before code

Evidence labels: **Documented** (a provider document states it), **Observed** (seen in
a saved or live sample), **Inferred** (reasoning from evidence), **Unresolved**.

| # | Requirement | Evidence | Label |
|---|---|---|---|
| R1 | Tradier `greeks.updated_at` (`YYYY-MM-DD HH:MM:SS`, no zone) is read as UTC, provider- and field-specific, with the raw string, parsed UTC, field, evidence reference, interpretation version and qualification retained. The global parsers are unchanged. | Tradier support answer of 2024-05-23 ("time is in UTC so 16:59 is about 13:00 EDT"), relayed on Wealth-Lab, not a current first-party schema: https://www.wealth-lab.com/Discussion/New-Support-for-Option-Chains-with-Tradier-Extension-11283 . Consistent with Tradier's own 2025-09-15 Quotes example: `updated_at` "2025-09-15 13:59:03" beside `bid_date` 1757948508000 ms (15:01:48Z); read as ET, the Greek time would lie three hours after its quote. https://docs.tradier.com/docs/quotes | Inferred (relayed vendor answer + example) |
| R2 | Tradier quote side/trade times (`bid_date`, `ask_date`, `trade_date`) are epoch milliseconds, UTC. Seconds-magnitude values are refused, not rescaled. | Current Quotes example uses ms; the older chain example (2021) shows seconds (1612196262), so the encoding has changed at least once. | Documented example + Observed (Taz's 10-07 run, ms) |
| R3 | Greek age is computed from the parsed Greek time; request, receipt and check times are separate; a negative age is kept as an anomaly; no Greek freshness threshold is invented; the quote's 60-second policy is not applied to Greeks. Tradier's hourly cadence is informational. | Tradier: "Greeks: Hourly" https://docs.tradier.com/docs/market-data ; Wealth-Lab staff: Greeks "don't really update until at least 10 minutes after the hour" (same thread). | Documented / relayed |
| R4 | ET display uses `ZoneInfo("America/New_York")`. | — | — |
| R5 | Greeks: raw value kept; normalized value exposed only where the source mapping is supported, per field, with unit, model, evidence and status. | See the field table. | — |
| R6 | Position exposure = raw per-share sensitivity × verified premium multiplier × signed contract quantity, once. Delta → share equivalents; gamma → share equivalents per $1; theta → $ per day; vega → $ per 1 IV point; rho → $ per 1 rate point. | Arithmetic definition; dxFeed documents per-day theta, per-percent vega and rho (https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/option/Greeks.html); ORATS documents theta "for one day", vega for "a one percent rise in the implied volatility", rho for "a one percent increase in interest rates" for its one-minute product (https://orats.com/one-minute-data). | Documented (tastytrade/dxFeed); upstream-documented, pass-through Inferred (Tradier) |
| R7 | No universal 100. The multiplier comes from the row's metadata (Tradier `contract_size`); missing, non-integer or conflicting terms give no exposure. Adjusted contracts are not silently standard. | OIC on adjusted contracts: https://www.optionseducation.org/optionsoverview/options-basics ; the desk already refuses non-standard contracts at ticket validation (`instruments.py`). | Documented |
| R8 | Tradier option `bidsize`/`asksize` (REST) and `bidsz`/`asksz` (stream) keep raw quantities, `size_unit_status=UNVERIFIED`, `interpreted_unit=CONTRACTS_PROVISIONAL`, and are excluded from automated arithmetic. REST and stream are separate encodings. | OPRA defines upstream sizes in contracts (2018 edition, §7.07/7.12/7.23); Tradier's Quotes page says "Size of bid (in hundreds)" for every quote; ORATS's one-minute product documents option sizes as contracts. No Tradier definition or OPRA-referenced Tradier option sample exists. | Unresolved |
| R9 | Tradier stock sizes: observed as shares in the 2026-10-07 eight-side sample only; never multiplied by 100 or `lot_size`. | `research/ai-trading/tradier-step2-alpaca-sip-quotes-2026-10-07/` | Observed (sample) |
| R10 | Tradier rho/phi are shared strike values (call and put both 0.0191 / −0.0192 in Taz's sample) and stay raw/informational. Tradier's put delta is already signed (call 0.449, put −0.551 in the same sample) and is never transformed again. | ORATS: one Greek set per strike, https://orats.com/blog/option-greeks-are-the-same-for-calls-and-puts ; ORATS scanner: "Per-share greeks (put deltas negative)", https://orats.com/docs/option-scanner-api | Observed + Documented |
| R11 | Tradier gamma, theta and vega: per share, per $1 / per day / per 1 IV point. Exposure is labelled PROVISIONAL, not supported, because Tradier itself documents no unit; whether the put row carries a put-specific theta is unresolved and is checked per pair (identical call/put values are flagged as shared). | ORATS one-minute definitions (upstream). A Black-Scholes fit of Tradier's own example corroborates the scale only (see report). | Inferred |
| R12 | IV: Tradier `bid_iv`/`mid_iv`/`ask_iv`/`smv_vol` and dxFeed `volatility` are decimal fractions by mapping (observed), displayed as percent only through that mapping, never by magnitude. | Tradier example `smv_vol` 0.358, `mid_iv` 0.3577; dxFeed `volatility` 0.1087 (live 10-07). | Observed |
| R13 | Validation: booleans, malformed and nonfinite values refuse that field only; zero and signed values are values; a missing Greek affects only its dependent analytics, not quotes, peers or other fields. HTTP-200 error bodies (`fault`, `errors`) are failures. | Repository conventions (`number_state`, `request_json`). | — |
| R14 | tastytrade Greeks stay raw observations: the existing measurement recorder has no indexed snapshot/transaction reduction. The conventions layer normalizes recorded samples offline and labels them `OBSERVATIONS_ONLY`; no current-state claim. | `quote_measure.GREEKS_NOTE`; dxFeed IndexedEvent semantics. | — |

## Producers and consumers

| Component | Change | Consumers affected |
|---|---|---|
| `src/desk/option_conventions.py` (new) | Field mappings, provider time parsing, Greek normalization, exposure, size and IV views, pair check, offline tastytrade Greeks normalizer CLI | New diagnostic only |
| `src/desk/tradier_option_check.py` (new) | Read-only Tradier REST diagnostic: GET `/v1/markets/quotes`, `/v1/markets/options/expirations`, `/v1/markets/options/chains`; bounded requests; token from `TRADIER_ACCESS_TOKEN` or a hidden prompt; refuses to overwrite a report | None (report file) |
| `src/desk/quote_measure.py` | Adds `TRADIER_ACCESS_TOKEN` to the credential guard's names | Every report written by `write_report` (guard is stricter, otherwise unchanged) |
| `src/desk/quote_measure.py` `number_state` | Accepts `Decimal` as a value type (found during this work: DXLink's `json_read` decodes numbers as `Decimal`, so at `d539d97` every live Greek, BBO price/size and `dayVolume` was recorded `INVALID`) | `quote_check --measure` reports (now record values); `webull_quote_check` (its values arrive as strings or floats, unchanged); booleans, NaN and Infinity still refused. No decision module reads `number_state` (isolation test). |
| Scanner, setups, quote service, quote mapping, quote risk bridge, risk engine, tickets, approval, Webull bars, Alpaca SIP volume, RTH30 measurement, source eligibility, quote-age policy, future-time policy, identity review, halts, schedules | **Unchanged.** Normalized analytics are not wired into tickets or risk in this repair; that integration stays open. | — |

No account, order, position or balance route; no support message; no schedule; no iMac
change. Webull stays market-data only.

## Acceptance examples (offline, deterministic)

1. Delta 0.50, multiplier 100, quantity +2 → +100 share equivalents; −2 → −100.
2. Put delta −0.50, multiplier 100, +2 → −100 (no second put conversion).
3. Theta −0.20 (documented daily convention), multiplier 100, +2 → −40 $/day; −1 → +20.
4. Multiplier 10 from metadata gives ×10; missing, zero, non-integer or conflicting terms give no exposure; nothing falls back to 100.
5. Raw option bid size 53 stays 53 regardless of multiplier or stock `lot_size`.
6. IV 0.20 (decimal mapping) displays as 20%; quantity never changes IV.
7. `2026-10-06 20:00:06` (Tradier, UTC interpretation) → 2026-10-06 16:00:06 EDT; `2026-12-07 20:00:06` → 15:00:06 EST.
8. Missing, malformed, explicit-offset, seconds-magnitude and future timestamps; distinct quote and Greek times; booleans, NaN/Infinity; valid zero and negative values.
9. Tradier rho/phi stay raw with no exposure.
10. A bad option's Greeks do not discard a good peer or valid prices; a later failed round leaves no current result.
11. No DXLink Greek reducer is added (R14), so its tests are not applicable.
12. Reports carry no credential value; HTTP-200 `fault`/`errors` bodies, denials and malformed replies never become observations.

## Unresolved provider definitions (handoff questions)

1. Tradier option `bidsize`/`asksize` and stream `bidsz`/`asksz`: contracts, or "hundreds"?
2. Tradier `greeks.updated_at`: confirm UTC as current schema (the 2024 support answer is relayed).
3. Tradier put rows: are theta, vega, rho and phi put-specific or the strike's shared (call) values?
4. Tradier theta: calendar day or trading day; vega and rho: per 1 percentage point?

## Rollback

`git revert <final commit>` on `codex/repair-step-01-baseline`. The two new modules
and their tests are additive. The edits to existing code are one tuple entry in
`quote_measure.CREDENTIAL_NAMES` and the `Decimal` acceptance in
`quote_measure.number_state`; reverting the latter brings back the all-`INVALID`
recording defect.

## Results

- Strict suite `python -m pytest -q -W error` (Python 3.13.16, cloud): **1918 passed**
  (base `d539d97`: 1872). `git diff --check` clean.
- Acceptance examples 1–10 and 12 have tests; 11 is not applicable (R14).
- Defect found while mapping consumers: `quote_measure.number_state` refused DXLink
  `Decimal` values. Two 15-second read-only tastytrade captures (2026-10-07, about
  04:17 ET) recorded INVALID 91 / VALUE 7 before the fix and INVALID 0 / VALUE 114 after.
  Files: project folder `research/ai-trading/tradier-option-conventions-2026-10-07/cloud-check/`.
- Live Step 3 comparison: **NOT_RUN** (market closed; no Tradier credential in the cloud).
  Tradier stream and controlled reconnect: NOT_RUN (not built in-repo).
- Option sizes: unresolved. Greek scaling: delta observed, gamma/theta/vega provisional,
  rho/phi raw. Greek time zone: inferred UTC. Handoff:
  `G5a-cp3-tradier-option-conventions-handoff.md`.
