# Trading desk repair plan and step tracker

**Current priority, 2026-10-01:** execute gap repairs G1–G5 in
[GAP_REPAIR_PLAN.md](GAP_REPAIR_PLAN.md) before resuming unfinished Step 09.
The prior numbered checkpoints below retain their bounded historical scope;
they are not claims that the identified operational gaps are resolved.
G1 isolates earnings failures; G2 repairs actual stop construction and risk terms;
G3 tests scalable vendor-basis/revision handling; G4 binds ticket confirmation;
G5 verifies integration. G1/G2 implemented: see `checkpoints/G2-stop-risk.md` and
`RISK_TERMS.md`. G3–G5 remain. G2 pulls forward only the actual day-low stop repair
from Step 10; the rest of Step 10 and operational runner start are not implied.

Prepared 2026-09-30; updated 2026-10-01. Status: Steps 01–08 complete for their documented scope. **Step 09 is reopened and IN PROGRESS.** Closing it after the single-company reviewed-file check was premature. 688 tests and the iMac replay verified the existing gates/fallback, not automatic earnings supply. See `checkpoints/09b-automatic-sources.md` for the remaining source access, reported-results, calendar, catalyst, integration and actual-host acceptance work. Step 10 has not started. Earlier checkpoint closure statements are historical and superseded by this correction.

Installation follow-up: [06a-resource-cleanup.md](checkpoints/06a-resource-cleanup.md)
records the Python 3.14 iMac test failure, its verified cleanup fix, and the six
remaining Webull checks. Local automated success does not complete live acceptance.
The iMac re-run passed (328 tests). [06b-sessions-and-history.md](checkpoints/06b-sessions-and-history.md)
records subsequent sandbox adapter/history evidence, explicit session/window fixes,
and the current six-item status. RTH freshness, corporate actions and daily-volume
definitions remain unresolved; the zero delay field is not real-time proof.
[06c-price-volume-basis.md](checkpoints/06c-price-volume-basis.md) records the
subsequent action/identity and volume-comparability safeguards, with 391 passing
tests on Python 3.12 and 3.14; Taz also reported 391 passed on the iMac on
2026-10-01. Real action-source and volume-definition acceptance
remain open; [DATA_BASIS_ACCEPTANCE.md](DATA_BASIS_ACCEPTANCE.md) contains the
evidence request. Step 07 remains paused.

[06d-provider-repair.md](checkpoints/06d-provider-repair.md) records sandbox pacing,
product-aware HTTP diagnostics, a read-only retail route probe, reviewed action-term
revision generation and corrected composed-bar provenance. Strict suites pass
426 tests on both local Python 3.12 and 3.14. Taz requested a research-based EP
volume decision: retain native daily volume and the existing 50-day/0.5 rule;
do not substitute an RTH-only baseline merely to reconcile totals. Corporate-action
source access/identity mapping, real volume definitions and the separate regular-
session timing report remain open. These local repairs do not close Step 06.

[06e-alphavantage-client.md](checkpoints/06e-alphavantage-client.md) adds a bounded
read-only Alpha Vantage client and direct check command with safe HTTP-200 error
handling and stop-on-limit behavior. Strict local suites pass 465 tests on Python
3.12 and 3.14. The reported REST rate limit does not prove usable source access.
Successful authenticated data, real security mapping/coverage and reconciliation
remain open. Claude cloud can run the direct check without waiting for the iMac.

[06f-action-ledger.md](checkpoints/06f-action-ledger.md) integrates paired Alpha
Vantage observations with a persistent reviewed action ledger and opt-in scanner
bar source (492 strict tests pass on both Python 3.12 and 3.14). Zero-dollar dividends are retained and ignored economically under
Taz's decision. New/corrected/removed events invalidate prior signal evidence.
The supplied four source checks and AAPL/SPY RTH timing observations are accepted.
Live mapping/channel configuration and Webull price/volume acceptance remain open;
no real decision profile or scheduled runner is activated. Earlier status notes
above record the state at their respective checkpoints, not the current result.

