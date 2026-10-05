# G5a parent Checkpoint 3 — repairs from the 2026-10-05 live run

Hierarchy: G5 → G5a parent Checkpoint 3 → children 1–6. These are follow-up repairs and
measurement work inside that hierarchy (child 3 code paths; child 5 evidence), not new
parent checkpoints. Step 09 stays paused; G5 is not complete.

Inputs: Taz's handoff `CLAUDE_G5_LIVE_RUN_REPAIR_PROMPT_2026-10-05.md` and Astra's
independent audit of the live evidence (`G5_LIVE_TEST_INDEPENDENT_AUDIT_2026-10-05.md`,
with `G5_LIVE_CHAIN_REPRO_2026-10-05.json`), external attachments kept in the scratch
area. The live evidence was collected on `0ad7a1d` (cloud, Python 3.12.3; not the iMac).

Base: `e2373098bbf0ba943c6a998834af7169e3cf349a` (parent `0ad7a1d`), Claude's
overlapping-identity ordering repair (O1). **Astra has not yet reviewed `e237309`;
child 3 is not approved.** That commit is preserved; each package below is a separate
commit on top of it. No provider calls are made in this batch; scratch fixtures only.

Corrections Astra made to Claude's live report, accepted: the 16:14 close slot skipped
85 stale-history names and 5 invalid-row names (not 90 stale); the 16:37 scratch scan
covered 9 watchlist names (not the scheduled 90-name universe) and saved its candidates
under the source date, so it did not show Tuesday's desk armed; SPY's closing values did
not all agree (Summary 774.88, last RTH Trade 774.94, Alpaca and Webull 774.83); the
Greeks ages used the newest Trade time as the clock (one came out negative after the
close); "BRK.B never scans" overstated it (scoped discovery is a separate path).

Labels: *Checked* = observed in this repository or the saved evidence; *Sourced* =
provider documentation; *Assumption* = unverified; engineering choices are named as such
and are not retail-trading conventions.

## Package 1 — share-class option-chain lookup (written before code)

Trader-day step: analyze / plan.

- **Defect (Checked).** `ReadClient.resolve` sends a canonical Equity share-class symbol
  (`BRK.B`) to tastytrade as `BRK/B`, URL-encoded. `chain_options` canonicalizes the
  requested underlying (so `BRK/B` also becomes `BRK.B`), sends `/option-chains/BRK.B/
  nested` and compares the returned `underlying-symbol` literally with `BRK.B`. Astra's
  fixture reproduction: SPY available; `BRK.B` and `BRK/B` both request the dotted path
  and refuse a valid `BRK/B` Standard chain (`OPTION_CHAIN_UNAVAILABLE`).
- **Requirement.** One shared Equity provider-boundary conversion (canonical `BRK.B` →
  tastytrade `BRK/B`, encoded as one path component `BRK%2FB`) used by the instrument
  lookup and the chain lookup (Sourced: tastytrade symbology documentation — Equity
  share classes use a slash; options have their own OCC and streamer symbols). Returned
  chain underlyings are compared through the canonical Equity form; classes stay
  distinct (`BRK/A` never satisfies `BRK.B`). Option identifiers (OCC symbols with their
  spaces, streamer symbols) are returned exactly as received; no Equity slash
  substitution touches them. Standard-only chains, expiry/strike parsing (Decimal),
  contract validation and request-stop behaviour (401/403/429/budget) are unchanged; no
  retry is added.
- **Ambiguity (engineering choice).** A reply whose matching Standard chains give the
  same (expiry, strike) two different call or put symbols refuses with
  `OPTION_CHAIN_AMBIGUOUS` rather than silently choosing one. Identical duplicates are
  merged, as before.
- **Producers/consumers.** `ReadClient.resolve`, `ReadClient.chain_options` (and its
  caller `chain_pair`, `quote_check.diagnostic`). New helper
  `tastytrade_quotes.equity_provider_symbol`.
- **Acceptance (offline).** SPY control; `BRK.B` and `BRK/B` request
  `/option-chains/BRK%2FB/nested` and accept a valid matching `BRK/B` Standard chain;
  `BRK/A`, another underlying, a malformed underlying and an ambiguous reply cannot
  satisfy `BRK.B`; non-Standard chains excluded; expiry and Decimal strikes intact; OCC
  symbols returned verbatim; denial, rate limit and budget stop as before with no extra
  request. The live `BRK/B` lookup is a later bounded provider check, not done here.
- **Rollback.** Revert the package commit; nothing is stored.

### Package 1 record

Changed: `src/desk/tastytrade_quotes.py` (`equity_provider_symbol`),
`src/desk/tastytrade_transport.py` (`resolve` uses it; `chain_options` requests the
provider symbol, compares canonical underlyings, refuses conflicting contracts),
`tests/test_share_class_chain.py` (new, 22), `docs/TASTYTRADE_QUOTES.md`.

