# G5a checkpoint 3 — scoped discovery history

2026-10-04. Implementer: Claude (cloud container). Auditor: Astra. Requested by Taz
("Claude implementation prompt: G5a Checkpoint 3 only", 22:29Z, relayed; saved verbatim at
`research/ai-trading/g5/audits/g5a-cp3-implementation-prompt-2026-10-04-verbatim.md` in the
project folder). Trader-day step: **watch** (the Friday leader build). Nothing here is
Astra's approval. Step 09, runner activation and iMac changes are not started.

## Before-code record (written before any code change)

### Baseline

- `codex/repair-step-01-baseline` at `ba78ba055179a8f6c487371efc65f27bb967ff21`, the
  revision Astra accepted for CP2 (1353 strict on Python 3.14.6, 71 targeted on 3.12.14,
  as relayed). `git fetch` shows no newer commit on that branch or on
  `claude/alpaca-volume-checkpoint-1-2slcsc`; the working tree is clean. Checked 2026-10-04.
- Read: `AGENTS.md`, `CLAUDE.md`, `docs/REPAIR_PLAN.md`, `docs/G5_ACCEPTANCE.md`,
  `docs/checkpoints/G5a-alpaca-volume.md`, `docs/checkpoints/G5a-cp2-volume-consumers.md`,
  and the code listed in the prompt's section 5. Astra's CP2 closure text was not
  attached; only the coordinator's summary of it arrived.

### Requirement (User policy, Taz 22:29Z)

An old malformed Webull daily row outside the proven discovery dependency interval must
not stop a valid discovery calculation. The same row must still reject every consumer
that needs it (all current full-history consumers). A defect inside the discovery
interval rejects that ticker's discovery path. Full-history diagnostics keep the
original row, session, field and reason. No trading rule, threshold, weight, budget,
approval or earnings policy changes.

### Evidence labels used here

- **Checked**: read in the code at `ba78ba0` or run in this container.
- **Sourced**: the two sources the prompt cites (pandas `ewm`, TA-Lib unstable period).
  They justify keeping recursive indicators on full history; they do not prove a 260-row
  window reproduces any setup indicator.
- **Assumption**: listed in the "Assumptions" section with how the journal or Taz can
  check them.

### Dependency inventory (Checked at `ba78ba0`; producers and actual reads)

`minimum_history()` is an eligibility threshold, not the numerical dependency. "Full"
below means `scanner.fetch(..., "D", DAILY_BARS=1000)` through `VendorBasisSource.bars`,
which also fetches 1000 daily rows for the price evidence of every M15 request.

