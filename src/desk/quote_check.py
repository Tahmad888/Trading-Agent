"""Explicit read-only quote diagnostic. No scan, scheduler, ticket or order use."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
import platform
from pathlib import Path
import subprocess

from desk.tastytrade_quotes import QuoteService, QuoteUnavailable, session_label
from desk.tastytrade_transport import Credentials, ReadClient, capture, utcnow


def diagnostic(client, service, equities, *, option_underlying=None, option_strike=None,
               seconds=30, reconnects=1, capture_fn=capture, clock=utcnow):
    started = clock()
    issues = {}
    # Denial/rate-limit/budget errors stop this run; symbol identity/404 faults isolate.
    stop_codes = {"REST_HTTP_401", "REST_HTTP_403", "REST_HTTP_429", "REST_REQUEST_BUDGET"}
    try:
        for symbol in equities:
            try:
                client.resolve(symbol, "Equity", service)
            except QuoteUnavailable as exc:
                issues[symbol] = str(exc)
                if str(exc) in stop_codes:
                    raise
        if option_underlying:
            call, put = client.chain_pair(option_underlying, strike=option_strike)
            for symbol in (call, put):
                try:
                    client.resolve(symbol, "Equity Option", service)
                except QuoteUnavailable as exc:
                    issues[symbol] = str(exc)
                    if str(exc) in stop_codes:
                        raise
        if not service.identities:
            raise QuoteUnavailable("NO_ACCEPTED_IDENTITIES")
        result = capture_fn(client, service, seconds=seconds, reconnects=reconnects)
    except QuoteUnavailable as exc:
        service.disconnect(str(exc))
        result = dict(stop_reason=str(exc), observations=[], requests=client.requests,
                      attempts=0, connected_after_capture=False)
    finished = clock()
    in_session = session_label(started) == session_label(finished) == "RTH"
    observations = result["observations"]
    latest = observations[-1] if observations else None
    groups = {}
    for kind, label in (("Equity", "stocks"), ("Equity Option", "options")):
        rows = [row for row in (latest or {}).get("checks", []) if row["kind"] == kind]
        groups[label] = dict(checks=rows, timing_status=("NOT_TESTED_MARKET_CLOSED" if not in_session else
                             "OBSERVATIONS_REQUIRE_REVIEW" if rows else "NOT_TESTED_NO_OBSERVATIONS"))
    return dict(purpose="read-only quote observations; no signal/order activation",
                environment=client.environment, started_at=started.isoformat(), finished_at=finished.isoformat(),
                LIVE_TIMING="OBSERVATIONS_REQUIRE_REVIEW" if in_session else "NOT_TESTED_MARKET_CLOSED",
                status=("OBSERVATIONS_ONLY" if observations and result["stop_reason"] == "CAPTURE_COMPLETE"
                        else "UNAVAILABLE"), identity_issues=issues,
                stocks=groups["stocks"], options=groups["options"], capture=result,
                missing_runtime_inputs=["broker account/risk snapshots", "market regime",
                                       "option ContractBook deliverables/price increments", "option open interest",
                                       "immediate consolidated intraday volume"],
                source_roles=dict(current_quotes="tastytrade-dxlink", historical_prices="Webull unchanged",
                                  decision_volume="Alpaca SIP unchanged"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--environment", choices=("production", "sandbox"), required=True)
    parser.add_argument("--option-underlying")
    parser.add_argument("--option-strike", type=Decimal)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--reconnects", type=int, default=1)
    parser.add_argument("--max-requests", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= len(args.symbols) <= 10 or len(set(args.symbols)) != len(args.symbols):
        parser.error("Provide 1 to 10 distinct equity symbols")
    if not 1 <= args.seconds <= 600 or not 0 <= args.reconnects <= 2:
        parser.error("Seconds must be 1..600; reconnects 0..2")
    if args.option_strike is not None and (not args.option_strike.is_finite() or args.option_strike <= 0):
        parser.error("Option strike must be positive and finite")
    try:
        client = ReadClient(Credentials(os.environ.get("TASTYTRADE_CLIENT_SECRET", ""),
                                        os.environ.get("TASTYTRADE_REFRESH_TOKEN", "")),
                            environment=args.environment, max_requests=args.max_requests)
        service = QuoteService(environment=args.environment)
        report = diagnostic(client, service, args.symbols, option_underlying=args.option_underlying,
                            option_strike=args.option_strike, seconds=args.seconds, reconnects=args.reconnects)
    except QuoteUnavailable as exc:
        report = dict(purpose="read-only quote observations; no signal/order activation", status="UNAVAILABLE",
                      reason=str(exc), LIVE_TIMING="NOT_TESTED", requests=0)
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        clean = not subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                   capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit, clean = "UNKNOWN", False
    report["commit"] = commit
    report["tracked_working_tree_clean"] = clean
    report["python"] = platform.python_version()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "OBSERVATIONS_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
