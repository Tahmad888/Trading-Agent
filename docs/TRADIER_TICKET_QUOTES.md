# Tradier current quotes in signal and ticket checks

Scope: G5 -> G5a parent CP3, bounded Step 2. See
[requirements and results](checkpoints/G5a-tradier-ticket-integration.md).
The integration is opt-in. It submits no orders and does not activate the runner.
Historical Webull prices, Alpaca SIP volume and Massive action evidence retain their roles.

## Quote evidence

`desk.tradier_quotes.QuoteStore` stores append-only request starts, successes,
failures, global STOP/resume events and reviewed crosswalk versions in SQLite.
One ticket recheck fetches the underlying and requested option legs in one GET.
The GET finishes before signal revalidation, account checks or final ticket locks.
A current trade is required before the scanner can change the signal. A current
stock BBO and every option leg's BBO are independently required by tickets.

Bid and ask each retain their own `*_date` time; `trade_date` never refreshes them.
The existing 60-second policy applies, including strict refusal of future, zero,
missing or stale times. A later use also rechecks receipt time. No clock allowance
or data-source substitution is added. Crossed and locked books are refused, as in
the preflight policy. Spreads use `(ask-bid)/midpoint` per leg; the worst leg is
checked against the existing spread limit, with the oldest side time for freshness.
Each option's complete OCC identity, root and reported `contract_size` must match
the trusted standard ContractBook. Adjusted or missing metadata cannot qualify.

Your chosen option quantity is in **contracts**. That quantity and verified contract
multiplier still drive premium, funding and maximum loss. Quote `bidsize`/`asksize`
are advertised quantities with unresolved encoding, retained as informational only.
They neither replace your position size nor establish available capacity. Greeks
are not used by this adapter or by its risk calculations. It makes no stop-price
valuation model or claim of immediate consolidated opening-window volume.

## Identity review

A matching ticker is insufficient. A human-reviewed record binds the current
Webull host, instrument ID, currency, exchange and common-stock/ETF classification
to Tradier's exact slash-form equity key, stock/ETF type and issuer description.
A Tradier stock quote supplies no shared immutable issuer ID here. This is labelled
`human-reviewed-reference-v1`; provenance `instrument_id` is a reviewed quote key,
not an invented CUSIP. Quote exchange fields are trading venues, not listing IDs.
Descriptions/types are compared on every capture. Contradiction persists as a
failure even if a later capture restores the old description.

`ReviewedMapping` defines the input schema. Review authentic saved captures of both
providers, then write a record with: symbol, webull (the `WebullSide` fields), tradier
(environment, symbol, type, description), reviewed_by, reviewed_at, and the two
capture SHA-256 hashes. This is a review assertion, not a cryptographic provider
attestation. No production records are supplied by this change. Record it locally:

```sh
python -m desk.tradier_quotes --store "$HOME/.config/trading-desk/tradier-quotes.sqlite" \
  --environment production review --record reviewed-crosswalk.json
```

The command displays the record and requires an interactive `MAP <symbol>` response.
Listing an example or collecting a diagnostic never creates an approved mapping.
To revoke, use the same store/environment plus `revoke --symbol NVDA` (zero GETs).
Mapping versions are fenced with quote health through final ticket commit.

## Healthy refresh, failure and recovery

Healthy GETs and separate command processes preserve the store's health generation.
Store replacement, a component failure, STOP recovery, or a mapping replacement
changes the bound evidence and requires a new ticket version. This is deliberately
provider-specific; tastytrade dxLink approvals still cannot cross connection sessions.

An older result cannot clear a failure recorded after that request started. A late
failure also invalidates a newer success until a check begun after the failure
succeeds. An unresolved newer request blocks previous captures. A process restart
cannot forget these events. A malformed/missing ticker or component fails locally;
HTTP/transport/whole-response failures persist a global STOP for that environment.
No automatic denial/rate-limit retries occur. To permit another GET after investigating:

```sh
python -m desk.tradier_quotes --store "$HOME/.config/trading-desk/tradier-quotes.sqlite" \
  --environment production resume
```

Type `RESUME` interactively. Resume revives no prior captures: a fresh GET is needed.
Its recovered data cannot resurrect approvals from the earlier health generation.

## Wiring trusted adapters

Use `desk.tradier_risk.compose(base, price_source=source, log=log, store=store,
client=client)` where `base` is the existing independent `RiskInputs`, `source` is
the scanner's vendor/volume source and `log` its real `ScanLog`. The composer supplies
`TradierRiskSource`, a refresh hook and a local executable-BBO hook. It preserves
account state, market/regime evidence, registry, limits, contract metadata,
open interest and halt/tradability observations. Missing prerequisites still refuse.
The standalone source also exposes `refresh_signal(event_id, now, client)`.

