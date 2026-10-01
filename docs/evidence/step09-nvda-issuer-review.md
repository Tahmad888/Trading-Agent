# Step 09 NVDA source review — 2026-10-01

Scope: reviewed quarterly facts for NVDA (Webull ID 913257561, verified in Step 08).
No chart qualification, current-session catalyst, automatic filing coverage or
orders are implied. Codex source review/self-review; no independent review claimed.

## Reported iMac observation

Taz supplied `desk.earnings_probe` output from api.sandbox.webull.com, checked at
2026-10-01T18:53:05.061867Z. Calendar receipt 18:53:05.253440Z; income receipt
18:53:05.475284Z. Both requests completed; quarterly income payload was `[]`.
Calendar contained the following USD actual-labelled values, alongside estimates:

| Fiscal period | Expected publication date | EPS actual | Revenue actual |
| --- | --- | --- | --- |
| FY2027 Q1 | 2026-05-20 | 2.39 | 81,615,000,000 |
| FY2027 Q2 | 2026-08-26 | 2.46 | 96,221,000,000 |

No future date or matching prior-year quarter was returned. The report does not
establish why income is empty, entitlement, publication times, revision behavior,
or universal Webull accounting semantics. Do not promote expected dates to actual
release timestamps, or assume another key/account fixes this response.

## Sourced issuer comparison

[NVIDIA Q2 FY2027 release](https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-second-quarter-fiscal-2027),
dated August 26, 2026, contains the same-period GAAP comparative table:

| Metric | Q2 FY2027 | Q2 FY2026 | Decimal calculation, rounded for display |
| --- | --- | --- | --- |
| Diluted EPS, USD/share | 2.46 | 1.08 | +127.78% YoY |
| Total revenue, USD millions | 96,221 | 46,743 | +105.85% YoY |
| Three-month period end | 2026-07-26 | 2025-07-27 | Matching fiscal quarter |

Both columns are taken from this same release and accounting/share presentation.
The release explicitly describes changed historical non-GAAP presentation; this
review uses its GAAP columns and does not mix earlier adjusted definitions.

[NVIDIA Q1 FY2027 release](https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-first-quarter-fiscal-2027)
lists Q1 ends April 26, 2026 and April 27, 2025. Q2 starts April 27, 2026 and April
28, 2025 are inferred as the next days in these consecutive fiscal quarters. The
two Q2 durations match. FY2027 refers to NVIDIA's fiscal year, not calendar 2027.

Source contents were read in Codex using the web tool on 2026-10-01, with retrieval
completed by the recorded review time 19:00:20Z. That time is conservatively used
as received_at/reviewed_at; it is not an issuer publication timestamp or an iMac
download time. Exact publication time was not established. Both comparative
columns use published_on 2026-08-26, because this is the presentation reviewed.

## Bounded normalized evidence

`config/step09-review/NVDA-2026-10-01.json` records those facts. This reviewer
identifies FY2027 Q2 as the latest reported quarter for this dated check. Validity
ends at 23:59:59 EDT on October 1: an explicit conservative review scope, not an
automatic rule that quarterly facts expire daily. No environment points at this
file automatically. Refresh supported evidence before using it for another day.

Calendar and catalyst lists are empty deliberately: an August release does not
establish today's EP catalyst, and no next confirmed release date was verified.
Expected result: cup **earnings component** QUALIFIED; EP PENDING_EVIDENCE for the
missing session catalyst; next earnings UNKNOWN. Neither result says that a
technical cup or EP trade exists. The CLI does not evaluate charts or prices.

Reproduce the bounded historical evaluation offline (no keys or market access):

```bash
python -m desk.earnings_check \
  --file config/step09-review/NVDA-2026-10-01.json \
  --symbol NVDA --security-id 913257561 \
  --at 2026-10-01T19:00:20Z
```

Omitting `--at` evaluates at the current time and rejects an expired review.
Explicit `--at` is labelled historical replay; it does not reactivate old evidence
for current scanner decisions. Local reproduction is recorded in checkpoint 09a;
the corresponding iMac run has not yet been reported.
