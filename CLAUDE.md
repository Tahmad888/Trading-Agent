# Trading-Agent

## Active repair work

Current priority (2026-10-01): gap repairs G1–G5 in `docs/GAP_REPAIR_PLAN.md`,
then resume unfinished Step 09. Read `docs/checkpoints/G5-combined-verification.md`
for the active checkpoint and `docs/G5_ACCEPTANCE.md` for the matrix. G5 acceptance
is pending Astra's check of the policy-snapshot commit after `5e24e02` (her two re-audit
timing defects of `ee4dc90` fixed and closed by her; her `5e24e02` finding that a relaxed
threshold could clear a warning is fixed by binding the warning policy; Taz's 2026-10-02 decisions on price increments,
the 10% account-warning band and the EP chase from the frozen opening-range high are
implemented; Massive dividend evidence and `SSL_CERT_FILE` wait on the iMac); G4's
five audit defects were fixed in `d529880`. G3 automatic price/history handling is implemented and
locally verified; the read-only iMac probe in `docs/VENDOR_BASIS.md` is still pending.
It does not establish universal action/volume coverage or activate the scanner. Later user decisions supersede maximum-loss-only
option sizing proposals: show full exposure and supported stop-loss estimates
separately; Taz approves exact quantity/exposure on the final ticket. Read `docs/RISK_TERMS.md`: schema 3 requires an independently resolved event and
explicit sizing mode. G4 (Claude implements, Astra audits, per Taz 2026-10-02)
adds `desk.tickets`: local ticket display, exact budget re-entry, exact warning
acknowledgement, revocation, expiry and single-use consumption with no orders. Read
`docs/TICKETS.md` and `docs/checkpoints/G4-ticket-approval.md`. Astra's G3a follow-up
(targeted rebuilds, opt-in batch action evidence; `docs/G3_FOLLOWUP.md`) is integrated
and stays operationally open. Step 09 resumes only after the audits and Taz's go-ahead. No hardcoded dollar cap or silent over-budget execution. Step 09/10 and runner activation are
not completed by these repairs.

G5a (2026-10-04, Taz's volume/discovery handoff): checkpoint 1 (opt-in read-only
Alpaca SIP volume producer and probe, `docs/ALPACA_VOLUME.md`,
`docs/checkpoints/G5a-alpaca-volume.md`) closed by Astra on `df99472`. Checkpoint 2
(identity, volume consumers, signal/ticket gates, PARTIAL discovery publication;
`docs/checkpoints/G5a-cp2-volume-consumers.md`) is implemented, opt-in via
`DESK_ALPACA_VOLUME_CACHE`. Astra's audit of `b5dab2c` found F1 (identity health not
persisted) and F2 (final guard did not hold the volume/identity store), repaired in
`0d8838c`. Her re-audit found R1 (an unpersistable identity outcome left an old pin
eligible) and R2 (price-only tickets waited on the volume guard); both repaired in `ba78ba0`,
which Astra accepted for CP2. Checkpoint 3 (scoped discovery history; Friday build only
asks for its 260 sessions, `src/desk/history_scope.py`, `docs/checkpoints/G5a-cp3-scoped-history.md`)
is implemented; Astra's audit of `33256b3` found F1 (clipped coverage published a healthy
EMPTY), F2 (boolean OHLCV accepted as 1.0) and D1 (excluded duplicates not stored), repaired
and pending her re-audit. Every other consumer keeps 1000 strict rows.
Historical access does not prove real-time entitlement.

Read `AGENTS.md` and `docs/REPAIR_PLAN.md` before repair implementation. Verify and
checkpoint each numbered step before advancing under Taz's current authorization.
Taz chooses each trade's dollar risk budget, with no hard $100 ceiling or automatic
grade-dollar assignments. Step 02 implements that risk policy; Step 03 removes the
11:00 ET EP condition and permits either completed opening range throughout day one. Step 04 verifies option identity and computes strategy loss,
including calendars and iron condors. HALF must be advisory without automatically
changing the selected budget. Step 05 makes account-loss/drawdown and bearish-market
conditions user-overridable warnings, persists account/exposure/manual-stop state,
and checks a trusted setup registry. No user approval UI/order adapter exists yet.
Current status: Step 06 is closed for the scoped NVDA/SPY sandbox checks and
iMac verification; read docs/evidence/step06-integrated-results.md. Step 07 is
complete: read docs/checkpoints/07-signal-lifecycle.md and docs/SIGNAL_LIFECYCLE.md.
The scanner persists signal lifecycle separately from approvals/orders.
Step 08 closed with 621 strict tests on both local Python versions. Discovery
now uses security metadata, distinct bearish sources, per-setup history and next-scan
user additions. Read docs/checkpoints/08-discovery.md and docs/DISCOVERY.md.
Step 08 provider verification is complete: NVDA, SPY and BRK.B metadata/D/M15
identities passed on the iMac sandbox connection, 2026-10-01. Read
docs/checkpoints/08a-webull-symbols.md and docs/evidence/step08-metadata-imac.md.
Step 09e adds opt-in bounded retrieval/cache, original SEC catalyst candidates,
content-bound reviews and scanner/revalidation integration. Read
`docs/checkpoints/09e-refresh-catalysts.md` and `docs/EARNINGS_REFRESH.md`.
NVDA/AAPL/MSFT historical iMac mappings are accepted for their supplied scope;
current live refresh, real catalyst review and final integrated host acceptance
remain open. No scheduler/runtime activation; no Step 10 work.

