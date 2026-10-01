# Step 09d — Microsoft Q4 issuer fallback

In progress, base 3736c986651abd547831698f29dcab0485f89f60. Serves watch/analyze.
Codex implements and self-reviews under the existing eight-finding repair scope.

Before changes: NVDA and AAPL iMac archive mapping/evaluation results were supplied
by Taz. Both mapped SEC EPS/sales, qualified the cup fundamental check and remained
pending EP catalyst evidence. AAPL growth: EPS 28.6624%, sales 16.3565%; next report
estimated Oct 28–Nov 2. NVDA next report estimated Nov 17–23. No trade eligibility.
Latest iMac strict-suite count has not been supplied after the earlier 715 result.

MSFT identity 913323997 matched sandbox metadata/daily bars at
2026-10-01T20:19:27Z. Its full-archive mapper reported no supported comparable
quarter pair in the selected SEC filing, with healthy submissions/companyfacts
and empty Webull income. This does not prove every possible SEC source lacks Q4.

Requirement: supply explicit GAAP Q4 issuer results through the existing reviewed
fallback interface, preserve timing, and demonstrate exact arithmetic. Do not
modify the selector, derive EPS by subtraction or backdate this new review to the
earlier 20:20Z replay. No model-generated facts or new thresholds.

Sourced and rechecked: Microsoft FY2026 Q4 official release, July 29, 2026,
three-month columns ending June 30: diluted GAAP EPS 4.81/3.65, total revenue
90,007/76,441 million USD (2026/2025). Both are April 1–June 30 quarters.
The same comparative statement supplies both years and share presentation.
https://www.microsoft.com/en-us/investor/earnings/fy-2026-q4/press-release-webcast

Impact: one reviewed config file, evidence/checkpoint documentation and a regression
test through the existing issuer-fallback/evaluation interfaces. No runtime change,
DB migration, calendar override, catalyst invention, runner or orders. Review receipt
and review time use the observed clock after retrieving the issuer page:
2026-10-01T20:23:34Z. Validity ends 21:00Z for this bounded historical acceptance,
not production refresh policy. Plan B: pending evidence if actual archive cross-check
fails. Rollback: revert this checkpoint, keep historical records, no active config.

Acceptance: exact growth and identity; reject pre-review/stale uses; existing
normalizer accepts reviewed explicit quarters when quarterly SEC facts are missing;
EP still requires separate catalyst. Actual MSFT iMac replay remains necessary.

## Implemented and verified

Added `config/step09-review/MSFT-2026-10-01.json` and its source review record.
No runtime code changed. The existing evaluator produced EPS growth 31.7808% and
sales growth 17.7470%, qualified cup fundamentals, and left EP pending catalyst.
The issuer file alone has no calendar claim; the iMac normalizer must merge the
saved Webull estimated range. Existing archive files need not be fetched again.

- Focused earnings dates/normalizer suite: 70 passed in 0.33s.
- Python 3.12 strict full suite: 767 passed in 4.37s.
- Python 3.14 strict full suite: 767 passed in 4.30s.
- New test checks actual issuer arithmetic, identity, pre-review and expired use.
- `git diff --check`: passed.

Checkpoint implementation complete; MSFT actual-host merged replay still pending.
NVDA/AAPL actual-host checks are accepted for their reported scope only. This does
not close Step 09: automatic source refresh, catalyst retrieval and remaining
scanner-to-review acceptance from 09b are still open. No Step 10 changes.

## Subsequent actual-host result

Taz supplied the MSFT merged archive replay after this checkpoint: mapped issuer
fallback, EPS growth 31.7808%, sales growth 17.7470%, cup fundamentals qualified,
EP pending catalyst and next earnings estimated Oct 27–Nov 2. The earlier pending
MSFT replay statement is superseded for that historical scope. This does not
certify current live supply or close Step 09; follow-up 09e continues integration.