Before (base `e237309`, the new tests copied onto it): 10 failed (every share-class
request, the encoded-path stops, the shared resolve conversion), 12 controls passed.
After: 22 passed. The eight quote test files: 240 passed on Python 3.12.3 and 3.13.14.

## Package 2 — recoverable unfinished close preparation (written before code)

Trader-day step: watch. **Gap (Checked):** on 2026-10-05 the close+10-minute job ran at
16:14:55 before Webull published the day's completed daily bars (none at 16:16 or
16:26, present at 16:36). `run` retries a failed slot only inside its 15-minute window;
after it nothing recovers the preparation. Also (Checked in code): a failed close scan
still called `save_armed(next session, rec)` with no candidates and no market, so a
failed attempt could overwrite earlier preparation. One day's timings are not a
provider publication SLA, and the nominal close+10 slot is kept.

### Job contract

- **Identity.** One job per completed **source session**; its **target session** =
  `next_trading_day(source)`. Both are persisted at creation and never recomputed from a
  later clock. Stored in the existing signal store (`signals.sqlite`, table
  `close_jobs`) so job progress and the target's armed list commit in one transaction.
- **Frozen universe.** Created by the close slot from watchlist + movers + bearish
  candidates with their tags; discovery errors are recorded and make coverage
  `PARTIAL` — discovery is not re-run later (it describes the close). Recovery applies
  the *current* user removals (a removed name is recorded `REMOVED`, never prepared).
- **Per-symbol outcomes** from the attempt's reasons, by structured code, not the broad
  prefix: `PREPARED` (evaluated; may have no candidate — a genuine criterion result),
  `WAITING_LATEST_SESSION` (new code `LATEST_SESSION_NOT_YET_AVAILABLE`, emitted only
  when the history is otherwise contiguous and ends exactly one trading session before
  the required one), `TRANSIENT_UNAVAILABLE` (provider returned nothing without a stop
  code, missing minute anchor, identity store busy), `INVALID_HISTORY` (invalid rows,
  older gaps, out-of-bounds history), `IDENTITY_REFUSED`, `PROVIDER_STOP`
  (HTTP 401/403 denial or 429 rate limit), `REFUSED` (anything else), `REMOVED`. Only
  `WAITING_LATEST_SESSION` and `TRANSIENT_UNAVAILABLE` are retried. The market reference
  (SPY+QQQ) is its own dependency; nothing publishes until it is evaluated.
- **Publication.** A successful attempt appends its newly prepared names' candidates to
  the target's armed list (dedupe by candidate ID, existing behaviour) and sets the
  market once; it never replaces committed candidates or a committed market with empty
  or null. A failed market-reference attempt publishes nothing. Healthy peers publish
  while late names stay pending (`PARTIAL`). Volume-pending names are stored in the job
  row in the same transaction and read by the existing next-session volume path
  (unchanged completion rule: later Webull prices never prove the Alpaca daily final).
- **Exactly once.** An attempt first *claims* the job (token + 10-minute lease,
  engineering choice) in a short transaction; requests run with no lock held; the commit
  requires the same token. A duplicate or second process sees the claim and reports
  pending without requests; a crash leaves the claim to expire; an older delayed attempt
  whose lease was superseded cannot commit. No network call inside a write lock.
- **No backdating.** Candidates published once the target session has opened get an
  observation floor at the commit time, so only bars ending after it can produce a
  trigger (existing `start_after` mechanism); earlier crossings are never replayed.
- **Status:** `PENDING` (nothing published), `PARTIAL` (published, recoverable names
  remain), `FINISHED` (nothing recoverable remains; coverage `FULL` only if every
  universe name prepared and discovery was complete, else `PARTIAL`), `BLOCKED`
  (provider stop), `FAILED` (the market reference itself was refused, e.g. invalid SPY
  history: nothing can be prepared, so nothing is retried), `EXPIRED`. Only `FINISHED`
  with `FULL` coverage is "complete".

### Operational choices (engineering, not trading rules)

- Cadence: no new scheduler. Recovery runs inside the existing runner invocation (the
  documented 5-minute launchd/cron call of `python -m desk.scanner`). At most one attempt
  per job per invocation; the next attempt is recorded as last attempt +
  `DESK_CLOSE_RECOVERY_SPACING_MINUTES` (default 5, matching that cadence); an
  invocation before it reports pending without requests.
- Envelope per attempt: one close-scan pass over only the unresolved names plus
  SPY/QQQ, through the existing batches (20 symbols) and the existing Alpaca budget;
  the attempt records symbols requested and Alpaca requests used.