Step 09 is REOPENED / IN PROGRESS. Earlier closure after 688 passing tests and the
NVDA iMac replay was premature: automatic reported results, upcoming dates,
catalyst retrieval/review, refresh/integration and actual-host acceptance remain.
Read docs/checkpoints/09b-automatic-sources.md for research and exit criteria.
The multi-company source collector passed 715 strict tests; the iMac report
confirmed SEC access and Webull alert/calendar data for NVDA/AAPL/MSFT, with
empty Webull income. Follow-up 09c implements the eight interpretation repairs;
read docs/checkpoints/09c-source-normalization.md and docs/EARNINGS_NORMALIZATION.md.
NVDA/AAPL full-archive iMac mapping and fundamental evaluation passed. MSFT
needs explicit issuer Q4 evidence; follow-up 09d supplies the reviewed fallback
and passes 767 strict tests on both local Python versions. Taz subsequently supplied a passing merged MSFT
iMac replay for its dated historical scope. Read docs/checkpoints/09d-msft-issuer-fallback.md.
These checks do not establish automatic coverage or complete Step 09 acceptance.
Do not call a source probe or one reviewed file complete automatic coverage.
Step 10 has not started. Earlier descriptions retain historical status.
Step 06 adds bounded exchange-calendar timing, completed-bar checks and explicit
provenance; live Webull semantic acceptance remains Step 20. Taz authorized pushing
verified checkpoints to the dedicated repair branch, without an implicit merge.
The tracker records actual completion, not just approved intent.
Step 06 follow-up C replaces label-only price compatibility with structured action
coverage and isolates unknown volume definitions. Read its checkpoint and
`docs/DATA_BASIS_ACCEPTANCE.md` before provider work. No live profile is enabled;
real action-source, daily-volume and RTH-timing acceptance remain open. Step 07 is
paused until Taz's six Step 06 verification items are resolved.
Follow-up D repairs sandbox pacing and HTTP route diagnostics and adds a partial-
evidence probe plus reviewed action-term revisions. Read checkpoint 06d before
provider checks. Keep EP's native daily baseline after the research review; no
real profile or corporate-action source adapter is enabled by these repairs.
Follow-up E adds `desk.alphavantage_check` and the optional read-only observation
client. It stops on errors/rate limits and never publishes PriceBasis coverage.
Read checkpoint 06e before direct checks; no raw errors/URLs or credentials in
reports, no repeated calls after a limit, and no assumption about quota reset.

Follow-up F adds `desk.action_import`, a persistent reviewed action ledger and an
opt-in scanner source (`DESK_ACTION_LEDGER`, `DESK_ACTION_CHANNELS`). Read checkpoint
06f. The reported four action-source checks and RTH timing sample pass. Zero-dollar
dividends are logged but ignored economically under Taz's explicit instruction.
Real mapping/channel configuration and price/volume acceptance remain open; do not
promote fictional test reviews or activate the runner.

Follow-up G adds the explicit native-daily/RTH volume source-pair policy, bounded
post-split volume windows and `desk.data_acceptance`. Read checkpoint 06g and
`docs/evidence/step06-native-channels.md`. The checked NVDA/SPY sandbox config is
scoped to 2026-10-01. Actual dated imports and integrated checks passed for NVDA
in Claude cloud and SPY on the iMac; Taz confirmed 508 tests in 5.10s on the iMac. Do not claim local tests or a configured key activated the
desk. The original 50-day/0.5 EP rule is unchanged.

Read the six rules before doing anything. They come from Trading Desk Blueprint v2.4
(29 Sep 2026), amended by Taz's later decisions in the repair plan:
https://claude.ai/artifact/PttWVbAJ9tVDpRFKnhNewg
(source in the project folder at `research/ai-trading/outside-review/trading-desk-blueprint.md`).

