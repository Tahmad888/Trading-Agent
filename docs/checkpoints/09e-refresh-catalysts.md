# Step 09e — refresh, catalyst review and scanner integration

Implementation verified; actual-host acceptance pending. Step 09 remains IN PROGRESS.
Base 70b301a030373e1c9f49df8fd59dfd1e60daf72b; upstream matched.
Serves watch/analyze. One implementer (Codex), self-review. Step 10 not started.
Taz requested the remaining Step 09 workflow, with batched verification to conserve
credits. MSFT's supplied iMac merged replay passed; NVDA/AAPL also passed their
scoped historical checks. No viaNexus report found in this chat/workspace/Desktop
filenames, and its tools are not callable here. Do not wait on that optional source.

## Research and impact recorded before code

SEC submissions/facts are updated as filings disseminate, with possible processing
delays. Collection is not a guarantee that every earnings release is already filed.
https://www.sec.gov/search-filings/edgar-application-programming-interfaces
Form 8-K covers many event types; Item 2.02 concerns reported results, and filing
can follow the underlying event. Acceptance time proves filing availability, not
the earlier original news release. Do not call every 8-K an earnings catalyst.
https://www.sec.gov/files/form8-k.pdf
Qullamaggie's described workflow checks news/SEC/company sources after scanning;
it supports gathering and reviewing evidence, not automatic sentiment qualification.
https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/

Implementation scope: explicit bounded refresh policy, batched existing Webull/SEC
retrieval, durable current/history snapshots, negative caching and fail-closed latest
attempts, original SEC filing document candidates, explicit content-bound catalyst
reviews, and opt-in scanner/revalidation cache consumption. No order or schedule
activation, no source purchases, no inferred catalyst relevance.

Policy inputs explicitly configure refresh age, candidate lookback/count and symbol
identity mappings. Sample values are engineering assumptions, not trader rules.
No refresh extends a manual issuer/catalyst review's validity. Old archives cannot
be relabeled fresh: expiry is capped by original receipt plus configured age.
Original document content is untrusted data. Catalyst entry session is the first
cash-equity session not already closed at publication (exchange holidays/early
closes apply); ambiguous earlier publication requires separate issuer evidence.

Producers: existing source collector/normalizer, SEC primary filings, explicit
review JSON. Consumers: persistent earnings cache, qualify, scanner and fresh
revalidation. New opt-in SQLite store only; existing standalone evidence files
remain supported, but conflicting source configuration is rejected.

Acceptance: batch refresh/cache reuse/restart, source outage and recovery, altered
data and expired reviews, exact snapshot history, unknown symbols, after-hours and
holiday session assignment, unreviewed/revised/rejected catalysts, and scanner
trigger→review→revision/outage rejection. A consolidated host check will separate
real source retrieval from synthetic scanner plumbing. SEC-only news coverage is
explicitly incomplete for non-filing catalysts; viaNexus can supplement later.

Plan B: keep candidate/evidence visible with a reason and block required qualification.
Rollback: remove new opt-in cache/refresh configuration; retain DB/archive for audit;
revert code/docs together. No destructive migrations. Actual-host final acceptance
remains a separate exit criterion, never inferred from local tests.

## Implemented

- `earnings_refresh.py`: bounded explicit policy, original collector/normalizer,
  persistent immutable snapshots, policy/review content hashes, current-cache
  reads, single refresher lock, failure/retry state, source-age/manual-review
  expiry, backward-replay rejection and opt-in scanner refresh.
- `catalysts.py` and `sec.py`: bounded original SEC primary-document retrieval,
  8-K/8-K/A candidate selection, hash-bound accepted/rejected reviews, publication
  sessions, amendment invalidation, and one-document actual-host probe. SEC-only
  coverage and partial failures are visible; filing presence never qualifies EP.
- `earnings.py` / `scanner.py`: consume current cache at qualification and review;
  outage, revision, removed approval or expired evidence cannot reuse old success.
  Empty upcoming dates remain UNKNOWN without suppressing supported actuals.
- `earnings_cache_check.py`: offline current-state/candidate/gate inspection.
- `config/step09-refresh/historical-2026-10-01.json` and
  `docs/EARNINGS_REFRESH.md`: explicit dated three-symbol acceptance, one batched
  iMac procedure, review template, opt-in wiring, limitations and rollback.
- Status docs record Taz's MSFT merged historical pass without closing Step 09.

## Verification

22 added synthetic/local cases include the real collection interface with fake
providers, cache reuse/restart, source-age expiry, failure/recovery and negative
caching, earnings revisions, policy/identity mismatch, missing SEC contact,
refresh lock, original body/review binding, review withdrawal, SEC amendments,
publication sessions (including holidays/early close), SEC path/transport bounds,
scanner trigger→fresh review→outage rejection with journal evidence, backwards
replay protection, offline inspection, actual-document probe interface, issuer
review expiry and unknown future dates. No new actual provider result is claimed.

- Python 3.12: `.venv/bin/python -m pytest -q -W error` — **789 passed in 3.88s**.
- Python 3.14: `../verify-python314/bin/python -m pytest -q -W error` —
  **789 passed in 3.89s**.
- `git diff --check`: passed. Codex self-review; no independent agent review.
- Upstream still matched base 70b301a before the checkpoint commit.

## Remaining Step 09 work and acceptance

1. Consolidated actual-iMac archive/cache check plus SEC original-document access
   using `docs/EARNINGS_REFRESH.md`. Credentials/full archive are absent here.
2. Current live refresh on that host with a current explicit policy and issuer
   review where required, followed by review of an actual relevant catalyst and
   actual scanner-to-review acceptance. Historical replay cannot prove this.
3. Full watchlist enrollment/refresh queue and coverage beyond the deliberately
   bounded 1–5-symbol collector. Unknown mappings remain pending. Primary SEC
   documents do not cover every exhibit, original release or non-filing catalyst;
   source supplementation remains necessary for those cases. viaNexus unverified.

No environment, schedule, orders or runtime activation. No Step 10 changes.
