"""Bounded historical acceptance probe for the Alpaca SIP volume producer.

    python -m desk.alpaca_probe --symbols NVDA,SPY,QQQ,AAPL --entry-session 2026-10-02 \
        --daily-start 2026-07-01 --out DIR [--cache PATH] [--adjustment split|raw]

Two batch queries through ``desk.alpaca_volume`` (D: daily-start ET midnight to one
second before the entry session's ET midnight; RTH30: the entry session's first 30
minutes), at most six HTTP requests including pagination. Real credentials and the
real receipt clock; the replay entry session is historical. Stops on an
authentication, entitlement or rate-limit failure. Writes sanitized evidence only:
no keys, request headers or provider error bodies. Nothing is activated.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sys

import pandas as pd

from desk.alpaca_volume import (AlpacaVolumeClient, AlpacaVolumeError, BarRequest, EPVolumeComponent,
                                MAPPING, POLICY, RequestBudget, VolumeCache, evaluate, prior_sessions)
from desk.calendar import ET, session, trading_day

MAX_REQUESTS = 6


def windows(entry: date, daily_start: date, symbols: tuple[str, ...], adjustment: str):
    if not trading_day(entry):
        raise AlpacaVolumeError("ENTRY_NOT_A_SESSION")
    if daily_start > prior_sessions(entry)[0]:
        raise AlpacaVolumeError("DAILY_START_AFTER_FIRST_REQUIRED_SESSION")
    midnight = pd.Timestamp(entry).tz_localize(ET)
    opened = session(entry)[0]
    daily = BarRequest(symbols=symbols, timeframe="1Day", adjustment=adjustment,
                       start=pd.Timestamp(daily_start).tz_localize(ET).to_pydatetime(),
                       end=(midnight - timedelta(seconds=1)).to_pydatetime())
    rth = BarRequest(symbols=symbols, timeframe="15Min", adjustment=adjustment, start=opened.to_pydatetime(),
                     end=(opened + timedelta(minutes=30) - timedelta(seconds=1)).to_pydatetime())
    return daily, rth


def run(symbols, entry, daily_start, out: Path, *, client: AlpacaVolumeClient, budget: RequestBudget,
        adjustment="split") -> dict:
    daily_req, rth_req = windows(entry, daily_start, symbols, adjustment)
    results, labels = [], ("D", "RTH30")
    for label, req in zip(labels, (daily_req, rth_req)):
        results.append(client.fetch(req))
        if client.stopped:
            break  # no further request after an auth/entitlement/rate-limit stop
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    report = {"purpose": "G5 checkpoint 1 historical SIP volume acceptance (watch step); "
                         "not a current-session, real-time entitlement or setup check",
              "entry_session": str(entry), "symbols": list(symbols), "adjustment": adjustment,
              "mapping_provenance": MAPPING, "comparison_policy": POLICY.id,
              "numerator_definition": POLICY.numerator, "denominator_definition": POLICY.denominator,
              "request_budget": budget.limit, "http_requests": budget.used, "stopped": client.stopped,
              "requests": {}, "tickers": {}}
    for label, result in zip(labels, results):
        report["requests"][label] = result.report()
        for n, body in enumerate(result.raw_pages, 1):
            (raw_dir / f"{label}_page{n}.json").write_bytes(body)
    if len(results) == 2:
        for symbol, outcome in evaluate(entry, *results).items():
            report["tickers"][symbol] = (outcome.report() if isinstance(outcome, EPVolumeComponent)
                                         else {"status": "UNAVAILABLE", "code": outcome})
    else:
        report["tickers"] = {s: {"status": "UNAVAILABLE", "code": "RUN_STOPPED"} for s in symbols}
    for value in report["tickers"].values():
        value.setdefault("status", "AVAILABLE")
    (out / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    return report


def main(argv=None, env=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--entry-session", required=True, type=date.fromisoformat)
    parser.add_argument("--daily-start", required=True, type=date.fromisoformat)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--adjustment", choices=("split", "raw"), default="split")
    args = parser.parse_args(argv)
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    budget = RequestBudget(MAX_REQUESTS)
    try:
        client = AlpacaVolumeClient.from_env(env, budget=budget,
                                             cache=VolumeCache(args.cache) if args.cache else None)
        report = run(symbols, args.entry_session, args.daily_start, args.out, client=client, budget=budget,
                     adjustment=args.adjustment)
    except AlpacaVolumeError as exc:
        print(json.dumps({"status": "NOT_RUN", "code": exc.code}))
        return 2
    summary = {"http_requests": report["http_requests"], "stopped": report["stopped"],
               "requests": {k: {"status": v["status"], "error": v["error"], "pages": v["http_requests"]}
                            for k, v in report["requests"].items()},
               "tickers": {s: ({"ratio": v["ratio_display"], "threshold_met": v["threshold_met"]}
                               if v["status"] == "AVAILABLE" else {"code": v["code"]})
                           for s, v in report["tickers"].items()}}
    print(json.dumps(summary, indent=2))
    if report["stopped"]:
        return 2
    return 0 if all(v["status"] == "AVAILABLE" for v in report["tickers"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
