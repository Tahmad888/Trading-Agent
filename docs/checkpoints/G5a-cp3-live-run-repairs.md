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

## Package 3 — measurements without changing eligibility (written before code)

Trader-day step: analyze / approve (evidence for later decisions). Nothing here makes
unavailable data eligible: `QuoteService` acceptance, the quote-age policy, the
future-time refusal, the risk bridge, setups, the Alpaca volume role, the 50-day period
and the EP 0.5 threshold are unchanged. The new measurement path is diagnostic only and
is not imported by `risk`, `quote_risk`, `scanner`, `tickets` or any setup.

- **Where (engineering choice).** New module `desk.quote_measure` holding a bounded
  `Recorder`. It observes the existing core decoder (requested and server-accepted
  Quote/Trade maps, revisions, raw `bidTime`/`askTime`) and owns a separate
  measurement FEED channel (channel 7, like the Profile channel: its errors withhold
  only the measurement) for Trade volume fields and option Greeks. The core channel's
  requested fields are unchanged. Opt-in only: `quote_check --measure`, and the new
  `quote_measure` commands; without them no extra channel is opened.
- **Bid/ask.** Per decoded Quote row: environment, channel, generation, accepted-map
  revision, wire and canonical symbol, raw state of `bidTime`/`askTime` (`ABSENT` from
  the accepted map, `NULL`, `ZERO`, `NAN`, `INVALID`, `FUTURE` with its lead, or the
  epoch value), UTC receipt time, decision time (the moment the existing getter was
  asked) and the getter's verdict. Receipt time is never substituted for source time.
  A changed bid/ask with zero times stays `QUOTE_TIME_UNAVAILABLE`.
- **Schema history.** Each configuration per channel/type: requested map, accepted map
  (or `WITHDRAWN` when the decoder rejected it), `ACCEPTED`/`CHANGED`/`RESTORED`
  states and whether the side-time fields are in the accepted map — this separates a
  schema cause from a data cause.
- **Clock.** `python -m desk.quote_measure clock` runs `sntp <server>` (default
  `time.apple.com`, no clock-setting flags) on the host it is run on and records
  measurement start/finish, source, the printed offset and `+/-` value as text,
  exit status and bounded raw output. `--host-clock FILE` attaches that result to a
  quote diagnostic. Absent → `NOT_MEASURED`; failed/unparseable → `UNAVAILABLE` with
  a code. The offset is never applied, no tolerance is added, future leads stay as
  measured and refused. The sign is recorded as printed, not interpreted.
- **Greeks.** Raw observations only (no current-state reduction; no tested
  indexed-event helper exists in this repository): provider `time`, UTC receipt,
  decision time, receipt age = receipt − source, decision age = decision − source
  (negative kept), `eventFlags` decoded (TX_PENDING, REMOVE_EVENT, SNAPSHOT_BEGIN/
  END/SNIP/MODE), `index`, `sequence`, decimal values as text. `Greeks.price` is
  labelled market price; TheoPrice is not requested. No Greeks age cutoff.
- **Volume.** Per Trade update on the measurement channel: `dayId`, `dayVolume` state,
  the RTH Trade source time (labelled as such, never as the volume update time),
  local receipt (observation time, not exchange cutoff), generation. Transitions per
  symbol: first, increase, unchanged, decrease/correction, day reset, unavailable
  (NaN/missing), reconnect snapshot with the unobserved gap; fixed Trade time with
  changed volume flagged. No RTH 09:30–10:00 total is derived.
- **Opening window (engineering bound).** `python -m desk.quote_measure volume` runs
  consecutive capture segments of at most 600 s each through the unchanged
  `capture` (its 600-second safeguard stays), up to 3000 s in total, 1–5 equities,
  no options; the gap between segments is recorded as a reconnect gap.
  `python -m desk.quote_measure compare` puts that capture beside an explicit-bound
  `desk.alpaca_probe` result for the same session's RTH30 intervals: definitions,
  units/adjustment, bounds, the receipt-bracketed cumulative change (labelled not an
  RTH total) and the raw difference, `UNRESOLVED` unless equal. No tolerance.
