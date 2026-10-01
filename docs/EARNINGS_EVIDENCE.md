# Earnings and catalyst evidence

**Step 09 is open.** The tested reviewed-input adapter below is not a completed
automatic data service. See [remaining work](checkpoints/09b-automatic-sources.md).

Step 09 separates a technical setup from its required fundamental qualification.
A chart signal is still visible when evidence is missing. It cannot pass fresh
review for an earnings-dependent setup until the evidence qualifies.

| Setup | Existing requirement |
| --- | --- |
| Episodic pivot | EPS **or** sales growth at least 25%; 50%+ is a comparison tag, with the same sizing rules; supported catalyst for the entry session |
| Cup-with-handle | Latest reported quarterly EPS growth at least 25%; sales cannot substitute |
| Other current cards | No new earnings-growth requirement |

An upcoming earnings date is separate display evidence. ESTIMATED is never
promoted to CONFIRMED without an explicit source claim; conflicting dates/times
remain CONFLICT. Missing future dates remain UNKNOWN. This checkpoint does not
invent an earnings blackout window or automatically prohibit other setups.

## Normalized input contract

`desk.earnings.EarningsEvidence` is the typed boundary. A supported producer or
reviewer must establish the source facts; attaching a reference is not a provider
verification performed by this module. It requires:

- Canonical ticker, security ID matching the signal's reviewed price basis,
  review reference, timezone-aware review timestamp and explicit valid-until time.
- The latest reported fiscal year/quarter, a current actual quarter and matching
  prior-year fiscal quarter. Period kind must be `quarter`, not annual/YTD/TTM.
  Fiscal periods come from the report; do not derive them from calendar months.
  Different quarter lengths need an explicit comparability reference.
- EPS and sales are optional independently. Each present metric has a finite
  decimal value, positive scale, currency, unit, accounting basis and definition.
  EPS is basic or diluted with an explicit comparable share/split basis. Sales is
  company total/net revenue, not a segment. Adjusted metrics need a documented
  adjustment basis. Compare each metric only against like-for-like definitions.
- Each quarter, catalyst and calendar claim has a source reference, exactly one
  publication timestamp (`published_at`) or date (`published_on`), and an aware
  receipt timestamp. Date-only publication retains its precision. Availability
  is conservatively bounded by the earlier of actual receipt or that date's end
  in UTC-12 (latest ordinary civil day end). This bound is not an exact publication
  time. Same-day news received after a trigger cannot establish earlier availability.
  Don't assign midnight or assume that an expected report date means actuals.
- Catalyst evidence has an explicit reviewed connection to the symbol and entry
  session. Earnings catalysts also identify the matching report period. This is
  not sentiment scoring or automatic proof that a headline caused a price move.

Growth is `(current × scale − prior × scale) / (prior × scale)` using Decimal.
A nonpositive prior value yields unknown percentage, not a turnaround percentage.
Missing/noncomparable EPS does not suppress valid qualifying sales for EP. A
malformed bundle rejects; the producer must explicitly represent absent metrics
as null rather than silently fixing malformed numeric fields.

The review states which result is latest and how long its evidence remains valid;
these are explicit source/reviewer claims, not inferred completeness. Missing,
stale, conflicting or future claims cannot qualify a required gate. The desk has
not imposed an arbitrary permanent age limit on quarterly fundamentals.

## Point-in-time evaluation and persistence

Each evaluation records its current timestamp and original trigger publication
cutoff. Report/catalyst publication after that trigger cannot qualify it later.
A pre-trigger publication received later may support a **new current review**;
it cannot retroactively turn the earlier decision into a qualified one. All receipt
and review times must be at or before that current evaluation.

Results use `PENDING_EVIDENCE`, `REJECTED`, `QUALIFIED` or `NOT_REQUIRED`, with plain
reasons, metric results, next-earnings status and a content fingerprint. Revisions
change the fingerprint and are reevaluated. The caller must replace obsolete
source claims in its reviewed snapshot; conflicting current claims are not silently
resolved by preferring the most favorable value.

Close/new-candidate scan records have a separate `qualification` field. Durable
trigger output adds `fundamentals` and `qualified_for_analysis`. The signal store's
own `eligible` flag still describes **technical lifecycle only**. Consumers must
use `revalidate_signal` before any later approval workflow; it reloads fundamental
evidence and denies required gates that no longer qualify. Each such evaluation
is appended to `earnings-reviews.jsonl`, preserving the evidence used. No candidate
or technical event ID changes simply because earnings evidence changes.

