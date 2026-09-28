# Trading-Agent

A retail-style trading desk where AI does the heavy lifting: it watches Taz's tickers, reads the charts top-down, writes a plan in plain words and sends it for approval. Code owns risk, sizing and orders, and Taz approves every entry.

The plan is Trading Desk Blueprint v2.3. `CLAUDE.md` holds its six rules and the standing constraints.

## Status

Phase 0, step 2: the risk engine and order contracts, carried over from the earlier desk and updated for v2.3 (tiered sizing, the 2% worst-case cap, open interest and 14-day checks, no perps).

## Run the tests

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```
