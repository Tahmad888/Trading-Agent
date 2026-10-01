# Step 06 follow-up A — Python 3.14 resource cleanup

Status: cleanup fix verified locally and on the iMac. Base: `7c11d54`.
Function: watch / operational verification. Implementer: Codex; self-review.

## Impact record before implementation

The user's Intel iMac installed the intended commit on Python 3.14.7. Dependency
checking passed, but strict pytest returned 325 passed / 1 failed. Two SQLite
connections were finalized without being closed; synthetic HTTP 403/429 errors
also emitted resource warnings at shutdown. These are cleanup defects, not evidence
of a live Webull rejection or a failed trading setup assertion.

Inspection identifies two direct SQLite connections in `test_risk_warnings.py`.
The production RiskStateStore already closes its connections in `finally`.
SQLite's transaction context manager does not close the connection. Python 3.13+
reports this leak. The Webull adapter also needs to close HTTPError response bodies
before translating those exceptions to WebullError; test errors should be created
inside each test rather than held open from collection through interpreter exit.

Scope: the two test connection lifetimes, Webull HTTP failure cleanup and focused
regression coverage. Preserve transaction commits, error classification/chaining,
rate-limit timestamps, trading rules and strict warning treatment. No schema change.

Acceptance: reproduce with Python 3.14 if available; prove HTTP failure bodies are
closed while failures still reject data; run the full suite with `-W error` on
Python 3.12 and 3.14. The user's 3.14.7 Intel run remains a separate confirmation.

Sources:
- https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager
- https://docs.python.org/3/library/urllib.error.html
- User-supplied iMac installation/pytest transcript, commit `7c11d54`.

Rollback: revert this cleanup-only commit; no database migration or data rewrite.
Plan B: retain failed acceptance status and repair the resource owner; do not
suppress warnings or downgrade Python merely to hide the failure.

## Step 06 follow-up scope still open

Taz requires all six Webull items before Step 07: (1) actual adapter/key verification,
(2) explicit sessions and RTH completion behavior, (3) evidenced corporate-action
price scales, (4) daily versus intraday volume definitions, (5) actual 1,000-bar
retrieval, (6) historical early-close behavior. Claude is collecting additional
read-only sandbox evidence. This cleanup patch does not complete those items.

Re-entry choice is resolved: newly qualified setups may produce new tickets after
failure/closure, each requiring fresh approval. Step 07 remains paused by Taz's
explicit instruction to finish Step 06 verification first.

## Verification and delivery

- Reproduced the same baseline failure on macOS ARM64 / Python 3.14.6: 325 passed,
  one scanner-test failure from two finalized SQLite connections, plus HTTP error
  cleanup warnings at shutdown. This matches the supplied Intel / 3.14.7 transcript.
- Closed both test-owned SQLite connections while preserving transaction commits.
  Production `risk_state.py` did not require a change.
- Webull now closes HTTP error bodies when translating failures. Exception chaining
  and HTTP codes remain available; raw error bodies are not added to user messages.
- HTTP regression cases create errors at test time and assert response closure,
  failure propagation and body exclusion. Added 401/500 coverage alongside 403/429.
- Python 3.14.6 full `pytest -q -W error`: **328 passed in 1.74s**, clean shutdown.
- Existing Python 3.12 full `pytest -q -W error`: **328 passed in 1.64s**.
- Python 3.14 dependency check passed; `git diff --check` passed. No warnings filters
  relaxed. No live Webull requests, credentials, orders or scheduled runner involved.
- Pushed as `45c45cb`; Taz confirmed **328 passed in 3.93s** on the Intel iMac's
  Python 3.14.7. This is an installation acceptance fix, not completion of live
  Webull checks. Subsequent evidence is tracked in `06b-sessions-and-history.md`.
