# Step 09c — repair eight source interpretation findings

IN PROGRESS. Base a20ef25073bac177a6947a35d6366be51929018a, upstream matched.
Serves watch/analyze. Taz authorized implementing the six findings and two traps
after research. Codex implements and self-reviews. Step 10 remains paused.

## Evidence and requirements recorded before implementation

Retail practice evidence supports checking earnings releases, comparable reported
quarters and catalyst news; it does not establish that every retailer uses the same
feed or these engineering checks. No new trading threshold is introduced.

| Finding | Evidence | Repair and acceptance |
| --- | --- | --- |
| 1 Empty Webull income | Checked: all three iMac responses were empty; SEC facts succeeded. [SEC API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) documents companyfacts. [Qullamaggie](https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/) uses SEC/company news in earnings research. | Use archived SEC facts plus explicit issuer fallback. Empty income cannot erase usable independent evidence; no repeated endpoint guessing. |
| 2 Estimated date ranges | Checked: financial-alert returns start/end dates for NVDA, AAPL, MSFT. [Fidelity events help](https://www.fidelity.com/quick-content/etf/help/research/learn_er_conference_calls.shtml) presents expected dates. | Preserve estimated ranges, exact dates, conflicts and observation time; no fabricated midpoint or confirmation. |
| 3 Quarter versus YTD | Checked: NVDA 2.46 quarterly and 4.85 six-month EPS coexist. SEC documents differing fact periods/units. | Select actual start/end contexts for matching quarters, never latest row alone. Annual/YTD cannot qualify as quarter. |
| 4 Filing fiscal labels | Checked: NVDA prior-year comparative carries current filing FY2027/Q2. SEC warns calendar frames differ from fiscal calendars. | Latest reported calendar quarter and filing report end anchor current context; comparative date bounds anchor prior, not comparative fy/fp/frame. |
| 5 Revenue concept drift | Checked: NVDA current Revenues versus stale 2022 contract-revenue; AAPL the reverse. | Match period/accession first, supported concepts second. Conflicting overlapping concepts remain unresolved; no static priority that picks old facts. |
| 6 Noisy summaries | Checked: broad EPS substring includes unrelated concepts; first twelve filings can hide financial reports behind insider forms. | Exact EPS/revenue concepts and separate financial filing sample; retain full original observations. |
| 7 Upcoming baseline trap | Checked: alert eps_ly/rev_ly accompanies upcoming fiscal period, not latest reported calendar period. [Fidelity glossary](https://www.fidelity.com/webcontent/ap010098-etf-content/16.06/help/research/learn_er_glossary_1.shtml) distinguishes actual growth and estimates. | Alert fields supply calendar only, never reported growth. Tests poison estimates/baselines and assert unchanged actual calculations. |
| 8 Missing Q4 trap | Checked: MSFT report sample shows annual/9-month EPS; full file must be inspected before declaring absent. [Microsoft Q4 release](https://www.microsoft.com/en-us/investor/earnings/fy-2026-q4/press-release-webcast) explicitly gives quarterly EPS and distinct quarterly/annual weighted share counts. | Search full facts for explicit quarter. Otherwise accept reviewed issuer quarterly evidence, never subtract annual EPS minus nine-month EPS. Pending remains visible if no supported fallback. |

## Impact and boundaries

Producers: archived Webull calendar/alert, SEC ticker index/submissions/companyfacts,
explicit reviewed issuer evidence. Consumers: existing EarningsEvidence evaluator,
opt-in ReviewedEarningsSource, source diagnostics and later refresh integration.
New normalizer operates on complete hash-verified observation files, not report
samples. Explicit identity and bounded review window are required for export to
the existing evidence format. No automatic activation or silent review renewal.
Use matching facts from one filing to preserve comparative presentation; differing
durations or ambiguous contexts need explicit issuer evidence instead of guessing.
Quarter-duration bounds are conservative parser support, not trading rules.
Publication uses SEC acceptance, never the earlier estimated release date.

Acceptance includes successful pairs; YTD/annual exclusion; duplicate/conflicting
facts; filing revisions and future cutoffs; identity/hash failures; stale reviews;
date intervals/conflicts; issuer fallback without forecast substitution; unchanged
thresholds; combined scanner/revalidation suite. Actual iMac archive replay remains
separate from synthetic tests. No source entitlements are invented.

Plan B: report the specific unresolved component and retain source provenance.
Rollback: revert this checkpoint's code/docs together; no database migration.
Previously exported exact-date evidence remains accepted. Range evidence requires
the updated reader. Step 09 remains open for catalyst gathering, refresh/integration
and actual-host end-to-end acceptance described in 09b.

## Implemented checkpoint

- `earnings_normalize.py`: deterministic full-archive mapper, original byte hashes,
  SEC/stock identity, bounded explicit review export, filing/quarter/revision
  selection, comparable concept mapping, reported-calendar cross-check, issuer
  fallback with no conflict override and no annual/YTD EPS subtraction.
- `earnings.py`: estimated date intervals, compatible exact-date claims, conflicts,
  explicit first-observation availability bound and calendar-source health in the
  scanner evidence. Old exact-date files still parse.
- `earnings_sources.py`: precise concept allowlist and financial-filing summary;
  complete original data remains retained. No API schema is guessed from a summary.
- `tests/test_earnings_normalize.py`: 51 regression cases covering the eight findings,
  additional integrity/revision/outage cases, optional export and scanner `qualify`.
- `docs/EARNINGS_NORMALIZATION.md`: zero-call actual-archive replay instructions,
  supported scope, issuer fallback and failure interpretation.
- `docs/evidence/step09-connectors-research.md`: actual connected-service inventory,
  researched source comparison and prepared (not sent) read-only viaNexus prompt.

Research/implementation distinction: the iMac source report demonstrates successful
SEC access and empty Webull income, not successful execution of this new mapper.
Local cases use synthetic archives modeled on the observations. Microsoft official
Q4 values were researched, but its full iMac archive has not been imported here;
no claim is made that Q4 contexts are absent from that full file.

## Verification and status

Implementation/regression checkpoint complete; **actual-host acceptance pending**.
Step 09 remains IN PROGRESS. No Step 10 work or runtime activation.

- Python 3.12: `.venv/bin/python -m pytest -q -W error` — **766 passed in 4.16s**.
- Python 3.14: `../verify-python314/bin/python -m pytest -q -W error` — **766 passed in 4.20s**.
- A subsequent test-only improvement distinguishes a rehashed wrong-symbol archive
  from a simple altered hash. Focused suite: **51 passed in 0.27s**.
- `git diff --check`: passed. Code self-review; no independent reviewer claimed.
- The existing 715 cases still pass. New coverage includes actual/forecast separation,
  Q4 explicit contexts, issuer fallback and conflict rejection, amendments, period
  mismatches, partial metrics, intervals, source outages and scanner identity checks.

No live source checks executed by this mapper in Codex. The complete raw archive
remains on the iMac; the pasted summary cannot substitute for it. Next: execute the
zero-call replay in EARNINGS_NORMALIZATION.md there, then repeat AAPL/MSFT with
verified instrument IDs. Inspect real MSFT Q4 context/fallback before marking the
reported-results portion accepted. viaNexus entitlements and schemas require a
read-only connector result, not an assumption based on the Connected label.

The new optional evidence fields change serialized evidence fingerprints; existing
signals may require fresh evidence review. This is intentional invalidation, not an
order action. Reverting a reader requires removing newly exported range evidence
from its configuration, never mutating historical signal/audit records.
