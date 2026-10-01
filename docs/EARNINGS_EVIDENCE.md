# Earnings and catalyst evidence

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
- Each quarter, catalyst and calendar claim has a source reference, publication
  timestamp and receipt timestamp. Date-only publication cannot establish intraday
  availability; don't assign midnight or assume that a report date means actuals.
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

## Webull source verification still required

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

Plan B: keep the technical candidate visible with PENDING_EVIDENCE. Supported
issuer releases/filings can supply a reviewed bundle; unknown provider fields must
remain unknown. Do not substitute estimates, future news, or unreviewed raw data.