## The six rules

1. **The goal comes first.** Retail trading with AI assistance: watch tickers, analyze them, do technical analysis, and take the trade when there's potential, with Taz approving every entry.
2. **Every task names its step.** Each task has to say which step of the trader's day it serves: watch, analyze, plan, approve, manage or journal. Anything that serves none of them, such as multi-year backtests, statistical gates or research programs, waits until Taz asks for it.
3. **"Researched" has a clear meaning.** Every rule and number carries one of three labels. *Sourced* links to the trader, book or paper it came from. *Checked* means verified on our live data or journal. *Assumption* means the journal will check it. No backtest is needed.
4. **Tickets in plain words.** Each ticket gives the ticker, the setup's name, what each timeframe shows, the entry, stop and target, which option and why, the dollars at risk, and the next earnings date. If Taz can't follow a ticket, that's a bug to fix.
5. **A weekly check.** Every Friday a short note covers what was built, which step it served, paper results by setup, and the week's funnel: scans run out of scans scheduled, setups triggered, plans logged, tickets sent, and for each blocked plan the check that blocked it (a risk limit, a loss-limit halt or earnings). It raises a drift flag when any of these is true:
   - While building, a week passed with no progress on the current step.
   - Once the scanner is live, a scheduled scan was missed, or a trading day logged no plans or no-trade notes for the watchlist.
   - Setups triggered but no ticket reached Taz, and the block wasn't one of the written risk limits.
   - No setup triggered on any watchlist ticker for 2 weeks in a row, which means the scanner needs checking (Assumption on the 2 weeks).

   A week with zero tickets because nothing triggered, or because a loss-limit halt was on, is reported, not flagged. A flag is fixed by repairing the process, never by loosening a setup's rules. Setup rules change only when Taz approves it.
   *Superseded wording (D05, 2026-09-30; still in the external blueprint):* daily/weekly loss and drawdown limits are warnings Taz may acknowledge per ticket, not halts. Only the explicit manual stop blocks. Report a manual-stop block or a declined warning in their place.
6. **When history is allowed.** Indicators need warm-up bars. The desk loads about 1,000 daily bars so a 200-day average and the other indicators match Taz's screen. That's a data need, not a test, and it doesn't reopen backtesting.

## Standing constraints

- Keys live in environment variables only. Never put a key in chat, memory, code or a commit. `.env.example` lists the names with empty values.
- Webull is used for market data only. Its order tools are never called or enabled (`.claude/settings.json` denies them).
- No TradingView data, scraping or browser automation in any decision. TradingView is Taz's own screen.
- Deterministic code owns risk, sizing and orders. No model ever touches `src/desk/risk.py`'s decisions, and the risk layer can only shrink or reject a trade.
- Limit orders only. Fail closed: a timeout, a missing field or stale data means no trade.
- Model choice is per role, from config. No provider is hard-wired.
- Every new or unproven component gets a written Plan B next to it.

## Layout

- `src/desk/contracts.py`: typed hand-offs (trade proposal, legs, risk decision, schema-2 approval record).
- `src/desk/tickets.py`: G4 local ticket preparation, display, approval, revocation, history and single-use consumption (`python -m desk.tickets`; no broker action).
- `src/desk/risk.py`: the risk engine, with limits from blueprint section 10: user-selected dollar risk per ticket, including estimated costs, with no grade-dollar mapping or hard $100 ceiling, user-overridable dollar-loss warnings (not halts), the blocking manual stop, quote-age and trading-status checks. No caps on trade or position counts.
- `src/desk/alpaca_volume.py`, `src/desk/alpaca_probe.py`: G5a opt-in Alpaca SIP volume evidence and its bounded probe; `alpaca_assets.py` (identity pins and identity health) and `alpaca_source.py` (consumers' provider and the final guard) wire it in (prices stay Webull).
- `src/desk/bars.py`: parses and checks price bars; bad, short or stale bars raise `BarDataError` (no trade).
- `src/desk/history_scope.py`: G5a CP3 typed discovery scope (260 sessions, cannot be shortened) and pre-parse row classification; only the Friday leader build uses it.
- `src/desk/indicators.py`: the feature pack on TA-Lib 0.8.1, matched to TradingView; `tests/tv_reference.py` holds the Pine formulas it's tested against.
- `src/desk/screen_check.py`: prints a ticker's latest daily values to compare with Taz's screen.
- `tests/`: run with `pip install -e .[dev]` then `pytest`.