- Deadline: the target session's close (the existing candidate expiry) and the frozen
  source session must still be the latest completed session; afterwards `EXPIRED`. No
  trade cutoff is added; an earlier operational cutoff would need Taz's decision.
- Provider stop: `BLOCKED` with the reason, persisted; no further attempts. Resume
  condition: an operator runs `python -m desk.close_jobs resume` after the provider issue
  is resolved; no quota-reset time is assumed. Resume clears the block and makes the job
  eligible at the next runner invocation (it makes no request itself). `python -m desk.close_jobs status` prints
  source/target, status, coverage, attempts, next attempt, usage, per-name outcomes.
- Deployment prerequisite: the runner cadence and budgets must actually be configured
  on the host; this package installs no schedule.

### Acceptance (offline)

Bars unavailable at 16:14 and 16:26, usable at 16:36 → full original universe prepared
for the correct target; latest-missing vs older-missing; SPY/QQQ late then usable while
one ticker stays late; invalid OHLC / identity / unsupported isolated; a successful
publication survives a later failed attempt; evaluated-empty vs pending; duplicate
calls, reopened storage, crash after claim, two writers, reversed completion;
Friday→Monday, holiday, early close, DST, after-midnight recovery, expired target,
frozen source; volume pending survives price recovery; user removal; rate limit /
denial blocked without retries; bounded symbols per attempt; intraday, EP and Friday
leader jobs unaffected; no trigger from bars before a mid-session publication.

Rollback: revert the commit. The `close_jobs`/`armed_floors` tables are additive and
ignored by older code; armed lists stay in their existing format.

### Package 2 record

Changed:
- `src/desk/close_jobs.py` (new): job creation from the close slot (frozen universe),
  `attempt`/`recover`, structured classification, spacing, the `status`/`resume` CLI
  (`status` never creates a store).
- `src/desk/signal_state.py`: additive tables `close_jobs` and `armed_floors`; claim,
  token-checked commit (job row + target armed list + floors in one transaction),
  release, end, resume, `close_job_volume_pending`; `observe` honours a candidate floor.
- `src/desk/scanner.py`: `run` makes at most one recovery attempt per invocation when the
  close slot itself is not due (exceptions recorded, never blocking the slot jobs); the
  close slot opens the job and makes the first attempt instead of `save_armed` with a
  possibly empty result; the intraday volume path also reads job volume-pending names;
  `funnel` adds `close_recovery_attempts` and counts recovered setups in `setups_armed`
  (a failed close slot stays in `scans_failed`); `VOLUME_USAGE` names the scan-reported
  Alpaca usage key, which the job copies (the job never reaches the Alpaca producer;
  the existing architecture test holds).
- `src/desk/bar_contract.py`, `src/desk/vendor_basis.py`: `LATEST_SESSION_NOT_YET_AVAILABLE`
  appended only for a contiguous history ending exactly one session early; Webull HTTP
  401/403/429 recorded per symbol as `PROVIDER_STOP_HTTP_<status>`.
- `tests/test_close_jobs.py` (new, 23).

Before (base `0e38810`, scratch replay of the 10-05 shape with the existing scanner
fixtures): 16:14 `close` failed ("no SPY or QQQ bars: nothing armed; Stale daily
data…") and wrote Tuesday's armed list empty with no market; 16:26 and 16:36 returned
nothing (slot window over); Tuesday stayed `(None, [])`. The new test module cannot run
on the base (its module does not exist). After: 16:14 `close` pending (job `PENDING`,
nothing written for Tuesday), 16:26 `close_recovery` pending, 16:36 `close_recovery`
publishes the whole frozen universe for Tuesday: `FINISHED`/`FULL`, 3 attempts, market
`full`, LEAD breakout armed, nothing saved under Monday, 16:45 does nothing.

Focused mutations (each reverted): commit ignoring the claim token, ignoring the
observation floor, readiness code on any stale history (both `bar_contract` and the
vendor path, the latter after adding an older-gap vendor case), unspaced next attempt —
all killed. Publishing the market of a failed attempt survived as an equivalent mutant:
`close_scan` sets the market only after both references evaluate, so a failed attempt's
market is already null; the commit's null-market guard stays.

Strict suite (`-W error`) on the committed package, Python 3.12.3: 1711 passed (291.8 s).
Python 3.13.14: 1710 passed (303.7 s) one step earlier, before the read-only `status`
guard (+1 test) and the recovery error record's Eastern clock; both versions run again
on the final batch.

Limitations: discovery is not re-run after the close slot, so a mover-list outage at
16:14 stays `PARTIAL` coverage; a user addition made after the close slot is not added to
the frozen universe (it is picked up at the next close, as before); one runner invocation
per five minutes is a host prerequisite, not something this package installs.
