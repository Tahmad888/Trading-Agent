# Option and quote field conventions

2026-10-08 after-hours update: current first-party Tradier documents still leave
the advertised-size contradiction unresolved; see TRADIER_SIZE_CLARIFICATION.md.
Six authentic saved tastytrade Greek observations were independently checked for
exact index/time, contract reference and documented fields; this does not restore
a live connection or certify a Greek freshness threshold. See TASTYTRADE_GREEKS.md.
The analytics import allowlist adds only `saved_greek_audit.py`, an offline analysis
tool; it does not admit conventions into signal, ticket or risk decision modules.

G5a CP3, Tradier preflight Step 2 (2026-10-07). Code: `src/desk/option_conventions.py`.
Diagnostic: `python -m desk.tradier_option_check`. Record:
`checkpoints/G5a-cp3-tradier-option-conventions.md`.

Status words: **documented** (the provider states it for this field), **observed** (seen in
a saved or live sample), **inferred** (reasoning or an upstream definition from another
endpoint), **unresolved**. A definition is never copied silently from one provider or
endpoint to another. Nothing here feeds risk, tickets, setups or approval yet.

## Field table

| Provider · endpoint/event | Asset | Raw field | Physical unit / increment | Meaning / model | Time format / zone | Evidence | Status |
|---|---|---|---|---|---|---|---|
| Tradier · REST `/v1/markets/quotes`, chains | stock | `bidsize`, `asksize` | shares in the sample; doc says "in hundreds" | quoted size | — | Alpaca SIP same-second match, 8 sides, 2026-10-07; [Tradier Quotes](https://docs.tradier.com/docs/quotes) | observed (sample); doc conflicts |
| Tradier · REST | stock | `lot_size` | shares per round lot (40/100 by price tier in sample) | round lot; never applied to sizes | — | Taz's 10-07 run | observed |
| Tradier · REST | option | `bidsize`, `asksize` | raw integer; `CONTRACTS_PROVISIONAL` | quoted size | — | OPRA defines contracts upstream ([spec 2018](https://uploads-ssl.webflow.com/5ba40927ac854d8c97bc92d7/5bf4197268f8b277dbb7c12d_opra_output_binary_dr_spec.pdf) §7.07/7.12/7.23); Tradier doc says "in hundreds" | **unresolved** |
| Tradier · stream `quote` event | option | `bidsz`, `asksz` | raw; `CONTRACTS_PROVISIONAL`; separate encoding from REST | quoted size | — | [Tradier Streaming](https://docs.tradier.com/docs/streaming) (no unit) | **unresolved** |
| Tradier · REST | both | `bid_date`, `ask_date`, `trade_date` | epoch **milliseconds** (seconds refused) | side / trade time | UTC epoch | Quotes example (2025, ms); older chain example (2021) used seconds | documented example + observed |
| Tradier · REST | option | `contract_size` | contracts → shares multiplier | premium multiplier (deliverable not on this route) | — | Quotes/chains schema | documented |
| Tradier · REST `greeks` | option | `delta` | per share, signed per contract | dPrice/dUnderlying | — | Taz's sample call 0.449 / put −0.551; [ORATS scanner](https://orats.com/docs/option-scanner-api) "put deltas negative" | observed |
| Tradier · REST `greeks` | option | `gamma` | per share per $1 | dDelta/dUnderlying | — | ORATS "one dollar increase" ([one-minute](https://orats.com/one-minute-data)) | inferred (upstream) |
| Tradier · REST `greeks` | option | `theta` | $ per share per day (calendar vs trading not stated) | time decay | — | ORATS "for one day"; fit of Tradier's example matches a 365-day scale | inferred (upstream) |
| Tradier · REST `greeks` | option | `vega` | $ per share per 1 IV point | IV sensitivity | — | ORATS "one percent rise in the implied volatility" | inferred (upstream) |
| Tradier · REST `greeks` | option | `rho`, `phi` | unstated | ORATS shared strike values (identical on call and put) | — | Taz's sample 0.0191 / −0.0192 on both; [ORATS blog](https://orats.com/blog/option-greeks-are-the-same-for-calls-and-puts) | observed; raw only |
| Tradier · REST `greeks` | option | `bid_iv`, `mid_iv`, `ask_iv`, `smv_vol` | decimal fraction | implied volatility; `smv_vol` = ORATS final IV | — | Quotes example 0.358 | observed |
| Tradier · REST `greeks` | option | `updated_at` | `YYYY-MM-DD HH:MM:SS`, no zone | ORATS calculation update time; quote-input time not documented; hourly cadence | read as **UTC** | Tradier support 2024-05-23, relayed on [Wealth-Lab](https://www.wealth-lab.com/Discussion/New-Support-for-Option-Chains-with-Tradier-Extension-11283); consistent with the 2025 Quotes example | inferred (relayed vendor answer) |
| tastytrade · DXLink `Greeks` | option | `delta`, `gamma` | per share | derivatives by underlying price | — | [dxFeed Greeks](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/option/Greeks.html) | documented |
| tastytrade · DXLink `Greeks` | option | `theta` | $ per share per day | by number of days to expiration | — | dxFeed | documented |
| tastytrade · DXLink `Greeks` | option | `vega`, `rho` | $ per share per percentage point | by percentage volatility / interest rate | — | dxFeed | documented |
| tastytrade · DXLink `Greeks` | option | `volatility` | decimal fraction | Black-Scholes implied volatility | — | live 10-07 values 0.10–0.14 | observed |
| tastytrade · DXLink `Greeks` | option | `time` | epoch ms | event time | UTC epoch | dxFeed | documented |
| tastytrade · DXLink `Greeks` | option | `index`, `eventFlags` | indexed-event markers | snapshot/transaction protocol | — | dxFeed IndexedEvent | documented; separate analysis reducer in `TASTYTRADE_GREEKS.md`; raw observations remain separate |
| tastytrade · DXLink `Quote` | option | `bidSize`, `askSize` | not established | quoted size; NaN overnight | — | live 10-07 | unresolved |
| Alpaca · options snapshot, free plan | option | all | — | `indicative` feed: "trades are delayed and quotes are modified" | RFC-3339 | [Alpaca](https://docs.alpaca.markets/us/reference/optionsnapshots) | **not an OPRA reference**; stock SIP volume acceptance unaffected |

## Position exposure

`exposure = raw per-share value × verified multiplier × signed contracts`, once.
Delta → share equivalents; gamma → share equivalents per $1; theta → $ per day;
vega → $ per IV point; rho → $ per rate point. The multiplier comes only from contract
metadata (Tradier `contract_size`, standard root). The diagnostic accepts one positive
integer source when the other is absent; if both exist they must agree. Both missing,
an invalid supplied value, a nonstandard root or a conflict gives no exposure. Each
report discloses the supplied `quote.contract_size` and `chain_or_selection.contract_size`.
A single-source illustration is not two-source contract confirmation. Option
sizes and IV are never multiplied. Tradier gamma/theta/vega exposures are labelled
`PROVISIONAL`; Tradier rho/phi are raw only. Example: one long call with delta 0.494
and multiplier 100 is about +49.4 share equivalents.

## Commands

```bash
# Tradier REST diagnostic (token from TRADIER_ACCESS_TOKEN, else a hidden prompt)
python -m desk.tradier_option_check --environment production --symbols SPY QQQ NVDA \
    --option-underlying SPY --rounds 4 --interval-seconds 60 --output NEW_DIR/tradier.json
# Offline normalization of a tastytrade `quote_check --measure` capture
python -m desk.option_conventions tastytrade-greeks --capture CAPTURE.json --output NEW_DIR/greeks.json
```

Both refuse to overwrite an existing output file. Verdicts are diagnostic only.

For a cross-provider comparison, use `tradier_option_check --select-only` once, then
pass its report through `--option-selection` to both diagnostics. See
`tools/g5_tradier_comparison.sh`. Both check the explicit expiry/right/strike and current
provider metadata; neither substitutes a neighboring contract. `MATCHED_REPORTED_FIELDS`
compares the reported size fields only: Tradier `contract_size` and tastytrade
`shares-per-contract` are not an attestation of premium units or full adjusted
deliverables. Unknown fields give `PARTIAL`; conflicting terms give `FAIL`.

Freshness is measured independently for bid, ask and trade at report completion under
the existing 60-second quote policy. Future source times at receipt or completion fail.
Timestamp advancement is separate; a fresh trade does not freshen an old bid/ask.
Greek age is computed from its own time and has no new threshold. These checks do not
approve a trade or attest entitlement/coverage. Malformed dates produce field failures.