- **Reports.** Bounded: metadata, schema history, first raw samples per type,
  streamed records up to a cap with a dropped count, complete per-symbol counters,
  and the terminal eligibility view kept separate from historical observations. The
  written text is scanned for the configured credential values and token/header
  markers and refused (`REPORT_CREDENTIAL_MATCH`) if any appear.
- **Acceptance (offline).** Zero/null/missing BBO times; reordered, withdrawn and
  restored schemas; future events; Greeks ages where the latest Trade has another
  time; transaction/removal markers; volume with fixed Trade time; day reset/NaN;
  decreasing volume; reconnect snapshot with a gap; segment bounds; sntp parsing and
  failure; credential refusal; core eligibility identical with and without the
  recorder.
- **Rollback.** Revert the commit; nothing is stored outside the report files.

### Package 3 record

Changed:
- `src/desk/quote_measure.py` (new): field-state helpers, the bounded `Recorder`
  (core-decoder observer and measurement-channel decoder), `measure_clock`/
  `load_host_clock`, the credential guard, `volume_capture` segments, `compare`, CLI
  (`clock`, `volume`, `compare`).
- `src/desk/tastytrade_quotes.py`: `QuoteService.symbol_for`; `FeedDecoder` takes an
  optional observer told about each map outcome and each decided row; an observer
  fault ends recording and never reaches the quote path.
- `src/desk/tastytrade_transport.py`: `MEASURE_CHANNEL`; `Session(measure=...)` requests
  and decodes that channel separately (errors/closure withhold only the measurement);
  `capture(measure=...)`.
- `src/desk/quote_check.py`: `--measure`, `--host-clock`; the report adds
  `measurements` (`NOT_REQUESTED` without the flag) and `clock.host_clock`
  (`NOT_MEASURED` without a file); it is written through the credential guard. Its
  600-second bound is unchanged.
- `tests/test_quote_measure.py` (new, 33); `docs/TASTYTRADE_QUOTES.md`.

Design change after the pre-code record: `compare` requires the same `dayId` at all four
receipt edges, not one connection. `dayVolume` is the provider's cumulative counter, so a
reconnect or segment change between edges does not break it; with 600-second segments a
30-minute window always spans several connections. Connections spanned and both
bracket widths are reported as context. `dayId` is requested but not required: a map
without it records `ABSENT`, and `compare` then refuses (`NOT_COMPARABLE_DAY_ID_UNAVAILABLE`).
Record caps are per record type, so a busy BBO stream cannot crowd out volume records.

Offline replay of the saved 2026-10-05 `t1-raw-frames.json` (two lazy FEED_CONFIGs,
then Trade and Quote data; synthetic identities, receipt = last Trade time + 50 ms):
Trade map `ACCEPTED` without side times, Quote map `ACCEPTED` with
`side_times_in_accepted_map` = both true; SPY 3 and NVDA 4 Quote rows all
`bid=ZERO,ask=ZERO`, verdict `QUOTE_TIME_UNAVAILABLE`; Trade times `VALUE`. So the
missing BBO time is in the data, not the schema; it stays unavailable. The saved Greeks
files (`t1/r2–r5-greeks.json`) hold provider times but no receipt times, so their ages
cannot be recomputed; a new bounded capture is required.

Before: the base has no measurement path (the new test module cannot import). After: 33
passed; the nine earlier quote test files plus these: 273 passed on Python 3.12.3.

Focused mutations (each reverted; all killed): measurement rows fed into the quote
service; Greeks age from receipt instead of their own time; a future time recorded as
a value; an observer fault propagating; a measurement-channel error stopping the core;
a `dayId` change ignored by `compare`; a segment longer than 600 s; decision time equal
to receipt; the credential guard off; a restored map not distinguished.

Limitations: raw Greeks only (no indexed-event reduction, so no current value); no
BBO freshness contract; dayVolume units, odd-lot treatment, channel breadth and the
exchange cutoff are not established; receipt brackets are not an RTH total; the host
clock measurement is the iMac's only when run there; no provider call was made.

**Final verification (combined strict suites, `-W error`, on `f95d1ff`'s code; the runs
started before that commit and only its record text changed after):** Python 3.12.3:
1744 passed (305.3 s); Python 3.13.14: 1744 passed (313.9 s). `git diff --check` over
the three packages: clean. Next: Astra's audit of `e237309` and these three packages.