[06g-price-volume-acceptance.md](checkpoints/06g-price-volume-acceptance.md)
implements the explicit native-daily/RTH-minute volume comparison, split-bounded
volume windows, scoped NVDA/SPY configuration and `desk.data_acceptance` diagnostic.
It retains the EP formula and applies no guessed adjustments. The credentialed
checks now pass: NVDA in Claude cloud and SPY on the iMac, followed by Taz's
iMac confirmation of 508 tests in 5.10s. Step 06 is closed for this bounded
scope; see [reported results](evidence/step06-integrated-results.md). No runner
is activated and the 2026-10-01 profiles do not authorize later-date use.

This is the implementation sequence for the repair plan selected by Taz, incorporating
the strongest parts of Opus's plan. The purpose is retail AI assistance through
watch -> analyze -> plan -> approve -> manage -> journal. Each row is a stopping point.
The repair-step numbers below are separate from the original seven blueprint build steps.

## Authority and approved decisions

The conversation's explicit user decisions take precedence over older blueprint,
code and documentation. The baseline code still contains superseded behavior; the
step tracker says when each repair actually lands.

| ID | Decision | Evidence/status | Implementation step |
| --- | --- | --- | --- |
| D01 | Use the Codex repair plan, incorporating Opus's concrete examples and impact records. | User instruction, 2026-09-30 | All |
| D02 | Taz chooses a dollar risk budget for each trade. Remove the hard $100 per-trade ceiling and automatic A/B/C dollar assignments. Grade describes quality, not an automatic budget. | User instruction and budget-selection reply, 2026-09-30 | 02, 13 |
| D03 | Remove the 11:00 ET EP condition, including a time-based opening-range switch. Later opportunities are evaluated on valid data and the setup rules, not rejected solely for being after 11:00. | User instruction, 2026-09-30 | 03, 10 |
| D04 | User approves every entry; changing executable terms invalidates the previous approval. | Existing blueprint requirement, retained in selected repair plan | 13, 15 |
| D05 | The $200 daily loss, $400 weekly loss and 10% drawdown conditions are warnings the user may override at approval; no automatic account halt. An explicit manual stop remains separate. | Latest explicit user answer supersedes PR #1 automatic halts; thresholds remain policy assumptions | 02, 05 |
| D06 | No trade-count, position-count or sector-count caps; display exposure. Stock eligibility is separate from option suitability, preserving shares as an alternative. | Selected repair plan / existing user policy | 02, 05, 08, 11 |
| D07 | Retain the 30% Trend Template rule and 25% EP growth threshold; 50%+ is a comparison tag. | Existing user decisions / current cards | 08, 09, 10 |
| D08 | No multi-year backtesting requirement or invented statistical gate. Use focused point-in-time examples, live-data checks and forward records. | User instruction / CLAUDE.md | All |
| D10 | Include long debit calendars and credit iron condors in the initial supported structures; management and broker verification still required. | Explicit user reply | 04, 11, 16 |
| D11 | HALF is advisory: show the concern and sizing alternatives; leave the user-entered dollar budget unchanged. Daily/weekly loss, drawdown and bearish-market conditions are warnings with explicit per-review acknowledgement. | Latest user preference, interpreted explicitly in conversation | 05, 13 |
| D12 | Push Steps 01–06 to the dedicated repair branch once 06 is verified; then push each later step after its checks pass. Do not merge implicitly. | Explicit user authorization | 06 onward |
| D09 | Number the work; verify and checkpoint each step before advancing. Taz's latest instruction authorizes proceeding sequentially after verification. Pause on an unresolved trading-policy choice or failed acceptance check. | Latest user instruction, 2026-09-30 | All |

Research labels remain **Sourced**, **Checked**, and **Assumption**. A user-selected
policy is labelled as such; a test passing does not turn a policy assumption into
evidence of profitable trading. Keep source links or book editions/pages alongside
individual setup rules. Do not claim book verification when the passage is unavailable.

## How to use this tracker

- Only one numbered step is active at a time. Taz has authorized sequential advancement
  after verification; each step still needs its own checkpoint and completion evidence.
- Open the latest checkpoint, verify the checkout, and name the step before edits.
- Use the change record below. Close the step only when its listed exit criteria pass.
- On completion, mark the row done and add a checkpoint with exact evidence.
- If a step is blocked, state the dependency and leave it incomplete. Do not silently
  skip it, declare it done, or change a trading requirement to make a test pass.
