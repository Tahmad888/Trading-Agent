# Trading-Agent

A retail-style trading desk where AI does the heavy lifting: it watches Taz's tickers, reads the charts top-down, writes a plan in plain words and sends it for approval. Code owns risk, sizing and orders, and Taz approves every entry.

The original implementation follows Trading Desk Blueprint v2.3, with v2.4 risk work
in PR #1. Current repair work follows [the numbered repair plan](docs/REPAIR_PLAN.md)
and [Step 01's verified baseline](docs/checkpoints/01-baseline.md). `CLAUDE.md` holds
the six rules and standing constraints; `AGENTS.md` defines the checkpoint workflow.
Steps 01–02 are complete locally. PR #1 is integrated with user-selected dollar
risk, cost-inclusive sizing and no fixed $100 ceiling or grade-dollar assignment.
See [Step 02 evidence](docs/checkpoints/02-user-risk.md). Step 03 removes the 11:00 EP rule.

## Status

Risk engine: Taz supplies each trade’s dollar budget. Code sizes whole units within that budget, including a full estimated cost reserve, and retains account loss halts, liquidity and buying-power checks. No count caps or grade-based sizing. Option identity/loss verification and broker/account reconciliation are still later repair steps; passing unit tests does not make this live-ready.

Phase 0, step 3 (in progress): bars and indicators. `desk.bars` parses and checks Webull bars and fails closed on bad or missing data; `desk.indicators` computes the feature pack with TA-Lib 0.8.1 on TradingView's default settings, tested against TradingView's published formulas. `python -m desk.screen_check bars.json` prints a ticker's latest daily values to compare with a TradingView screen. `desk.webull` is the desk's own read-only Webull market-data connection (signed HTTPS, keys from environment variables, fail closed).

Phase 0, step 4 (in progress): the scanner. `desk.playbook.filters` has Minervini's Trend Template and the SPY/QQQ market filter; `desk.playbook.cards` holds the 10 wave-1 setup cards with every number labelled; `desk.playbook.triggers` checks each setup on daily bars; `desk.watchlist` ranks the leader scan and builds the watchlist; `desk.scanner` runs on a schedule and logs every scan for the Friday funnel. It runs on the iMac: setup steps in `ops/imac/README.md`.

## Run the tests

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```