| Consumer | TF / fields read | Endpoint, lags, rolling windows | Recursive | Shape search / anchor | Calendar | Benchmark | Volume source, window | Evidence persisted | Entry / stop / target | Request policy (CP3) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Discovery** `_leader_scan_job` → `watchlist.leader_scan` | D close, volume; SPY close | history gate 260 rows; close[-1] > $10; returns lags 21/63/126; rs12m lags 63/126/189/252 (needs 253 rows); SMA50/150/200 at the end and SMA200 21 sessions back (needs 221); 252-row high/low; RS line on matched SPY dates, 252-row max and 50-row mean | none (SMA, rolling max/min, ratios) | none | contiguous exchange sessions ending at the latest completed session | SPY, same dates, inner join, no fill | Alpaca native-daily SIP 50 sessions ending at `complete_through` (Thursday at the Friday 16:40 build), else Webull last 50 rows | build report, liquidity evidence digests, vendor history store | none (watchlist only) | **scoped: 260 sessions ending at the cutoff, plus any liquidity dates** |
| Trend Template (inside close scan, per watchlist name) | D features from `daily_features` | as discovery | none for the gate itself | — | as above | SPY | — | signal saw | — | full (unchanged) |
| Market filter `market_filter` (close scan) | SPY/QQQ close, SMA50 at end and 5 back, SMA200 | 200-row `require` | none | — | contiguous | itself | — | scan record | — | full |
| 1 Qullamaggie breakout | D high, low, close, SMA20, ADR%20 | base 10–40 rows after a 63-row prior move (≤103 rows); TT gate | none | yes, base length loop | contiguous | via TT | — | signal terms | trigger base high; stop = session low at entry (M15); no target | full |
| 2 Minervini VCP | D high, low, close, volume | highest high of the last 150 rows, 2-bar swings; TT | none | yes, contractions from the base high | contiguous | via TT | Alpaca/Webull 50 sessions (10 vs 50 mean) | volume evidence + terms | pivot; stop last contraction low − 0.05; no target | full |
| 3 O'Neil cup | D high, low, close, volume | handle ≤ 26 rows, cup 35–325 rows, prior advance 126 rows before the left lip (≤ ~477 rows, clamped by available history) | none | yes, range depends on history length | contiguous | via TT | Alpaca/Webull 50 sessions + handle | volume evidence + terms | pivot = right high + increment; stop max(−8%, handle low − 0.05); target +20% | full |
| 4 Darvas | D high, low, close, high_52w, ADR%20 | new high within 10 rows; 252-row high at that row (≤ 262 rows) | none | box after the high | contiguous | via TT | breakout volume is card text only, not checked | terms | box top; stop box bottom − 0.05 | full |
| 5 EP (`episodic_pivots`) | D close, high, low, ADR%20; M15 open and first two bars | 60-row sideways base; 50 prior sessions | none | — | contiguous; entry = next session | — | Alpaca daily 50 prior + RTH 09:30/09:45 (or Webull) | ep volume evidence + terms | trigger today's open → ORH 15/60 on day one; stop session low | full |
| 6 Kell crossback | D EMA10/20, ATR14, high, low, close | 5-row rising, 40-row fresh cross | **EMA, Wilder ATR** | first pullback since cross | contiguous | via TT | — | terms | today's high; stop today's low − 0.05 | full |
| 7 Luk reclaim | D EMA9/21, EMA50 (inline), ATR14, ADR%20, close lags 21/63/126 | anchored VWAP from the lowest low of 63 rows | **EMA, Wilder ATR** | AVWAP anchor | contiguous | via TT | Webull typical price × volume from the anchor | terms | level (EMA21/AVWAP); stop max(low − 0.05, ADR cap) | full |
| 8 Holy Grail | D ADX/±DI14, EMA20, SMA50, ATR14 | 10-row swing, ADX 5 back | **Wilder ADX/DI, EMA, ATR** | swing extreme | contiguous | — | — | terms | today's high/low; stop swing − 0.05; target swing | full |
| 9 Weinstein stage 4 | D SMA150 (end, 5 back, 41-row window), low/high | support 40 rows, rally 10 rows | none | top window | contiguous | SPY RS 50-row mean | — | terms | close; stop rally high + 0.05 | full |
| 10 Connors RSI(2) | D SMA200, RSI2, ATR10; developing daily from M15 at 15:45 | 200 | **Wilder RSI, ATR** | — | contiguous | — | developing volume masked | snapshot evidence | close; stop 2.5 × ATR10 | full |
| Hourly `hourly_from_m15`, weekly `weekly_from_daily` | — | — | — | — | — | — | — | — | — | defined; no production caller at `ba78ba0` (Checked: grep). Kell's "1-hour" is card text; `entry_hit` uses the generic first-15-minute rule |
| Entry checks `entry_hit` / `intraday_scan` | M15 RTH completed bars today | 40-row M15 request | none | first bar high/low, EP ORH | completed RTH slots | — | — | event rows | crossings only | full daily evidence inside the M15 request (unchanged) |
| Signal revalidation `revalidate_signal` | M15 + daily evidence | `compatible_prices`: every original stored row present and unchanged | — | raw M15 anchor of the latest daily session | completed | — | Alpaca cache refresh | candidate/event state | quote vs stop/entry/chase | full (unchanged) |
| Ticket price evidence `risk_terms` / `tickets` | stored event terms (price basis minus `verified_at`), trusted quote | — | — | — | — | — | volume guard only when the event depends on Alpaca (CP2 R2) | binding digest | no new price fetch | unchanged |
| Rebuild `revision_rebuild` → `close_scan(preparing)` | as close scan | — | — | — | — | — | — | new candidate | — | full |
| Diagnostics `vendor_check`, `data_acceptance`, `screen_check` | D/M15 | 1000 | — | — | — | — | Webull | reports | — | full |