- A discovered cross-cutting defect is recorded against its owning step. If it blocks
  the active step, amend the current scope visibly before fixing it.
- A second reviewer is desirable for behavior changes. If no independent reviewer
  was used, say so; do not label self-review as independent review.

## Numbered implementation sequence

Ownership is proposed, based on the documents reviewed, not a claim of model superiority.
Claude work starts only after a concrete handoff from Taz or an authorized coordination
workflow. No agent has been dispatched by creating this plan.

| Done | Step | Deliverable | Proposed implementer / reviewer | Completion criterion |
| --- | --- | --- | --- | --- |
| [x] | 01 | Baseline, branch reconciliation and repair instructions | Codex | Both branches and a temporary combination tested; integration decision and full tracker recorded; preview reverted; checkpoint saved. |
| [x] | 02 | User-selected dollar risk; reconcile PR #1 | Codex / Claude | One coherent risk policy; no $100 ceiling or grade-dollar assignment; explicit finite positive budget; combined tests pass. |
| [x] | 03 | Remove the 11:00 EP condition | Codex / Claude | Runtime, cards, descriptions and tests agree; no 11:00 cutoff/switch; no use of an unfinished opening range. |
| [x] | 04 | Shared contracts, option identity and loss calculations | Codex / Claude | Verified contract metadata, allowed structures, independently computed loss measures and integer quantities; malformed proposals fail closed. |
| [x] | 05 | Market/account gates and persistent risk state | Codex / Claude | Trusted setup eligibility; user-overridable account/market warnings; manual stop and exposure recover after restart; no count caps. |
| [x] | 06 | Market-data timing, provenance and trading calendar | Codex / Claude | Timeframe-aware freshness/completion, session and corporate-action handling tested; live-only uncertainties explicitly separated. |
| [x] | 07 | Persistent signal lifecycle | Codex / Claude | Unique events survive scans/restarts; stale or expired approval eligibility cannot persist; long/short rules and re-entry policy explicit. |
| [x] | 08 | Discovery and watchlist coverage | Codex / self-review | Leaders, movers, bearish candidates, ETFs and user additions reach appropriate setup checks; options do not gate stock discovery. |
| [ ] | 09 | Earnings growth and catalyst evidence | Codex / self-review | Reported results separated from upcoming earnings; point-in-time growth/catalyst gates and unknown-data behavior tested. |
| [ ] | 10 | Qullamaggie breakout and EP fidelity | Claude / Codex | Structural stops, completed entry ranges, growth/volume/chase gates and positive/negative chart examples agree with approved cards. |
| [ ] | 11 | Instrument selection and executable exits | Codex / Claude | Shares/options compared on fresh executable inputs; whole-contract exits explicit; no automatic substitution of a full first-target exit. |
| [ ] | 12 | Analyst and evidence-grounded plan drafts | Claude / Codex | Structured facts in; rationale, countercase, grade and draft plan out; invalid output and timeouts handled without fabricated values. |
| [ ] | 13 | Budget selection, final ticket and exact approval | Codex / Claude | Taz enters budget, code sizes, final terms are displayed and approved; approval is version-bound, expiring and single-use. |
| [ ] | 14 | Journal and fair comparisons | Claude / Codex | Every opportunity/decision logged point-in-time; all-trigger and matched-execution comparisons distinguish AI choice, human delay and fills. |
| [ ] | 15 | Alpaca paper order adapter | Codex / Claude | Approval required; revalidation, idempotency, rejects, partial fills and cancel races exercised against simulated cases and the paper API. |
| [ ] | 16 | Position management and first complete paper path | Codex / Claude | Entry through exit/reconciliation/journal works; restart and exit-failure scenarios exercised before unattended paper operation. |
| [ ] | 17 | VCP, cup-with-handle and Darvas repairs | Claude / Codex | Each card has source/policy traceability, correct volume/chase/shape checks, point-in-time fixtures and full-path compatibility. |
| [ ] | 18 | Kell crossback and Luk reclaim repairs | Claude / Codex | Correct timeframe and fresh trigger semantics; stop anchors and any broader filters explicitly resolved; full-path checks pass. |
| [ ] | 19 | Holy Grail, Weinstein and Connors repairs | Claude / Codex | Direction-aware entries/exits, correct weekly/intraday inputs and timestamped near-close snapshots; all ten cards reconciled. |
| [ ] | 20 | Operational reliability and read-only live-data acceptance | Codex / Claude | Calendar-correct scheduling, single runner, durable state, recovery, monitoring and real data-contract checks demonstrated. |
| [ ] | 21 | Forward paper observation and review | Joint, Taz reviews | Existing observation requirements evidenced honestly; costs/fills/selection comparisons reported; no automatic live promotion. |
| [ ] | 22 | Explicitly approved small live pilot | Taz authorizes; implementer assigned then | Broker/account eligibility checked, approved route and quantities confirmed, operational controls verified; separate activation decision recorded. |

