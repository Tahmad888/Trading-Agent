"""Bounded timestamped REST stock/option quote observations; no orders or approvals."""
from __future__ import annotations

import argparse
from datetime import timedelta
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import time

from desk.quote_measure import write_report
from desk.snapshot_quotes import normalize_batch
from desk.tastytrade_quotes import QuoteService, QuoteUnavailable, aware, canonical, session_label
from desk.tastytrade_transport import Credentials, ReadClient, select_pair, utcnow

STOPS = {"REST_HTTP_401", "REST_HTTP_403", "REST_HTTP_429", "REST_REQUEST_BUDGET"}


def diagnostic(client, equities, *, option_underlying=None, option_strike=None, rounds=2,
               interval_seconds=5, clock=utcnow, sleep=time.sleep):
    from desk.risk import RiskLimits
    max_age = RiskLimits().max_quote_age  # existing policy, not a new threshold
    report = dict(purpose="read-only REST snapshot observations; no signal/order activation",
                  status="UNAVAILABLE", environment=client.environment, identity_issues={}, rounds=[],
                  option_selection={"status": "NOT_REQUESTED"}, current=[], current_failures={},
                  LIVE_TIMING="NOT_TESTED", coverage="NOT_ATTESTED", mapping="NOT_REVIEWED_BY_DIAGNOSTIC",
                  decision_eligibility="NOT_EVALUATED", requests=client.requests,
                  note="Earlier rounds are history only. Quote-update time is not a bid/ask side-change time.")
    service = QuoteService(environment=client.environment)  # identity registry only, never connected
    identities = []
    try:
        if (not isinstance(equities, (list, tuple)) or not 1 <= len(equities) <= 10
                or type(rounds) is not int or not 1 <= rounds <= 3
                or isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float))
                or not math.isfinite(interval_seconds) or not 0 <= interval_seconds <= 30):
            raise QuoteUnavailable("SNAPSHOT_PROBE_ARGUMENTS_INVALID")
        names = [canonical(s, "Equity") for s in equities]
        if len(set(names)) != len(names):
            raise QuoteUnavailable("SNAPSHOT_PROBE_ARGUMENTS_INVALID")
        underlying = canonical(option_underlying, "Equity") if option_underlying else None
        if option_strike is not None and (not isinstance(option_strike, Decimal)
                or not option_strike.is_finite() or option_strike <= 0 or underlying is None):
            raise QuoteUnavailable("SNAPSHOT_PROBE_ARGUMENTS_INVALID")
        if underlying and underlying not in names:
            names.append(underlying)
        started = aware(clock())
        report.update(started_at=started.isoformat(), requested_symbols=names)
        for symbol in names:
            try:
                identities.append(client.resolve(symbol, "Equity", service))
            except QuoteUnavailable as exc:
                report["identity_issues"][symbol] = str(exc)
                if str(exc) in STOPS or str(exc).startswith("REST_") and str(exc) != "REST_HTTP_404":
                    raise
        if not identities:
            raise QuoteUnavailable("NO_ACCEPTED_IDENTITIES")

        def observe():
            sent = aware(clock())
            rows = client.market_quotes(identities)
            received = aware(clock())
            values, failures, unexpected = normalize_batch(rows, identities, client.environment, sent, received)
            view = dict(request_started_at=sent.isoformat(), received_at=received.isoformat(),
                        checks=[v.view(received, max_age) for v in values.values()], failures=failures,
                        unexpected_row_count=unexpected, current_eligible=False,
                        scope="RTH_OBSERVATIONS_REQUIRE_REVIEW" if session_label(sent) == session_label(received) == "RTH"
                              else "OFFHOURS_SCHEMA_AND_ACCESS_ONLY")
            return values, view

        if underlying:
            report["option_selection"] = {"status": "UNAVAILABLE", "underlying": underlying}
            try:
                if option_strike is None:
                    values, seed = observe()
                    report["option_reference_observation"] = seed
                    ref = values.get(underlying)
                    now = aware(clock())
                    if (ref is None or not timedelta(0) <= now - ref.updated_at <= max_age
                            or ref.halted is True):
                        raise QuoteUnavailable("OPTION_REFERENCE_SNAPSHOT_UNAVAILABLE")
                    target = (ref.bid + ref.ask) / 2
                else:
                    target = None
                selection = select_pair(client.chain_options(underlying), now=clock(),
                                        strike=option_strike, reference=target)
                report["option_selection"].update(selection, status="RESOLVING",
                                                  reference="SNAPSHOT_MIDPOINT" if target else "EXPLICIT_STRIKE")
                accepted = []
                for symbol in (selection["call"], selection["put"]):
                    try:
                        accepted.append(client.resolve(symbol, "Equity Option", service))
                    except QuoteUnavailable as exc:
                        report["identity_issues"][symbol] = str(exc)
                        if str(exc) in STOPS or str(exc).startswith("REST_") and str(exc) != "REST_HTTP_404":
                            raise
                identities.extend(accepted)
                report["option_selection"]["status"] = "RESOLVED" if len(accepted) == 2 else "PARTIAL"
            except QuoteUnavailable as exc:
                report["option_selection"].update(status="UNAVAILABLE", reason=str(exc))
                if str(exc) in STOPS or str(exc).startswith("REST_") and str(exc) != "REST_HTTP_404":
                    raise
        latest = {}
        for index in range(rounds):
            # No cache path: a refresh failure cannot return an earlier quote.
            report["current"], report["current_failures"], latest = [], {}, {}
            if index:
                sleep(interval_seconds)
            latest, view = observe()
            report["rounds"].append(dict(round=index + 1, **view))
            report["current_failures"] = view["failures"]
        finished = aware(clock())
        report["current"] = [v.view(finished, max_age) for v in latest.values()]
        report["status"] = ("PARTIAL_OBSERVATIONS" if report["current_failures"] or report["identity_issues"]
                            or report["rounds"][-1]["unexpected_row_count"]
                            or report["option_selection"]["status"] in {"PARTIAL", "UNAVAILABLE"}
                            else "OBSERVATIONS_ONLY") if latest else "UNAVAILABLE"
        report["LIVE_TIMING"] = ("OBSERVATIONS_REQUIRE_REVIEW" if session_label(started) == session_label(finished) == "RTH"
                                  else "NOT_TESTED_MARKET_CLOSED")
        report["finished_at"] = finished.isoformat()
    except QuoteUnavailable as exc:
        report.update(status="UNAVAILABLE", reason=str(exc), current=[], current_failures={},
                      stopped_on_error=True)
    report["requests"] = client.requests
    report["identity_capture"] = [i.capture() for i in identities]
    return report


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("production", "sandbox"), required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--option-underlying")
    parser.add_argument("--option-strike", type=Decimal,
                        help="Listed strike nearest this value; otherwise use a fresh underlying snapshot midpoint")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--max-requests", type=int, default=12)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        client = ReadClient(Credentials(env.get("TASTYTRADE_CLIENT_SECRET", ""), env.get("TASTYTRADE_REFRESH_TOKEN", "")),
                            environment=args.environment, max_requests=args.max_requests)
        report = diagnostic(client, args.symbols, option_underlying=args.option_underlying,
                            option_strike=args.option_strike, rounds=args.rounds, interval_seconds=args.interval_seconds)
    except QuoteUnavailable as exc:
        report = dict(purpose="read-only REST snapshot observations; no signal/order activation", status="UNAVAILABLE",
                      reason=str(exc), requests=0, decision_eligibility="NOT_EVALUATED", LIVE_TIMING="NOT_TESTED")
    try:
        report["commit"] = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True,
                                           text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        report["commit"] = "UNKNOWN"
    report["python"] = platform.python_version()
    report = write_report(report, args.output, env)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "OBSERVATIONS_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
