# Trading-Agent

## Active repair work

Read `AGENTS.md` and `docs/REPAIR_PLAN.md` before repair implementation. Verify and
checkpoint each numbered step before advancing under Taz's current authorization.
Taz chooses each trade's dollar risk budget, with no hard $100 ceiling or automatic
grade-dollar assignments. Step 02 implements that risk policy; Step 03 removes the
11:00 ET EP condition and permits either completed opening range throughout day one. Step 04 verifies option identity and computes strategy loss,
including calendars and iron condors. HALF must be advisory without automatically
changing the selected budget. Step 05 makes account-loss/drawdown and bearish-market
conditions user-overridable warnings, persists account/exposure/manual-stop state,
and checks a trusted setup registry. No user approval UI/order adapter exists yet.
Step 06 adds bounded exchange-calendar timing, completed-bar checks and explicit
provenance; live Webull semantic acceptance remains Step 20. Taz authorized pushing
verified checkpoints to the dedicated repair branch, without an implicit merge.
The tracker records actual completion, not just approved intent.

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

- `src/desk/contracts.py`: typed hand-offs (trade proposal, legs, risk decision, approval record).
- `src/desk/risk.py`: the risk engine, with limits from blueprint section 10: user-selected dollar risk per ticket, including estimated costs, with no grade-dollar mapping or hard $100 ceiling, user-overridable dollar-loss warnings, manual stops, quote-age and trading-status checks. No caps on trade or position counts.
- `src/desk/bars.py`: parses and checks price bars; bad, short or stale bars raise `BarDataError` (no trade).
- `src/desk/indicators.py`: the feature pack on TA-Lib 0.8.1, matched to TradingView; `tests/tv_reference.py` holds the Pine formulas it's tested against.
- `src/desk/screen_check.py`: prints a ticker's latest daily values to compare with Taz's screen.
- `tests/`: run with `pip install -e .[dev]` then `pytest`.
