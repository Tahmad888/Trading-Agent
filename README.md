# Trading-Agent

A retail-style trading desk where AI does the heavy lifting: it watches Taz's tickers, reads the charts top-down, writes a plan in plain words and sends it for approval. Code owns risk, sizing and orders, and Taz approves every entry.

The plan is Trading Desk Blueprint v2.3. `CLAUDE.md` holds its six rules and the standing constraints.

## Status

Phase 0, step 2: the risk engine and order contracts, carried over from the earlier desk and updated for v2.3 (tiered sizing, the 2% worst-case cap, open interest and 14-day checks, no perps).

Phase 0, step 3 (in progress): bars and indicators. `desk.bars` parses and checks Webull bars and fails closed on bad or missing data; `desk.indicators` computes the feature pack with TA-Lib 0.8.1 on TradingView's default settings, tested against TradingView's published formulas. `python -m desk.screen_check bars.json` prints a ticker's latest daily values to compare with a TradingView screen.

## Run the tests

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```