The early complete path uses the Qullamaggie breakout in shares as an engineering
test path, not a claim it is the most profitable strategy. EP repairs remain early
because Taz explicitly requested them. Remaining setup families are completed after
the first full paper path, preventing a scanner-only build from hiding integration gaps.

## Step details, dependencies and acceptance examples

### 01 — Establish the baseline (watch, plan, journal)

Read repository instructions; pin working-branch and PR heads and their merge base;
inspect their actual differences; create an isolated local repair branch; install an
isolated test environment; test both histories and a temporary merge. Write the
reconciliation decision and record runtime/dependency versions. Publish a user-facing
copy of this checklist and checkpoint. Do not deploy or call a broker.

Evidence: `docs/checkpoints/01-baseline.md`. Integration is planned and compatibility
tested here; PR #1 is not permanently incorporated until Step 02.

### 02 — User-selected risk budget (plan, approve)

Depends on 01. Incorporate PR #1's two commits into the latest working-branch lineage,
then remove superseded ceiling/default-grade policy before treating the result as
the new baseline. Preserve newer scanner/setup work. Resolve the CLAUDE.md documentation
overlap using latest user decisions, not a blanket ours/theirs selection.

Existing targets: `src/desk/contracts.py`, `src/desk/risk.py`, `tests/conftest.py`,
`tests/test_risk.py`, `tests/test_risk_v23.py`, PR #1's `tests/test_risk_v24.py`, and
`CLAUDE.md`. Search all consumers of `risk_usd`, `GRADE_RISK_USD`,
`suggested_risk_usd` and `max_risk_per_trade_usd` before changing their interfaces.

Acceptance: a fixture with a $150 selected budget must not be clipped to $100;
zero/negative/NaN/infinite/missing budgets fail; grades cannot change an identical
budget's size; quantity rounds down and never grows an existing proposal; retained
account, tradability, quote-age and buying-power checks still work. Sizing fees and
slippage must have an explicit convention, not be hidden outside the budget.
For a fixture with $60 total estimated stop loss per whole unit, including its fixture
costs, a $150 budget allows two units, not three. This is arithmetic test data, not
a recommended trading budget. Full independent option-loss verification follows in 04.

### 03 — EP timing correction (watch)

Depends on 02. Targets: `scanner.py` (`EP_CUTOFF`, `entry_hit`),
`playbook/cards.py`, `playbook/triggers.py`, `test_scanner.py`, `test_triggers.py`,
`test_cards.py`. Review frozen-card fingerprint expectations as intentional policy changes.

Use completed opening ranges according to actual bar timestamps. The first-hour
range cannot exist before its constituent bars finish. Define explicitly how the
15-minute and 60-minute alternatives interact, with one signal identity; do not
invent an 11:00 substitute deadline. The approved policy allows later valid day-one
entries; late timestamps can be journal metadata without becoming an extra gate.
Acceptance: time-shifted valid fixtures straddling 11:00 remain eligible; incomplete
hour bars cannot leak future values; genuine stop/chase/staleness failures still fail.

### 04 — Contracts and independently checked loss (plan, approve, manage)

Depends on 02–03. Targets: `contracts.py`, `risk.py`, shared fixtures/tests; add a
dedicated instrument-validation module if it keeps these interfaces clearer.
Define setup/version IDs, evidence timestamps, source provenance, quantity units,
option underlying/right/strike/expiry/multiplier/deliverable and broker identity.
Separate estimated stop loss, contractual maximum loss, premium and buying power.
Do not trust model/caller-declared losses or infer a structure from its name alone.

