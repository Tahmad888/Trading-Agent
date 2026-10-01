# Trading-Agent

A retail-style trading desk where AI does the heavy lifting: it watches Taz's tickers, reads the charts top-down, writes a plan in plain words and sends it for approval. Code owns risk, sizing and orders, and Taz approves every entry.

The original implementation follows Trading Desk Blueprint v2.3, with v2.4 risk work
in PR #1. Current repair work follows [the numbered repair plan](docs/REPAIR_PLAN.md)
and [Step 01's verified baseline](docs/checkpoints/01-baseline.md). `CLAUDE.md` holds
the six rules and standing constraints; `AGENTS.md` defines the checkpoint workflow.
Steps 01–06 are complete and verified. PR #1 is integrated with user-selected dollar
risk, cost-inclusive sizing and no fixed $100 ceiling or grade-dollar assignment.
See [Step 02 evidence](docs/checkpoints/02-user-risk.md). The 11:00 EP rule is removed;
either completed opening range can qualify during day one, with no unfinished-bar
look-ahead. See [Step 03 evidence](docs/checkpoints/03-ep-timing.md). See [Step 04 evidence](docs/checkpoints/04-instrument-contracts.md) for contract identity and independent loss calculations. See [Step 05 evidence](docs/checkpoints/05-risk-warnings.md) for user-overridable warnings and durable account state. See [Step 06 evidence](docs/checkpoints/06-data-contracts.md) for calendar, completed bars and data provenance. Step 07 is next.

## Status

Risk engine: [G2 event evidence and sizing](docs/RISK_TERMS.md) require independently resolved entry/stop terms. Taz explicitly chooses stop-budget sizing, optional maximum-loss sizing, or selected quantity for review with exposure warnings. Code reserves estimated costs, and shows account-loss and market warnings for user review while retaining manual stops, liquidity and funding checks. HALF does not alter the chosen budget. No count caps or grade-based sizing. Option identity and loss calculations now require separate contract metadata and reject contradictory structures, including calendars and iron condors. Broker/account reconciliation and execution remain later repair steps; passing unit tests does not make this live-ready.

Phase 0, step 3 (in progress): bars and indicators. `desk.bars` parses and checks Webull bars and fails closed on bad or missing data; `desk.indicators` computes the feature pack with TA-Lib 0.8.1 on TradingView's default settings, tested against TradingView's published formulas. `python -m desk.screen_check bars.json` prints a ticker's latest daily values to compare with a TradingView screen. `desk.webull` is the desk's own read-only Webull market-data connection (signed HTTPS, keys from environment variables, fail closed).

Phase 0, step 4 (in progress): the scanner. `desk.playbook.filters` has Minervini's Trend Template and the SPY/QQQ market filter; `desk.playbook.cards` holds the 10 wave-1 setup cards with every number labelled; `desk.playbook.triggers` checks each setup on daily bars; `desk.watchlist` ranks the leader scan and builds the watchlist; `desk.scanner` runs on a schedule and logs every scan for the Friday funnel. It runs on the iMac: setup steps in `ops/imac/README.md`.

## Run the tests

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Data eligibility after Step 06

The scanner now uses a pinned XNYS session calendar, including early closes and DST.
It requires explicit bar timestamp/session/adjustment/price-scale evidence. Parsing
Webull rows alone does not supply that evidence: until the adapter is given a checked
per-symbol/timeframe profile, raw data remains readable but cannot arm/trigger signals.
Live semantic verification is tracked in Step 20. Existing armed files without price
scale metadata require rebuilding from verified bars; the code does not assume a scale.
