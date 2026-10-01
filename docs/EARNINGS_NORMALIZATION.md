# Step 09 source normalization

This checkpoint fixes the eight interpretation findings in 09c. It does not close
Step 09 or establish automatic all-ticker coverage. Trading thresholds are unchanged.

`desk.earnings_normalize` reads the complete saved collector archive. It makes zero
provider requests. A compact pasted report is insufficient: it deliberately contains
samples and can omit the relevant filing or Q4 context.

## What is mapped

- Webull financial-alert → estimated start/end date interval only. No midpoint,
  confirmed flag, actual EPS or historical baseline is inferred from that alert.
- Webull calendar → latest reported fiscal label and actual-value cross-check;
  future estimates → estimated dates. Estimated publication dates are not used as
  precise publication timestamps for reported metrics.
- SEC submissions → current financial filing, accession, report end, acceptance time.
- SEC companyfacts → USD US-GAAP diluted EPS and revenue contexts for the actual
  quarter and its prior-year comparative **in the same filing**. Comparative `fy`,
  `fp` and calendar `frame` do not label the prior fiscal quarter. Only the current
  context's fiscal label is cross-checked with the reported calendar.
- Supported revenue concepts are matched by accession/period before selection.
  Conflicts stay pending; obsolete contexts cannot win by concept priority.
- Explicit reviewed issuer evidence supplies missing quarterly results, including
  Q4. It is checked against the latest reported calendar, identity, validity and
  metric comparability. Annual EPS minus YTD EPS is never used.

Conservative parser support: quarter lengths 70–110 days inclusive, prior-year ends
350–380 days apart, with equal current/prior durations required for automatic mapping.
These are engineering support bounds, not trading rules or claims about all fiscal
calendars. Unusual periods require explicit issuer comparability review. Basic EPS,
IFRS, custom taxonomy concepts, mixed currencies and cross-filing share-basis
reconciliation are not automatically substituted for supported data.

A newer amendment with missing facts stays pending. A newer reported release than
the available filing needs issuer evidence. Automatic release discovery beyond the
provider calendar and catalyst retrieval remain Step 09 work; this command cannot
certify that an old archive reflects everything published since its receipt.

`MAPPED` means a supported pair was constructed, not every metric is available or
any setup qualified. Read `metric_coverage`, `issues`, `calendar_issues`, source
health and provenance. Calendar outages survive export as UNAVAILABLE/PARTIAL;
an empty successful calendar stays UNKNOWN, not confirmed no-event coverage.
EPS/revenue errors, identity errors and a missing catalyst cannot be overridden by
calling the mapper. An exported pair still passes the existing evaluator/scanner.

## Actual iMac replay, no fresh API requests

The known archive is:
`$HOME/Desktop/step9-sources/20261001T193535Z-14ecec3d`.
Keep all files together, including `report.json` and `sec-tickers.json`.
Paths in the report can originate on another machine: files are resolved by basename
inside the run folder and verified against the original byte hashes. A changed hash
or identity aborts the mapping; a missing provider is reported explicitly.

After pulling the repair branch and activating the venv, run the strict suite, then
this bounded historical NVDA replay. 20:00Z is the explicit replay review time, later
than the collected responses; the one-hour validity is test scope, not a live policy.
Use fresh output filenames if you have already run it; nothing is overwritten.

```sh
python -m pytest -q -W error
python -m desk.earnings_normalize \
  --run-dir "$HOME/Desktop/step9-sources/20261001T193535Z-14ecec3d" \
  --symbol NVDA --security-id 913257561 --cik 0001045810 \
  --review-ref 'Step 09c historical iMac archive acceptance, not live activation' \
  --reviewed-at 2026-10-01T20:00:00Z \
  --valid-until 2026-10-01T21:00:00Z \
  --output "$HOME/Desktop/step9-nvda-mapping.json" \
  --evidence-output "$HOME/Desktop/step9-nvda-evidence.json"
python -m desk.earnings_check \
  --file "$HOME/Desktop/step9-nvda-evidence.json" \
  --symbol NVDA --security-id 913257561 --at 2026-10-01T20:00:00Z
```

If there is a legitimate missing comparable context, the mapper reports why.
For NVDA, an already checked issuer fallback can be supplied with
`--issuer-evidence config/step09-review/NVDA-2026-10-01.json` on a new output run.
Do not use this to conceal an archive hash/identity mismatch or conflicting sources.

Repeat for AAPL (CIK 0000320193) and MSFT (CIK 0000789019) after verifying their
Webull instrument IDs using metadata. Do not copy NVDA's ID or guess one. Read the
full MSFT result before declaring Q4 absent. If absent, review the explicit issuer
quarterly data using the existing EarningsEvidence schema. The Microsoft issuer
source gives Q4 FY2026 diluted GAAP EPS 4.81 versus 3.65, and revenue 90,007 million
versus 76,441 million, for the three months ending June 30 (not the annual columns).
[Microsoft source](https://www.microsoft.com/en-us/investor/earnings/fy-2026-q4/press-release-webcast).
Receipt/review times must reflect the actual issuer review; do not fabricate an
old receipt or label this source's annual/forecast values as quarterly actuals.

No environment variable is changed by these commands. In particular they do not
set DESK_EARNINGS_EVIDENCE or start the scanner. Production refresh/expiry policy,
a real catalyst case and actual-host scanner-to-review acceptance remain open.
