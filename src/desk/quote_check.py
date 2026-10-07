"""Explicit read-only quote diagnostic. No scan, scheduler, ticket or order use."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
import platform
from pathlib import Path
import subprocess

from desk.tastytrade_quotes import CLOCK_NOTE, QuoteService, QuoteUnavailable, canonical, session_label
from desk.tastytrade_transport import Credentials, ReadClient, capture, select_pair, utcnow
from desk.quote_measure import Recorder, load_host_clock, write_report
from desk.diagnostic_pair import load_pair, size_match, validate_pair

STOP_CODES = {"REST_HTTP_401", "REST_HTTP_403", "REST_HTTP_429", "REST_REQUEST_BUDGET"}


def _lag_evidence(rows, in_session):
    """Whether trade-time lag evidence exists and how it compares with the quote policy."""
    trades = [r["trade"] for r in rows if "age_seconds" in r.get("trade", {})]
    if not trades:
        return "UNAVAILABLE"
    if not in_session:
        return "CLOSED_SESSION_AGES_ONLY_NOT_LIVE_EVIDENCE"
    if any(t.get("lag_status") == "WITHIN_QUOTE_POLICY" for t in trades):
        return "AVAILABLE_WITHIN_QUOTE_POLICY"
    return "INCONSISTENT_WITH_QUOTE_POLICY"


def diagnostic(client, service, equities, *, option_underlying=None, option_strike=None, option_median=False,
               seconds=30, reconnects=1, profile=False, capture_fn=capture, clock=utcnow, measure=None,
               host_clock=None, option_pair=None):
    started = clock()
    issues = {}
    option = dict(status="NOT_REQUESTED")
    pair_checks = {}
    pair_input, option_pair = option_pair, None
    # Denial/rate-limit/budget errors stop this run; symbol identity/404 faults isolate.
    try:
        if pair_input is not None:
            option_pair = validate_pair(pair_input, environment=client.environment, now=started)
            if option_strike is not None or option_median:
                raise QuoteUnavailable("OPTION_PAIR_SELECTION_CONFLICT")
        underlying = (canonical(option_underlying, "Equity") if option_underlying else
                      option_pair["underlying"] if option_pair else None)
        if option_pair and underlying != option_pair["underlying"]:
            raise QuoteUnavailable("OPTION_PAIR_IDENTITY_MISMATCH")
        names = list(dict.fromkeys(list(equities) + ([underlying] if underlying else [])))
        for symbol in names:
            try:
                client.resolve(symbol, "Equity", service)
            except QuoteUnavailable as exc:
                issues[symbol] = str(exc)
                if str(exc) in STOP_CODES:
                    raise
        chain = None

        def add_pair(selection):
            identities = []
            for symbol in (selection["call"], selection["put"]):
                try:
                    # resolve registers its result. Keep a contradicted counterpart
                    # outside the real service, so capture cannot subscribe it.
                    registry = QuoteService(environment=service.environment) if option_pair else service
                    identity = client.resolve(symbol, "Equity Option", registry)
                    if option_pair:
                        wanted = {option_pair[right]["symbol"]: option_pair[right]
                                  for right in ("call", "put")}.get(identity.symbol)
                        if wanted is None:
                            raise QuoteUnavailable("OPTION_PAIR_IDENTITY_MISMATCH")
                        metadata = dict(identity.metadata)
                        chain_type = metadata.get("option-chain-type")
                        check = size_match(wanted["contract_size"], metadata.get("shares-per-contract"),
                                           standard_root=chain_type in {None, "Standard"})
                        if chain_type is None and check["status"] == "MATCHED_REPORTED_FIELDS":
                            check.update(status="INCOMPLETE", reason="CHAIN_TYPE_UNAVAILABLE")
                        check["provider_fields"] = {"option-chain-type": chain_type,
                                                    "shares-per-contract": metadata.get("shares-per-contract")}
                        pair_checks[identity.symbol] = check
                        if check["status"] == "CONFLICT":
                            raise QuoteUnavailable("OPTION_PAIR_TERMS_CONFLICT")
                        service.register(identity)
                    identities.append(identity)
                except QuoteUnavailable as exc:
                    issues[symbol] = str(exc)
                    if str(exc) in STOP_CODES:
                        raise
            option.update(selection, status="SUBSCRIBED" if identities else "UNAVAILABLE")
            return identities

        if underlying:
            if option_pair:
                option = dict(status="UNAVAILABLE", underlying=underlying, method="SHARED_EXPLICIT_PAIR",
                              selection_id=option_pair["selection_id"])
                try:
                    chain = client.chain_options(underlying)
                    matches = [row for row in chain if row[0].isoformat() == option_pair["expiry"]
                               and row[1] == Decimal(option_pair["strike"])
                               and canonical(row[2], "Equity Option") == option_pair["call"]["symbol"]
                               and canonical(row[3], "Equity Option") == option_pair["put"]["symbol"]]
                    if len(matches) != 1:
                        raise QuoteUnavailable("OPTION_PAIR_NOT_LISTED" if not matches else "OPTION_PAIR_AMBIGUOUS")
                    match = matches[0]
                    add_pair(dict(call=match[2], put=match[3], strike=str(match[1]), expiry=match[0].isoformat(),
                                  method="SHARED_EXPLICIT_PAIR", selection_id=option_pair["selection_id"]))
                except QuoteUnavailable as exc:
                    option.update(status="UNAVAILABLE", reason=str(exc))
                    if str(exc) in STOP_CODES or str(exc).startswith("REST_") and str(exc) != "REST_HTTP_404":
                        raise
            else:
                option = dict(status="WAITING_FOR_UNDERLYING_TRADE", underlying=underlying)
                chain = client.chain_options(underlying)
                if option_strike is not None or option_median:
                    add_pair(select_pair(chain, now=clock(), strike=option_strike, median=option_median))
        if not service.identities:
            raise QuoteUnavailable("NO_ACCEPTED_IDENTITIES")

        def extend(session, svc, received):
            """Choose the pair near an observed underlying trade, then subscribe it once."""
            if option.get("status") != "WAITING_FOR_UNDERLYING_TRADE":
                return []
            trade = svc.last_trade(underlying)
            if trade is None:
                return []
            from desk.risk import RiskLimits
            age = (received - trade.traded_at).total_seconds()
            option.update(reference_price=str(trade.price), reference_age_seconds=age,
                          reference_within_quote_policy=age <= RiskLimits().max_quote_age.total_seconds())
            try:
                identities = add_pair(select_pair(chain, now=received, reference=trade.price))
            except QuoteUnavailable as exc:
                option.update(status="UNAVAILABLE", reason=str(exc))
                if str(exc) in STOP_CODES:
                    raise
                return []
            return session.add(identities)

        options = dict(seconds=seconds, reconnects=reconnects, extend=extend, profile=profile)
        if measure is not None:  # opt-in diagnostic measurement channel (package 3)
            options["measure"] = measure
        result = capture_fn(client, service, **options)
    except QuoteUnavailable as exc:
        service.disconnect(str(exc))
        result = dict(stop_reason=str(exc), observations=[], requests=client.requests,
                      attempts=0, connected_after_capture=False)
    finished = clock()
    in_session = session_label(started) == session_label(finished) == "RTH"
    observations = result["observations"]
    final = result.get("final_attempt") or {}
    # Status, coverage and lag come from the final attempt only (R4), and from its
    # terminal view, not its last data message (D1): a schema, status, identity or
    # transport change after the last data is reflected. The last data view and
    # earlier connections' observations are kept, labelled ineligible history.
    final_views = [view for view in observations if final.get("generation")
                   and view.get("generation") == final["generation"]]
    earlier = [view for view in observations if view not in final_views]
    terminal = final.get("terminal_view") or {}
    rows = terminal.get("checks", []) if final.get("generation") else []
    received = {symbol for symbol, seen in (final.get("components") or {}).items()
                if seen.get("Quote") or seen.get("Trade")}
    if option.get("status") == "WAITING_FOR_UNDERLYING_TRADE":
        option["status"] = "NOT_TESTED_NO_UNDERLYING_TRADE"
    groups = {}
    for kind, label in (("Equity", "stocks"), ("Equity Option", "options")):
        group = [row for row in rows if row["kind"] == kind]
        timing = ("NOT_TESTED_MARKET_CLOSED" if not in_session else
                  "OBSERVATIONS_REQUIRE_REVIEW" if any(row["symbol"] in received for row in group)
                  else "NOT_TESTED_NO_OBSERVATIONS")
        if label == "options" and option.get("method") == "MEDIAN_LISTED_NOT_REPRESENTATIVE":
            timing = "NOT_REPRESENTATIVE_MEDIAN_STRIKE"
        groups[label] = dict(checks=group, timing_status=timing)
    groups["options"]["selection"] = option
    if final_views and result["stop_reason"] == "CAPTURE_COMPLETE":
        status = "OBSERVATIONS_ONLY"
    elif final_views:
        status = "FINAL_ATTEMPT_FAILED_AFTER_OBSERVATIONS"  # data, then a fault: never "no observations"
    elif observations:
        status = "NO_FINAL_ATTEMPT_OBSERVATIONS"
    else:
        status = "UNAVAILABLE"
    last_data = final_views[-1] if final_views else None
    clock_rows = {f"{row['symbol']}:{part}": row[part]["clock_uncertainty"] for row in rows
                  for part in ("trade", "quote") if "clock_uncertainty" in row.get(part, {})}
    pair_result = dict(status="NOT_REQUESTED")
    if pair_input is not None and option_pair is None:
        pair_result = dict(status="FAIL", reason=result["stop_reason"])
    if option_pair:
        checks = {option_pair[right]["symbol"]: pair_checks.get(option_pair[right]["symbol"]) or
                  dict(status="CONFLICT", reason=option.get("reason", "OPTION_PAIR_NOT_RESOLVED"))
                  for right in ("call", "put")}
        states = [check["status"] for check in checks.values()]
        pair_result = dict(status="FAIL" if "CONFLICT" in states else "PARTIAL" if "INCOMPLETE" in states
                           else "MATCHED_REPORTED_FIELDS", selection_id=option_pair["selection_id"],
                           by_symbol=checks, scope="Current provider identity and reported size fields only; "
                           "full deliverables NOT_ATTESTED. Not a reviewed mapping or quote eligibility.")
    return dict(purpose="read-only quote observations; no signal/order activation",
                environment=client.environment, started_at=started.isoformat(), finished_at=finished.isoformat(),
                LIVE_TIMING="OBSERVATIONS_REQUIRE_REVIEW" if in_session else "NOT_TESTED_MARKET_CLOSED",
                lag_evidence=_lag_evidence([r for r in rows if r["kind"] == "Equity"], in_session),
                status=status,
                identity_issues=issues, stocks=groups["stocks"], options=groups["options"],
                option_pair_comparison=pair_result,
                final_attempt=dict(
                    attempt=final.get("attempt"), generation=final.get("generation"), outcome=final.get("outcome"),
                    deliberate_end=final.get("deliberate_end"), components=final.get("components", {}),
                    usable_at_end=final.get("usable_at_end", {}),
                    health=final.get("health", "NO_QUOTE_OR_TRADE_USABLE_AT_END"),
                    terminal_view_at=terminal.get("checked_at"),
                    last_data_view=dict(
                        note="The final attempt's last data message, as of its receipt: history only, superseded "
                             "by the terminal view above, never current evidence",
                        eligible=False, view=last_data,
                        lag_evidence_then=_lag_evidence([r for r in (last_data or {}).get("checks", [])
                                                         if r["kind"] == "Equity"], in_session))),
                historical_observations=dict(
                    note="Earlier connections' observations: history only, never current or final-attempt evidence",
                    eligible=False, latest=earlier[-1] if earlier else None),
                capture=result,
                measurements=measure.report() if measure is not None else {"status": "NOT_REQUESTED"},
                clock=dict(host_offset="NOT_MEASURED_BY_THIS_COMMAND",
                           host_clock=host_clock or load_host_clock(None),
                           read_only_check="python -m desk.quote_measure clock (sntp time.apple.com, "
                                           "no clock-setting flags), attached with --host-clock",
                           tolerance_applied="NONE", future_source_time=clock_rows,
                           explanation=CLOCK_NOTE if clock_rows else None),
                identity_capture=[identity.capture() for identity in service.identities.values()],
                mapping_review="Identity capture is evidence for a person's review only; this command never "
                               "records or approves a Webull↔tastytrade mapping (python -m desk.quote_mapping review).",
                missing_runtime_inputs=["broker account/risk snapshots", "market regime",
                                        "option ContractBook deliverables/price increments", "option open interest",
                                        "immediate consolidated intraday volume",
                                        "reviewed Webull↔tastytrade mappings for watchlist names",
                                        "verified provider trading status wiring (Profile delivery unconfirmed)",
                                        "long-lived shared quote service with token renewal"],
                source_roles=dict(current_quotes="tastytrade-dxlink", historical_prices="Webull unchanged",
                                  decision_volume="Alpaca SIP unchanged"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--environment", choices=("production", "sandbox"), required=True)
    parser.add_argument("--option-underlying")
    parser.add_argument("--option-selection", type=Path, help="Use the exact pair from a Tradier select-only report")
    parser.add_argument("--option-strike", type=Decimal,
                        help="Explicit strike; otherwise the strike nearest the observed underlying trade")
    parser.add_argument("--option-median-fallback", action="store_true",
                        help="Off-hours only: median listed strike, reported as not representative")
    parser.add_argument("--profile", action="store_true",
                        help="Also request Profile trading status on a separate channel")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--reconnects", type=int, default=1)
    parser.add_argument("--max-requests", type=int, default=20)
    parser.add_argument("--measure", action="store_true",
                        help="Diagnostic only: raw BBO side-time states, Trade day volume and option Greeks")
    parser.add_argument("--host-clock", type=Path,
                        help="Attach a separately measured result of python -m desk.quote_measure clock")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= len(args.symbols) <= 10 or len(set(args.symbols)) != len(args.symbols):
        parser.error("Provide 1 to 10 distinct equity symbols")
    if not 1 <= args.seconds <= 600 or not 0 <= args.reconnects <= 2:
        parser.error("Seconds must be 1..600; reconnects 0..2")
    if args.option_strike is not None and (not args.option_strike.is_finite() or args.option_strike <= 0):
        parser.error("Option strike must be positive and finite")
    if args.option_strike is not None and args.option_median_fallback:
        parser.error("Use either --option-strike or --option-median-fallback")
    try:
        client = ReadClient(Credentials(os.environ.get("TASTYTRADE_CLIENT_SECRET", ""),
                                        os.environ.get("TASTYTRADE_REFRESH_TOKEN", "")),
                            environment=args.environment, max_requests=args.max_requests)
        service = QuoteService(environment=args.environment)
        pair = load_pair(args.option_selection, environment=args.environment, now=utcnow()) if args.option_selection else None
        report = diagnostic(client, service, args.symbols, option_underlying=args.option_underlying,
                            option_strike=args.option_strike, option_median=args.option_median_fallback,
                            seconds=args.seconds, reconnects=args.reconnects, profile=args.profile,
                            measure=Recorder(service) if args.measure else None,
                            host_clock=load_host_clock(args.host_clock), option_pair=pair)
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
    report = write_report(report, args.output)  # refused if a credential value or marker appears
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["status"] == "OBSERVATIONS_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
