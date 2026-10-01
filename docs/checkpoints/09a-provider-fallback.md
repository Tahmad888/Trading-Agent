# Step 09 source follow-up — Webull observation and issuer fallback

Status: implementation and local acceptance verified; iMac fallback reproduction
pending. Base 935a90d; upstream matched before follow-up. Taz supplied the iMac sandbox probe at
2026-10-01T18:53:05.061867Z. Calendar returned FY2027 Q1/Q2 actual-labelled values;
five-quarter income request returned []. OBSERVATIONS_ONLY, not qualification.
This does not establish why income data is empty or prove a paid/live key fixes it.

## Impact record before changes

Webull FY2027 Q2 EPS 2.46 and revenue 96,221,000,000 match NVIDIA's issuer release.
That release supplies GAAP diluted EPS 1.08 and revenue 46,743 million for FY2026 Q2.
The current release also explicitly revises historical non-GAAP presentation;
use its same-basis GAAP comparative table rather than mixing adjusted definitions.
https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-second-quarter-fiscal-2027
Current period ends 2026-07-26; prior ends 2025-07-27. Their starts are the days
after Q1 ends 2026-04-26 and 2025-04-27 shown in the issuer's Q1 FY2027 table:
https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-first-quarter-fiscal-2027
Only calendar publication dates were established here, not exact release times.

The initial contract required exact publication times even for old releases. That
would unnecessarily block useful dated primary evidence. Add an explicitly dated
publication alternative with a conservative upper bound, never an invented exact
time. The bound is the earlier of actual receipt and the end of that date in UTC-12
(latest end across ordinary civil time zones). For same-day news received after a
trigger, that bound cannot establish pre-trigger availability. Existing exact-time
records continue unchanged. Keep original publication precision in saved evidence.

Affected: Published contract, quarter validation, all publication-cutoff consumers,
calendar/catalyst comparisons, docs, focused tests and a read-only local evidence
acceptance command. Record a dated NVDA issuer review for manual opt-in verification;
no environment or scanner activation. The issuer review contains no today's catalyst
and no confirmed next earnings date. Expected: cup earnings component QUALIFIED,
EP PENDING for missing current-session catalyst, next earnings UNKNOWN. No actual
chart setup/entry or profitability is asserted. Source mapping is bounded to these
specific observations; no automatic market-wide issuer parser is claimed.

Acceptance: exact and date-only timing, no early same-day access, next-day conservative
bound, wrong/future timing rejection, actual fiscal-pair arithmetic, stale review and
missing catalyst. Both strict suites. Retain source failure as visible evidence.
Plan B: unsupported/unknown facts remain pending; don't retry an empty route blindly.

## Implemented and verified

- All quarter/calendar/catalyst consumers use the explicit publication bound;
  timestamp-only evidence remains supported. Original precision stays in the
  evidence snapshot. New optional fields change content fingerprints as expected;
  no technical event IDs or SQLite schemas change.
- `desk.earnings_check` evaluates an explicit local file and independently supplied
  security ID. No keys, market requests, chart inputs or scanner configuration.
  Explicit `--at` means historical replay; default means current-time evaluation.
- `config/step09-review/NVDA-2026-10-01.json` contains the dated reviewed issuer
  comparative facts, not synthetic production values or a general ticker feed.
  [Source record](../evidence/step09-nvda-issuer-review.md) preserves provenance,
  reported iMac observations, period-start inference and review-window assumption.
- Actual offline replay at 2026-10-01T19:00:20Z: EVALUATED; cup fundamental component
  QUALIFIED, EPS growth 1.277777777777777777777777778 and sales growth
  1.058511434867252850694221595 (ratios, not percentages). EP PENDING_EVIDENCE for
  absent session catalyst; next earnings UNKNOWN. No chart/entry PASS asserted.
- 18 new tests cover publication precision, same-day trigger boundaries, old-date
  availability, invalid/future records, issuer arithmetic, identity/review expiry,
  CLI replay and malformed input. Existing gate/scanner tests continue to pass.
- `.venv/bin/python -m pytest -q -W error`: **688 passed in 3.51s**, Python 3.12.14.
- `../verify-python314/bin/python -m pytest -q -W error`: **688 passed in 3.59s**,
  Python 3.14.6. Focused earnings suites: 67 passed in 0.36s.
- `git diff --check`: passed. Codex self-review, no independent reviewer.

## Remaining and stopping point

Have the iMac reproduce the documented offline check and strict suite. No market
open or further Webull/Alpha Vantage requests are needed for that check. Step 09
remains open until that scoped confirmation; do not begin Step 10 in this turn.

Webull income emptiness is unresolved. The fallback establishes one reviewed
quarter pair, not provider completeness, a current catalyst, a next earnings date,
or an automatic all-ticker data service. Those unavailable inputs remain visible
and must be supplied by supported source adapters/reviews before required signals
can qualify in operation. No purchase or paid-key fix is assumed.

Rollback this follow-up's code, review file and docs together; timestamp-only
evidence remains usable. Remove any external opt-in pointer before reverting
date-only support. Preserve historical evidence/review logs. No keys, orders,
scheduled runner or environment changes were made.