[Sourced] pandas documents exponentially weighted results as depending recursively on
earlier results, and `min_periods` only controls emission
(<https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.ewm.html>); TA-Lib marks
EMA as having an unstable period
(<https://ta-lib.github.io/ta-lib-python/func_groups/overlap_studies.html>). So every row
marked recursive keeps its full request, and so do the shape searches whose range depends
on how much history is supplied (cup, VCP base). Only discovery is scoped here.

### Discovery window (Checked against the code)

The scoped window is the 260 exchange sessions ending at the latest completed session
at the decision clock (`latest_closed_session`), using `desk.calendar` (holidays and
half-days included). 260 keeps the existing history gate and covers every discovery
read: lag 252 needs 253 rows, SMA200 21 back needs 221, the 252-row high/low and RS
window need 252. When Alpaca SIP liquidity is attached, its 50 final sessions end at
`complete_through(now)`; at the Friday 16:40 build that is Thursday while prices end
Friday. The scope's required start is the earlier of the two starts, so the price window
always contains every liquidity date (at present the 260-session start is always
earlier; the scope still checks it). SPY uses the same scope; a missing SPY session is
never filled.

The provider request asks for the required sessions plus a margin of 5 earlier sessions
(`count` = sessions in that range, `start_time` at the margin start, `end_time` one
millisecond before the cutoff session's close so the forming bar is excluded at the
provider). The margin rows are classified outside the scope; their presence is what
proves a missing first required session is a gap rather than the start of a young
listing.

### Contracts to add

- **Typed scope** (`desk/history_scope.py`, new): `HistoryScope` binds consumer
  (`weekly-leader-discovery`), policy (`discovery-history-v1`), symbol and Webull
  instrument ID, timeframe `D`, completed-session cutoff, required start and end sessions
  and the liquidity end. Its validator recomputes the window from the policy, so a caller
  cannot shorten it; anything else is refused. `scope_id` is the digest of those fields.
- **Pre-parse classification** (`WebullData.bars_scoped`): ordinary `bars()` and
  `bars_partial()` stay strict. The scoped method reads each row's timestamp under the
  existing contract (ISO with zone, or an integer in the configured unit) before any
  OHLCV parsing. A timestamp that cannot be parsed, or a daily label that is not an ET
  session midnight, rejects that ticker. Rows dated before the required start or after
  the cutoff are excluded and checked separately for diagnostics. Required rows go
  through the unchanged strict parser and `validate`. Delay, identity, duplicate-symbol,
  response-shape and receipt checks run exactly as today and cannot be waived.
- **Vendor wrapper** (`VendorBasisSource.discovery_bars`): metadata, category, metadata
  age and identity pin as today; scoped daily request; required-window validation (aware
  ET session labels, contiguous required sessions, ends at the cutoff; a gap or a missing
  first session with older rows present is `MISSING_REQUIRED_SESSIONS`; a response that
  hit its row cap without reaching the start is `SCOPED_HISTORY_TRUNCATED`; a contiguous
  series with no older rows is short history and keeps the existing "under a year"
  eligibility outcome); the unchanged raw M15 anchor check; price evidence with method
  `webull-discovery-v1`. An inner source without `bars_scoped` raises
  `DISCOVERY_SCOPE_UNSUPPORTED` before any request, and the scanner then uses the
  unchanged full path and says so in the build report.
- **Discovery features** (`indicators.discovery_features`): only close, SMA50/150/200 and
  the 252-row high/low, with the existing `sma()` and `Settings`. `leader_scan` uses it for
  the Trend Template on every path. `daily_features` and `triggers.scan` refuse a frame
  marked with a discovery scope, so no NaN recursive column can reach setup evaluation.
- **Evidence separation** (`VendorHistoryStore`): new `scope`/`scope_digest` columns on
  `observations` (NULL = legacy = full history), a separate `scoped_current` pointer
  keyed by host, symbol and scope, and a `history_defects` table. Discovery never writes
  the full-history `current` pointer. `latest(..., scope)` filters by scope, so a
  discovery success cannot clear a full-history failure. A discovery fetch that sees a
  common session changed (against the full-history snapshot or its own previous one)
  records `REVISED`, and `latest_revision()` reads revisions from every scope, so the
  revision stays visible to full-history consumers. `compatible_prices()` keeps its
  every-original-row rule and also refuses discovery evidence outright (withhold, not
  invalidate). The signal store refuses to arm a signal carrying discovery evidence.
- **Diagnostics**: the strict parser reports the first offending row (index, timestamp,
  field, reason). `bars_partial` keeps that per ticker (`last_partial_errors`), and the
  full-history wrapper reports `DAILY_ROW_INVALID: <reason> at <session> (row i, field f)`
  instead of `DAILY_PROVIDER_UNAVAILABLE`, which remains only for a ticker the provider
  did not return. Each defect is stored with host, symbol, instrument ID, scope, timeframe,
  receipt time, requested and required interval, row index and time, field, reason,
  `EXCLUDED_OUTSIDE_SCOPE` or `REJECTED_REQUIRED_DATA`, the sanitized row (time and OHLCV
  only) and its digest; at most 20 per ticker per response, with the total counted. No
  credentials or headers are stored, and nothing is refetched to look for old defects.
- **Scanner**: `_leader_scan_job` requests the scope through `fetch_scoped` (no per-ticker
  retry of a failed batch) and publishes through the existing `ScanLog` build path; the
  report gains `history_scope` (policy, window, requests, per-ticker exclusions and
  rejections). Candidate sources, the $10 and 1M rules, the 50-session volume calculation,
  ranking arithmetic, Trend Template, 50-name cap, core ETFs, picks, PARTIAL / FAILED /
  EMPTY / INCOMPLETE and restart behavior are unchanged. `DAILY_BARS` stays 1000.

### Acceptance cases (prompt section 9; fixtures through the real `WebullData` parser, `VendorBasisSource`, scanner and `ScanLog`)

1. Full 1000-row versus scoped inputs agree on features, returns, scores, checks and
   leaders for one population, with a flat-series tie control.
2. A defect before the required start is stored as `EXCLUDED_OUTSIDE_SCOPE` and does not
   block; the full-history path still refuses that ticker with the row's session.
3. The defect on the first, last and an interior required session rejects that ticker;
   peers publish PARTIAL.
4. Bad/unlocatable timestamps, duplicates, NaN/inf, negative volume, impossible high/low,
   stale receipt and wrong identity refuse.
5. A missing required session or a suspension gap is refused, never filled; zero volume
   alone passes.
6. Exactly 260 sessions, fewer than 260 (eligibility), lag-252 indexing, holidays,
   half-day, weekend cutoff, Friday price end versus Thursday liquidity end.
7. A required SPY defect fails the build; an old SPY defect does not.
8. Excess old rows hit the pre-parse boundary; ordinary strict parsing still rejects the
   same full response.
9. Interleaved discovery and full requests with restarts keep separate pointers and
   health; a discovery success never clears a full-history failure.
10. A revised overlapping row stays observable and invalidates old signals; shortened
    discovery evidence cannot revalidate a full-history signal; a missing raw M15 anchor
    still refuses.
11. CP2 suites, the strict suite, and the bounded request count and range.

### Rollback

Revert the CP3 commit. The added store columns and tables are ignored by the old code;
`current`, `snapshots`, `identities` and legacy observation rows are untouched, so the
old full-history path reads exactly what it read before. Discovery then returns to the
1000-row path.

### Plan B

If Webull ever stops returning the requested bounded range, `bars_scoped` still
classifies the excess rows before parsing (tested with a provider that ignores
`start_time`). If the scoped path itself misbehaves, removing `discovery_bars` from the
wrapper (or reverting) puts discovery back on the unchanged strict path, which only
costs the names whose old rows are malformed.

### Assumptions (for Taz)

- TA-Lib's SMA uses a running sum, so the same SMA computed from 260 rows and from 1000
  rows can differ in the last floating-point bits. Checked on 2000 random series: the
  largest relative difference in SMA50/150/200 over the last 22 rows was 1.9e-14. A Trend
  Template comparison could only change if an SMA sits that close to its threshold; flat
  series ties agree exactly. No tolerance is added to any decision.
- A contiguous scoped series with no row before the window and fewer rows than requested
  is treated as a genuinely short history, as the full path already does.