Acceptance: explicit call/put strike ordering, matching underlying/expiry/quantity,
integer strategy units, finite prices/costs, verified loss arithmetic; reject mislabeled,
uncovered, unsupported or adjusted contracts until supported correctly. A valid trade
with greater premium than its stop-risk budget is not rejected solely for that difference.
Contractual option maxima must not be misrepresented as guaranteed managed-trade losses.

### 05 — Account and market gates (approve, manage)

Depends on 04. Targets: `risk.py`, `contracts.py`, `playbook/filters.py`, and new
durable account-state storage. Restore open/pending positions for exposure displays;
no count caps. Use a trusted setup registry rather than proposal-supplied live tier.
P&L includes realized/unrealized changes and fees, excludes external cash flows, and uses explicit ET day/Monday week baselines. Persist high-water mark, manual stop and exposure; no silent P&L reset.
HALF is advisory under D11: display the market concern without changing the selected
budget. Daily/weekly/drawdown and bearish-market conditions are warnings under the
latest user answer. Bind acknowledgements to the exact review and account revision;
manual stops and technical data/broker requirements remain separate.

Acceptance: regime/grade combinations, halt boundary/restart, rejected orders, open
and pending exposure and authorized reset. Plan B: block new entries on uncertain
account state while preserving the position-management recovery path.

### 06 — Data contracts and calendar (watch, analyze)

Depends on 04. Targets: `bars.py`, `webull.py`, `scanner.py`, `indicators.py`, their
tests and a shared exchange-calendar helper. Define timestamp meaning, forming versus
closed bars, timezone, session, expected latest completed bar, delayed-feed flags,
duplicates/gaps, adjusted daily versus intraday prices and split handling. Build hourly
and weekly bars consistently. Do not use one 20-minute freshness limit for every series.

Acceptance: delayed/unknown/future/missing data, regular and early closes, DST, hour
boundaries, splits and missing constituents. Preserve indicator parity checks. Mark
provider-specific semantics that require actual read-only Webull evidence for Step 20;
simulated tests cannot certify those semantics. Plan B: no eligible signal on unknown data.

### 07 — Signal lifecycle (watch, approve, journal)

Complete: [checkpoint 07](checkpoints/07-signal-lifecycle.md); durable event history,
fresh-data revalidation and 564 passing tests on Python 3.12/3.14. Runtime approval
and execution adapters remain their later numbered steps.

Depends on 06. Targets: `scanner.py`, signal contracts and durable state.
Separate armed/triggered/invalidated signal history from approval/order state. Use
event IDs, first trigger time and expiry. Repeated scans do not duplicate a trigger.
Clock-based expiry continues when no new bars arrive. Revalidate eligibility before
approval/execution; use direction-aware stop/chase comparisons. Define legitimate
new events/re-entry without a blanket one-trigger-per-day rule.

Acceptance: restart, missing bar, delayed approval, failed breakout, new eligible event,
duplicate scan and long/short symmetry. Plan B: retain history but suspend new eligibility.

### 08 — Discovery (watch)

Completed 2026-10-01. Read `checkpoints/08-discovery.md` and `DISCOVERY.md`.
621 strict tests pass on Python 3.12/3.14 after follow-up 08a. NVDA/SPY/BRK.B
metadata and D/M15 identities passed on the iMac sandbox connection. Read `checkpoints/08a-webull-symbols.md`. No runner activation.

Depends on 06–07. Targets: `watchlist.py`, `scanner.py`, provider metadata and tests.
Keep leadership, gap/momentum movers, declining/bearish candidates, core ETFs and
user additions distinct. Use security metadata instead of ticker-string guesses.
Assign warm-up history per setup; an unrelated long-history filter must not suppress
a young EP candidate. User additions should be analyzed promptly, not wait for Friday.
Keep stock eligibility separate from option liquidity. Mark stale/failed watchlist builds.

Acceptance: user ticker, non-optionable stock, bearish candidate, young ticker, malformed
metadata, empty healthy result and failed preparation have distinct observable outcomes.

### 09 — Earnings and catalyst evidence (watch, analyze)