For the existing ticket CLI, a module factory can return `Dependencies(base,
price_source, log)`. Configure explicit `DESK_TRADIER_BASE_FACTORY=module:factory`,
`DESK_TRADIER_ENVIRONMENT=production`, `DESK_TRADIER_QUOTE_STORE=<absolute path>`
and the existing protected `TRADIER_ACCESS_TOKEN`; then use
`--adapters desk.tradier_risk:factory`. Nothing selects this factory automatically.
The ticket request must name `quote_source: "tradier-rest"`; changing that label
alone supplies no independent quote evidence. There is no canned live account or
contract factory in this change. Do not point
production tickets at the tests' synthetic adapters. The composer explicitly selects
the operational quote workload policy below. This is not an all-day quote service
or a scheduler.

## Operational request policy (2026-10-08)

Diagnostics retain the 12-request lifetime ceiling. Operational composition permits
one batched GET per explicit refresh workload, without stopping a healthy client
after its twelfth lifetime request. A used diagnostic client cannot change modes to
escape its limit; a used operational client cannot switch reservation stores.

The shared quote store reserves attempts atomically and persistently before dispatch.
Its conservative rolling-minute bound uses production 120/sandbox 60 from
[Tradier's market-data policy](https://docs.tradier.com/docs/rate-limiting). Tradier
documents per-token intervals starting at the first request; this local rolling
window is an engineering guard, not an exact mirror of the provider counter or a
trading rule. Other applications or stores using the same token remain invisible
to it. Provider response headers are not currently integrated, so the local ledger
does not attest remaining provider quota. No automatic sleeps or denial retries.

A local workload/rate refusal withdraws only the requested quote/trade components;
it does not create a global provider STOP. Healthy peers retain their evidence.
Recovery changes the affected health generation, so an old approved ticket cannot
revive. An actual HTTP/transport/global-reply failure still persists STOP and needs
explicit resume plus a fresh successful capture. Clock rollback refuses a local
reservation until its earlier evidence is no longer ahead; no tolerance is invented.

Accounting distinguishes reserved/attempted dispatch from confirmed network sends.
The shared client cannot know whether an injected request function contacted the
network; `confirmed_network_sends` is null, `network_send_status` is NOT_ATTESTED,
and log entries use `dispatch_attempted_at`, replacing the misleading `sent_at`.
Keep the old count as attempts, never cite it as an independently verified send count.

## Offline preparation and inventory

New Webull diagnostics retain `identity_capture` issuer metadata with its host.
Tradier diagnostic stock observations retain `issuer_reference` symbol/type/name
and the raw exchange field (which is not used as a listing identifier).
`desk.tradier_readiness` prepares non-installable drafts from complete,
non-contradictory captures and their SHA-256 hashes. Missing issuer fields remain
missing; equal ticker strings do not repair them. Drafts lack the human review
fields required by ReviewedMapping. Without capture arguments, the command only
inventories configuration presence. It never invokes the configured factory,
creates a quote store, or calls a provider. Configured settings cannot establish
real account/status/contract/market readiness.

Final lock order: ticket, signal, existing volume/vendor identity guards, Tradier
store, account. The final clock is sampled after all lock acquisition. The held
store supplies current trade and all BBOs again; risk is rerun against the same
chosen limits/quantity plus current market evidence. No provider request, credentials
or signal mutation occurs under these final locks. A racing writer either commits
before the fence and is observed, or waits until the ticket commits.

## iMac verification after the commit is pushed

Update the dedicated branch and verify the exact published SHA and strict suite
first. Source the protected environment as usual. During a regular session a
single read-only capture can check the new producer on the host:

```sh
out=$(mktemp -d "$HOME/Desktop/g5-tradier-store-XXXXXX")
python -m desk.tradier_quotes --store "$out/quotes.sqlite" --environment production \
  refresh --symbols SPY QQQ NVDA --output "$out/capture.json"
printf 'Report directory: %s\n' "$out"
```

This uses a scratch store and **does not install mappings or change ticket configuration**.
Option capture takes explicit `--options` followed by exact currently selected OCC
symbols. This capture proves fields/freshness only. Full consumer acceptance also
needs reviewed live crosswalks and real independent account/status/contract/market
inputs; the offline successful ticket is a labelled synthetic integration test.
G5, opening-volume verification, remaining host checks and Step 09 keep their
separate status. Revert the integration commit for code rollback; keep SQLite
history for audit. No permissive alternate-source fallback exists.