## Configured local adapter

Optional `DESK_EARNINGS_EVIDENCE` points to a reviewed JSON file:

```json
{"schema_version": 1, "securities": []}
```

Each member is an `EarningsEvidence` object. An empty list provides no coverage.
The file is reloaded for every evaluation; duplicate/missing identities and invalid
files fail closed for required gates. No example or production file is configured
by this checkpoint. The full schema can be inspected without provider access:

```bash
python - <<'PY'
import json
from desk.earnings import EarningsEvidence
print(json.dumps(EarningsEvidence.model_json_schema(), indent=2))
PY
```

This is a working reviewed-input adapter, not an automatic news or filing service.
It does not imply that the user must manually enter every future ticker forever.
Automatic adapters need validated source mappings for the same contract.

## Webull observations and supported fallback

Run on the configured credentialed host:

```bash
python -m desk.earnings_probe --symbol NVDA
```

It requests the existing earnings calendar and five quarterly income observations,
once each, stopping on fetch errors. It prints the host and receipt times, redacts
configured Webull credentials and labels results OBSERVATIONS_ONLY. It does not
start the scanner, place orders, publish normalized evidence or certify provider
accounting/time semantics. Source fields must be checked before implementing an
automatic mapping; the calendar alone cannot supply reported quarterly growth.

The 2026-10-01 iMac probe returned two NVDA actual-labelled calendar rows and an
empty quarterly-income payload. Its cause is unknown. A bounded issuer review
supplies the missing GAAP prior-year comparison; see
[review and offline command](evidence/step09-nvda-issuer-review.md) and checkpoint
[09a](checkpoints/09a-provider-fallback.md). The calendar's latest values match
that review, but this is not a universal Webull mapping or a market-wide feed.

`desk.earnings_check` reads an explicit local file and independently supplied
security ID. EVALUATED means the reviewed input was evaluated, not that every gate
passes. It prints the cup/EP component statuses separately. It uses no provider
requests; `--at` explicitly labels historical replay. Without it, expired review
files are unavailable at the current time. No scanner environment is modified.

Plan B: keep the technical candidate visible with PENDING_EVIDENCE. Supported
issuer releases/filings can supply a reviewed bundle; unknown provider fields must
remain unknown. Do not substitute estimates, future news, or unreviewed raw data.

## Step 09 source access collection

Run once on the iMac with its existing Webull environment and activated venv:

```bash
python -m desk.earnings_sources \
  --symbols NVDA,AAPL,MSFT \
  --output-dir "$HOME/Desktop/step9-sources"
```

SEC requires a declared User-Agent. If SEC_USER_AGENT is absent, the command asks
privately for an application name and your real contact email (for example,
`TradingDesk your-address`). This is not a new API key, purchase, or account. It is
sent to SEC in the request header and not stored in the report. In noninteractive
use, supply SEC_USER_AGENT in the environment; absence is a configuration failure.

The three-stock default makes at most nine Webull requests (financial alert,
calendar and quarterly income) and seven SEC requests (ticker index once, then
submissions/companyfacts per resolved company). Each provider stops at its first
request/schema failure; an unknown ticker is reported separately. The other
provider can still run. No retries, no Alpha Vantage calls, no market-open need.
Use `--sources webull` or `--sources sec` for a deliberately isolated check after
reviewing an earlier result; do not repeatedly run around a provider restriction.

Each run creates a new local subfolder containing observations and `report.json`.
Successful observations retain request route, receipt time and a SHA-256 checksum.
Webull credentials are redacted before writing provider text to disk. Reports
contain samples; complete observations remain in the local files for later mapping.
Re-running does not overwrite earlier receipt times. Files are evidence archives,
not an automatic stale-data cache used by the scanner.

- OBSERVATIONS_ONLY: a response was collected; no semantic/coverage approval.
- EMPTY_UNVERIFIED: the endpoint returned an empty container; not proof of no events.
- UNAVAILABLE: access, transport or schema failed; later calls to that provider stop.
- NOT_RUN: skipped after that failure.
- Overall INCOMPLETE (exit 1) includes any empty/unavailable/skipped response. It
  still saves the other usable observations. Do not rerun merely to get exit 0.

SEC facts are displayed without selecting a quarter: quarterly and year-to-date
values can share a period end. Filing period labels, restatements, share basis and
an earlier issuer release must be resolved by the next adapter work. This command
never writes an EarningsEvidence bundle, extends a review, or activates a scanner.