Reopened. See `checkpoints/09b-automatic-sources.md` for the complete remaining
work and exit criteria. Passing gates and a manually prepared NVDA review do not
complete automatic watchlist coverage. Follow-up `checkpoints/09c-source-normalization.md`
repairs eight source interpretation findings. NVDA/AAPL/MSFT supplied historical
iMac mappings passed (MSFT through reviewed issuer fallback). Follow-up
`checkpoints/09e-refresh-catalysts.md` adds bounded opt-in refresh, cache and catalyst
review integration; see `EARNINGS_REFRESH.md` for one consolidated host check.
Live refresh, real catalyst review and broader watchlist coverage remain open.
Keep Step 10 paused until Step 09 exit criteria are resolved.

Depends on 06, 08. Targets: `webull.py`/provider adapter, setup context, scanner and
growth tests. Separate upcoming report dates from actual reported quarterly results.
Compare matching fiscal quarters and like-for-like EPS/revenue units and accounting
basis. Zero/negative comparison EPS cannot create a fabricated percentage; valid
sales growth can satisfy the approved OR rule. Keep 25% qualifying / 50%+ tagging.
Timestamp catalyst publication before the decision; do not backfill future evidence.

Acceptance: 24.9/25/50% boundaries, missing/negative EPS, sales-only qualification,
conflicting dates, post-trigger news, estimated versus confirmed next earnings.
Unknown required evidence may keep a candidate visible but cannot make it qualified.
Plan B: visibly incomplete candidate pending supported evidence, no fabricated catalyst.

### 10 — Core breakout and EP repairs (watch, plan)

Depends on 03, 06–09. Targets: their cards, trigger functions, scanner entries and
point-in-time fixtures. Use the structural intraday low and then check stop distance
against the card's ADR/ATR rule; never move the stop just to fit a budget. A cumulative
daily low cannot rise later in the same session. Define later management separately.
Use volume/chase conditions where each card requires them. Define cumulative
same-time-of-day volume precisely; Opus's 20-session baseline is an explicit assumption
to review, not a universal replacement for every card's volume definition.

Acceptance: valid, near-miss, borderline and missing-data examples; a correctly detected
setup that later loses still counts as correct detection. No future pivot confirmation
or full-day information in an earlier signal. Retain primary source traceability.

### 11 — Instrument selection (plan)

Depends on 04–05, 10. Add instrument-selection logic using current broker contracts,
quotes and liquidity. Compare shares and supported options at the same underlying
stop/target, with holding horizon, costs, expiry and buying power explicit. IV rank
may inform choices but cannot dictate them. Retain existing 14-day/spread/OI policies
unless separately changed. Reverify provider quote/Greek timing before using them.

Acceptance: no suitable option still permits an eligible shares proposal; illiquid or
stale options fail; quantities and exits work in whole units. A one-contract partial
exit requires an approved alternative, not an automatic full first-target sale or larger
position. Calendars and iron condors are included by D10; broker and assignment/expiry
management acceptance remains required before executable tickets. Plan B: eligible shares or no executable candidate.

### 12 — Analyst (analyze, plan)

Depends on 09–11. Add provider-neutral analyst interfaces/configuration. Supply a
timestamped fact pack of computed features and evidence; optional chart images add
context but do not become the source of numeric risk facts. Return evidence, countercase,
grade, rationale and structured draft or insufficient-data outcome. Code validates output.

Acceptance: clear setup, ambiguous setup, missing evidence, numeric contradiction,
prompt-injection text, malformed response and timeout. Record model/prompt/schema versions
and cost/latency. A runtime model cannot change fixed stops, gates or risk decisions.
Plan B: logged unavailable/insufficient result with deterministic facts retained.

### 13 — Planner, ticket and approval (plan, approve)

Depends on 02, 04–05, 07, 11–12. Add the budget-selection and ticket workflow.
Taz sees the setup and structural terms, selects risk, receives code-calculated quantity
and final terms, and explicitly approves. Show stop estimate, maximum loss, premium,
buying power, exits, next earnings, freshness and any reductions in plain language.
Bind approval to setup/plan version, exact order arguments and expiry; make it single-use.

Acceptance: budget or quantity edits invalidate approval; stale quote/earnings changes
block submission; retries cannot reuse approval to create another order. Specify
underlying-price triggers separately from option-premium orders. Manual execution
needs an explicit fill/reconciliation workflow, never an implied broker fill.

### 14 — Journal and comparisons (journal)

Depends on 07, 12–13. Add immutable point-in-time opportunity/plan/approval/order/fill
records with version identifiers, costs and reasons for missing/rejected outcomes.
Compare rules-only and AI decisions across all eligible triggers (including AI passes),
then separately compare execution on matched timing to isolate human approval delay.
Compare shares/options and grade groups at equal risk for analysis, separate from actual
user sizing. Keep modeled and broker fills distinct; missing fills are not zero-cost fills.

Acceptance: an AI-rejected opportunity remains in selection analysis; future information
cannot enter a saved decision; multi-leg simulations do not assume simultaneous favorable
fills; hypothetical comparisons do not imply unlimited capital. Plan B: store events,
withhold comparisons whose required data are missing.

### 15 — Paper execution (approve, manage)

Depends on 13–14. Add a paper-only Alpaca adapter and order state machine. Recheck
approval, prices, tradability, earnings, account state and buying power immediately
before submitting. Use stable client IDs and reconcile unknown outcomes before retrying.
Use bounded limit-order repricing; do not promise fills. Keep live credentials/routes disabled.

Acceptance: accepted/rejected/timeout orders, duplicate retry, partial fill, cancel/replace
race and broker disconnect; then verify actual paper API behavior. Plan B: stop new
submission and reconcile broker state. Paper-adapter tests alone do not authorize an
unattended desk; position management must pass Step 16.

### 16 — Management and first complete path (manage, journal)

Depends on 15. Add structured stop/target/time/earnings exits, partial quantities,
restart reconstruction, manual fill import and broker reconciliation. Model assignment,
exercise and expiry where supported; fetch required account events, including events
not delivered by the streaming order feed. Prevent orphan orders and double closes.

Acceptance: one breakout-in-shares path from detection through approved paper entry,
exit and journal; loss-limit halt still permits required exit management; rejected exit,
no-fill limit order, restart and external/manual position changes produce visible recovery
actions. Options remain unavailable for unattended operation until their management cases
pass. Plan B: alert Taz, block new entries, and present reconciled positions for action.

### 17 — VCP, cup and Darvas (watch, plan)

Depends on 10, 16. Repair volume/chase enforcement and causal pivots. Resolve rounded
cup versus V-shaped/price-only variants as explicit card decisions, not silent relaxations.
Verify Darvas floor confirmation against the actual cited source passage. Keep all
numeric assumptions visible. Test each setup's successful and rejected full-path handoff.

### 18 — Kell and Luk (watch, plan)

Depends on 17. Implement Kell's intended hourly entry, defining whether an intrabar
cross or a completed-hour condition is required. Do not add the full Trend Template
to a card unless approved. Implement a fresh Luk dip/reclaim event with an explicit
structural stop anchor. Test timeframe boundaries, old versus fresh reclaims and invalid data.

### 19 — Holy Grail, Weinstein and Connors (watch, plan)

Depends on 18. Remove unrelated opening-range requirements from Holy Grail touch-bar
entries; mirror short rules correctly. Use actual weekly structure for Weinstein or
clearly label/approve an approximation. Preserve a timestamped developing daily bar
for the near-close RSI(2) decision; do not overwrite it with the eventual closing bar.
Complete the all-ten-card requirement-to-code-to-example matrix and integration checks.

### 20 — Operations and actual data checks (watch, manage, journal)

Depends on 06, 16, 19. Targets: scanner/log storage, `ops/imac` and recovery tooling.
Verify actual Webull timestamp/completion/adjustment/delay/earnings behavior read-only.
Use calendar-aware schedules, one active runner, atomic durable writes, backups and
restore/replay rules. Distinguish valid empty results from missing prep and feed failures.
External heartbeat/recovery must not create a second active order runner.

Acceptance: missed slot, duplicate launch, machine sleep, early close, corrupted state,
failed backup/restore and dependency outage are detectable. Configure/activate a scheduled
runner only in the explicitly selected environment, with required keys provided securely.
Plan B: supervised recovery; replacement host only after the prior runner is fenced off.

### 21 — Forward observation (journal)

Depends on 20. Preserve the existing ten-trading-day scanner operational acceptance
including Friday runs. Define expected slots using the calendar; a relevant failure
requires repair and a new documented observation period. This is reliability evidence,
not proof of profitability. Independent offline development need not wait for this period.

Preserve the existing 30+ completed paper-trade / positive-after-costs review policy
before live consideration, without portraying it as statistically established edge.
Report drawdown, losing streaks, best-trade dependence, missed/late signals, costs,
fill limitations, all-trigger AI-versus-rules results and remaining setup-specific gaps.
No forced trades to fill a quota. Open trades and fabricated fills do not count as completed.

### 22 — Small live pilot (approve, manage, journal)

Depends on 21 and explicit activation authorization. Recheck current broker/account
permissions, trading restrictions and supported instruments against official sources.
Confirm the route: supported automated broker or explicit manual workflow; Webull
remains data-only. Taz selects risk and approves each entry. No autonomous expansion
of size, structures, setups or limits after initial results.

Acceptance: controls and recovery demonstrated, selected pilot scope recorded and
Taz authorizes activation. This future step is not authorized merely by finishing repairs.
Plan B: return to paper and reconcile any existing positions.

## Decisions that remain open (resolve before the dependent step)

| Topic | Required resolution | Step |
| --- | --- | --- |
| EP opening-range alternatives (resolved) | Taz selected either completed 15-minute or 60-minute opening-range breakout throughout day one, subject to other checks. | 03 |
| Supported option structures (resolved) | Include calendars and iron condors initially; assignment/expiry handling before executable support. | 04, 11, 16 |
| HALF regime (resolved) | Advisory; user-selected budget is unchanged. | 05, 13 |
| Other account/market conditions (resolved) | Daily/weekly/drawdown and bearish-market conditions become warnings the user can override at approval. | 05 |
| Account loss accounting (defined) | Net liquidation including realized/unrealized/fees, excluding external flows; explicit ET date and Monday week. Adapter must supply reconciled baselines. Manual stop/reset is separately audited. | 05, 15–16 |
| Signal re-entry (resolved) | Taz accepted the recommendation: newly qualified setups after failure/closure may produce new tickets, each requiring fresh approval. No blanket one-trigger-per-day cap. | 07 |
| Relative volume/chase details | Same-time baseline, session handling, lookback and per-card thresholds; distinguish user policy from assumptions. | 10, 17 |
| Single-contract exits | Approved exit variant or shares alternative; never increase size merely to enable fractions. | 11 |
| Source-dependent setup changes | Cup variants, Darvas floor, Kell filter/timeframe and Luk stop anchor. | 17–19 |

These are explicit specification tasks, not permission to guess. They do not block
Step 02. Ask Taz only when the existing sources and prior decisions cannot resolve them.

## Change record template

Create `docs/checkpoints/NN-short-name.md` for each step:

1. Status, trader-day function, implementer/reviewer, exact base commit and prerequisite.
2. Requirement IDs; sources/policy/assumption labels; change being made and why.
3. Existing/new files, upstream producers, downstream consumers and state migration.
4. Valid, invalid, boundary, missing-data and interaction examples with expected outcomes.
5. Decisions resolved; unresolved requirements that block this step.
6. Actual changes and commands/results; distinguish local, live-data and operational evidence.
7. Remaining limitations, fallback and rollback (include state/data compatibility).
8. Completion checklist and next step. Continue only under the current sequential authorization; pause for unresolved policy or failed acceptance.

## Primary references and planning inputs

- User decisions recorded above and this conversation's selected repair plan.
- Blueprint: https://claude.ai/artifact/PttWVbAJ9tVDpRFKnhNewg
- Opus repair plan: https://claude.ai/artifact/FkaoE4sBq5HUdos43LXEXQ
- Existing risk PR: https://github.com/Tahmad888/Trading-Agent/pull/1
- Qullamaggie setups: https://qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/
- Qullamaggie EP guide: https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/

The linked trader guides support source fidelity, not claims of future profitability.
Reverify current provider/broker documentation in the step that depends on it. This
tracker does not assert that every source passage or live endpoint has already been checked.
