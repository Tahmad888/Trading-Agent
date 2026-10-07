"""Bounded read-only Tradier stock/option quote and Greek observations; no orders or approvals.

G5a CP3, Tradier preflight Steps 2/3. Market-data GET routes only:
``/v1/markets/quotes``, ``/v1/markets/options/expirations`` and
``/v1/markets/options/chains``. No account, order, position, balance or streaming
route. Every option field passes through ``desk.option_conventions``; verdicts are
diagnostic results, never quote, ticket or order eligibility.

Token: environment variable ``TRADIER_ACCESS_TOKEN`` (name only), otherwise a hidden
prompt on an interactive terminal. It is never printed or written. Example::

    python -m desk.tradier_option_check --environment production --symbols SPY QQQ NVDA \
        --option-underlying SPY --rounds 4 --interval-seconds 60 --output NEW_DIR/tradier.json
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from decimal import Decimal
import getpass
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from urllib.parse import urlencode

from desk import option_conventions as oc
from desk.diagnostic_pair import load_pair, make_pair, occ_terms, size_match, validate_pair
from desk.quote_measure import write_report
from desk.tastytrade_quotes import QuoteUnavailable, aware, canonical, session_label
from desk.tastytrade_transport import request_json, select_pair, utcnow

from desk.tradier_client import HOSTS, ROUTES, MAX_REQUESTS, TradierClient, listed, wire_symbol

STOPS = {"REST_HTTP_401", "REST_HTTP_403", "REST_HTTP_429", "REST_ERROR", "REST_REQUEST_BUDGET",
         "REST_TRANSPORT_FAILURE", "REST_HTTP_500", "REST_HTTP_502", "REST_HTTP_503"}
PURPOSE = "read-only Tradier quote and Greek observations; never eligibility, no orders"
EXCH = re.compile(r"[A-Z0-9]{1,4}")


def occ(symbol: str) -> dict:
    return occ_terms(symbol)


# ----------------------------------------------------------------- normalize ----

def observe_row(row: dict, requested: dict, received: datetime, chain_terms: dict | None) -> dict:
    """One quote row → field views. Identity failures refuse the row; field failures stay per field."""
    kind = requested["kind"]
    if not isinstance(row, dict) or row.get("symbol") != requested["wire"]:
        raise QuoteUnavailable("IDENTITY_MISMATCH")
    if (kind == "stock" and row.get("type") not in {"stock", "etf"}) or (kind == "option" and row.get("type") != "option"):
        raise QuoteUnavailable("INSTRUMENT_TYPE_MISMATCH")
    view = dict(symbol=requested["symbol"], wire_symbol=row.get("symbol"), kind=kind, type=row.get("type"),
                received_at=received.isoformat())
    if kind == "option":
        terms = occ(requested["symbol"])
        try:
            strike = oc.value_of(row.get("strike"))
        except oc.ConventionError:
            raise QuoteUnavailable("OPTION_IDENTITY_MISMATCH") from None
        if (row.get("underlying") != terms["underlying"] or row.get("expiration_date") != terms["expiry"]
                or row.get("option_type") != terms["right"] or strike != terms["strike"]):
            raise QuoteUnavailable("OPTION_IDENTITY_MISMATCH")
        root = row.get("root_symbol")
        view["terms"] = dict(underlying=terms["underlying"], expiry=terms["expiry"], right=terms["right"],
                             strike=str(terms["strike"]), root_symbol=root,
                             expiration_type=row.get("expiration_type"),
                             root_status="MATCHES_UNDERLYING" if root == terms["underlying"]
                             else "NONSTANDARD_OR_UNKNOWN_ROOT")
        multiplier = oc.multiplier_from(row.get("contract_size"), (chain_terms or {}).get("contract_size"))
        if view["terms"]["root_status"] != "MATCHES_UNDERLYING" and multiplier["status"] == "VERIFIED_FROM_METADATA":
            multiplier = dict(status="UNVERIFIED_NONSTANDARD_ROOT", value=None)
        view["multiplier"] = dict(multiplier, deliverable="NOT_PROVIDED_BY_ROUTE")
        view["multiplier"]["metadata_sources"] = {
            "quote.contract_size": row.get("contract_size"),
            "chain_or_selection.contract_size": (chain_terms or {}).get("contract_size")}
        if (chain_terms or {}).get("pair_expected"):
            view["pair_terms"] = size_match(chain_terms.get("contract_size"), row.get("contract_size"),
                                             standard_root=root == terms["underlying"])
    prices = {}
    for name in ("bid", "ask", "last"):
        try:
            prices[name] = dict(raw=row.get(name), state="VALUE", value=str(oc.value_of(row.get(name))))
        except oc.ConventionError as exc:
            prices[name] = dict(raw=row.get(name), state=str(exc))
    if prices["bid"]["state"] == prices["ask"]["state"] == "VALUE":
        bid, ask = Decimal(prices["bid"]["value"]), Decimal(prices["ask"]["value"])
        prices["book"] = "CROSSED" if bid > ask else "LOCKED" if bid == ask else "NORMAL"
    view["prices"] = prices
    times = {}
    for name in ("bid_date", "ask_date", "trade_date"):
        try:
            parsed = oc.provider_time("tradier", name, row.get(name))
            times[name] = dict(parsed, receipt=oc.age_view(parsed, received))
        except oc.ConventionError as exc:
            times[name] = dict(raw=row.get(name), state=str(exc))
    view["times"] = times
    view["exchanges"] = {k: row.get(k) if isinstance(row.get(k), str) and EXCH.fullmatch(row.get(k)) else
                         ("ABSENT" if row.get(k) is None else "INVALID") for k in ("bidexch", "askexch", "exch")}
    view["sizes"] = {k: oc.size_view("tradier", "rest", kind, row.get(k)) for k in ("bidsize", "asksize")}
    if kind == "stock" and "lot_size" in row:
        view["lot_size_raw"] = row.get("lot_size")  # recorded only; never applied to sizes
    if kind == "option":
        greeks = row.get("greeks")
        if not isinstance(greeks, dict):
            view["greeks"] = dict(state="ABSENT" if greeks is None else "INVALID")
        else:
            normalized = oc.normalize_greeks("tradier", "rest", greeks)
            try:
                parsed = oc.provider_time("tradier", "greeks.updated_at", greeks.get("updated_at"))
                normalized["updated_at"] = dict(parsed, age=oc.age_view(parsed, received),
                                                cadence_note="Tradier documents hourly Greeks; no Greek age threshold.")
            except oc.ConventionError as exc:
                normalized["updated_at"] = dict(raw=greeks.get("updated_at"), state=str(exc))
            normalized["per_contract_example"] = dict(
                contracts=1, label="one long contract; illustration, not a position",
                values=oc.position_view(normalized, view["multiplier"], 1))
            view["greeks"] = normalized
    return view


# ---------------------------------------------------------------- diagnostic ----

def selection(client: TradierClient, underlying: str, reference: Decimal, now: datetime) -> tuple[dict, dict]:
    from desk.calendar import clock, session, trading_day
    reply = client.get("expirations", {"symbol": wire_symbol(underlying), "includeAllRoots": "false"})
    dates = []
    for raw in listed((reply.get("expirations") or {}).get("date") if isinstance(reply.get("expirations"), dict)
                      else None, text=True):
        try:
            dates.append(date.fromisoformat(raw))
        except (TypeError, ValueError):
            raise QuoteUnavailable("EXPIRATIONS_INVALID") from None
    stamp = clock(now)
    today = stamp.date()
    open_today = trading_day(today) and stamp < session(today)[1]
    live = sorted(d for d in dates if d > today or (d == today and open_today))
    if not live:
        raise QuoteUnavailable("OPTION_CHAIN_UNAVAILABLE")
    chain = client.get("chains", {"symbol": wire_symbol(underlying), "expiration": live[0].isoformat(),
                                  "greeks": "false"})
    rows = listed((chain.get("options") or {}).get("option") if isinstance(chain.get("options"), dict) else None)
    by_strike, terms, rejected = {}, {}, []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("root_symbol") != underlying or row.get("underlying") != underlying:
            continue  # nonstandard roots are never selected
        try:
            ident = occ(row.get("symbol") if isinstance(row.get("symbol"), str) else "")
        except QuoteUnavailable as exc:
            rejected.append(dict(row=index, reason=str(exc)))
            continue
        if ident["expiry"] != live[0].isoformat() or row.get("option_type") != ident["right"]:
            continue
        try:
            consistent = (ident["underlying"] == underlying and
                          oc.value_of(row.get("strike")) == ident["strike"] and
                          row.get("expiration_date") == ident["expiry"])
        except oc.ConventionError:
            consistent = False
        if not consistent:
            rejected.append(dict(row=index, reason="OPTION_IDENTITY_MISMATCH"))
            continue
        if row["symbol"] in terms and terms[row["symbol"]]["contract_size"] != row.get("contract_size"):
            raise QuoteUnavailable("OPTION_CHAIN_AMBIGUOUS")
        by_strike.setdefault(ident["strike"], {})[ident["right"]] = row["symbol"]
        terms[row["symbol"]] = {"contract_size": row.get("contract_size")}
    options = [(live[0], strike, pair["call"], pair["put"]) for strike, pair in by_strike.items()
               if "call" in pair and "put" in pair]
    if not options:
        raise QuoteUnavailable("OPTION_CHAIN_UNAVAILABLE")
    chosen = select_pair(options, now=now, reference=reference)
    return dict(chosen, reference="TRADIER_STOCK_QUOTE_MIDPOINT", chain_rows=len(rows),
                rejected_chain_rows=rejected), terms


def quotes(client: TradierClient, requested: list[dict], chain_terms: dict) -> dict:
    sent = aware(client.clock())
    reply = client.get("quotes", {"symbols": ",".join(r["wire"] for r in requested), "greeks": "true"})
    received = aware(client.clock())
    body = reply.get("quotes")
    if not isinstance(body, dict):
        raise QuoteUnavailable("REPLY_SHAPE_INVALID")
    rows = listed(body.get("quote"))
    unmatched = listed((body.get("unmatched_symbols") or {}).get("symbol")
                       if isinstance(body.get("unmatched_symbols"), dict) else None, text=True)
    out, failures = {}, {}
    for want in requested:
        matches = [r for r in rows if isinstance(r, dict) and r.get("symbol") == want["wire"]]
        if len(matches) != 1:
            failures[want["symbol"]] = "DUPLICATE" if matches else "MISSING"
            continue
        try:
            out[want["symbol"]] = observe_row(matches[0], want, received, chain_terms.get(want["wire"]))
        except QuoteUnavailable as exc:
            failures[want["symbol"]] = str(exc)
    expected = {r["wire"] for r in requested}
    extra = sum(1 for r in rows if not isinstance(r, dict) or r.get("symbol") not in expected)
    return dict(request_started_at=sent.isoformat(), received_at=received.isoformat(),
                session=session_label(sent) if session_label(sent) == session_label(received) else "MIXED",
                observations=out, failures=failures, unexpected_rows=extra, unmatched_symbols=len(unmatched))


def advancing(rounds: list[dict], symbol: str) -> dict:
    """Did any provider time move between this symbol's first and last observation?"""
    seen = [r["observations"][symbol] for r in rounds if symbol in r["observations"]]
    out = {"observations": len(seen)}
    if len(seen) < 2:
        out["state"] = "INSUFFICIENT_ROUNDS"
        return out
    moved = []
    for name in ("bid_date", "ask_date", "trade_date"):
        a, b = seen[0]["times"].get(name, {}), seen[-1]["times"].get(name, {})
        if "utc" in a and "utc" in b and b["utc"] > a["utc"]:
            moved.append(name)
    out.update(state="ADVANCED" if moved else "NOT_ADVANCED", fields=moved,
               note="An unchanged side is not by itself proof of delay; receipt age is separate.")
    return out


def price_time_view(observation: dict, price_name: str, time_name: str, checked_at: datetime) -> dict:
    """Latest field's measured age and existing policy, not feed entitlement or eligibility."""
    from desk.risk import RiskLimits
    limit = RiskLimits().max_quote_age.total_seconds()
    price = observation["prices"][price_name]
    stamp = observation["times"][time_name]
    reasons = []
    if price.get("state") != "VALUE":
        reasons.append("PRICE_UNAVAILABLE")
    elif Decimal(price["value"]) <= 0:
        reasons.append("PRICE_NONPOSITIVE")
    timing = dict(stamp)
    if "utc" not in stamp:
        reasons.append(stamp.get("state", "TIME_UNAVAILABLE"))
    else:
        timing["at_check"] = oc.age_view(stamp, checked_at)
        age = timing["at_check"]["age_seconds"]
        if age < 0 or stamp.get("receipt", {}).get("age_seconds", 0) < 0:
            reasons.append("FUTURE_SOURCE_TIME")
        elif age > limit:
            reasons.append("OLDER_THAN_QUOTE_POLICY")
    return dict(verdict="FAIL" if reasons else "PASS", price=price, time=timing,
                checked_at=checked_at.isoformat(), max_age_seconds=limit, reasons=reasons)


def latest_policy_view(observation: dict, checked_at: datetime) -> dict:
    bid = price_time_view(observation, "bid", "bid_date", checked_at)
    ask = price_time_view(observation, "ask", "ask_date", checked_at)
    trade = price_time_view(observation, "last", "trade_date", checked_at)
    book = observation["prices"].get("book", "UNAVAILABLE")
    quote = dict(verdict="PASS" if bid["verdict"] == ask["verdict"] == "PASS" and book == "NORMAL"
                 else "FAIL", bid=bid, ask=ask, book=book,
                 note="Side-change age outside policy does not alone prove a delayed feed. "
                      "Trade time cannot freshen bid/ask; size units and entitlement are not attested.")
    return dict(quote=quote, trade=trade)


KEYS = ("stock_quote_freshness", "option_quote_freshness", "stock_trade_freshness", "option_trade_freshness",
        "option_size_interpretation", "greek_normalization",
        "greek_timestamp_interpretation", "current_greek_state", "reconnect_behavior")


def verdicts(report: dict) -> dict:
    if report.get("stopped_on_error"):
        # A stopped run has no current result: earlier rounds stay history only.
        verdict = "FAIL" if report["rounds"] else "NOT_RUN"
        out = {k: dict(verdict=verdict, reason=f"RUN_STOPPED:{report.get('reason')}") for k in KEYS}
        out["reconnect_behavior"] = dict(verdict="NOT_RUN", reason="REST-only diagnostic; no Tradier stream here.")
        out["note"] = "Diagnostic verdicts only; never approval or order eligibility."
        return out
    rounds = report["rounds"]
    rth = (bool(rounds) and all(r["session"] == "RTH" for r in rounds)
           and session_label(datetime.fromisoformat(report["finished_at"])) == "RTH")
    out = {}
    for kind, key, component in (("stock", "stock_quote_freshness", "quote"),
                                 ("option", "option_quote_freshness", "quote"),
                                 ("stock", "stock_trade_freshness", "trade"),
                                 ("option", "option_trade_freshness", "trade")):
        names = [r["symbol"] for r in report["requested"] if r["kind"] == kind]
        if not names:
            out[key] = dict(verdict="NOT_RUN", reason="NOT_REQUESTED")
        elif not rth:
            out[key] = dict(verdict="NOT_RUN", reason="NOT_A_REGULAR_SESSION_FOR_EVERY_ROUND")
        else:
            final = rounds[-1]
            checked_at = aware(datetime.fromisoformat(report["finished_at"]))
            states = {}
            for n in names:
                observation = final["observations"].get(n)
                states[n] = (latest_policy_view(observation, checked_at)[component] if observation is not None
                             else dict(verdict="FAIL", reason=final["failures"].get(n, "FINAL_SYMBOL_UNAVAILABLE")))
            good = [n for n, s in states.items() if s["verdict"] == "PASS"]
            out[key] = dict(verdict="PASS" if len(good) == len(names) else "PARTIAL" if good else "FAIL",
                            by_symbol=states, scope="Latest round prices and their own source times under "
                            "the existing quote policy; advancement is separate, real-time coverage NOT_ATTESTED.")
    options = [o for r in rounds[-1:] for o in r["observations"].values() if o["kind"] == "option"]
    out["option_size_interpretation"] = dict(
        verdict="PARTIAL" if options else "NOT_RUN",
        reason="Raw sizes kept with size_unit_status=UNVERIFIED, interpreted_unit=CONTRACTS_PROVISIONAL; "
               "no Tradier definition or OPRA-referenced option comparison yet.")
    greek_states = [o.get("greeks", {}) for o in options]
    out["greek_normalization"] = dict(
        verdict="PARTIAL" if any("fields" in g for g in greek_states) else "NOT_RUN",
        reason="delta normalized (observed signed per contract); gamma/theta/vega provisional (ORATS "
               "upstream definitions); rho/phi raw only (shared strike values).")
    parsed = [g.get("updated_at", {}) for g in greek_states if "fields" in g]
    out["greek_timestamp_interpretation"] = dict(
        verdict="PARTIAL" if parsed and all("utc" in p for p in parsed) else "FAIL" if parsed else "NOT_RUN",
        reason="Parsed as UTC under a relayed 2024 Tradier support answer; not a current first-party schema.")
    out["current_greek_state"] = dict(verdict="OBSERVATIONS_ONLY" if parsed else "NOT_RUN",
                                      reason="REST snapshots of hourly Greeks; no Greek age threshold exists.")
    out["reconnect_behavior"] = dict(verdict="NOT_RUN", reason="REST-only diagnostic; no Tradier stream here.")
    out["note"] = "Diagnostic verdicts only; never approval or order eligibility."
    return out


def pair_comparison(report: dict) -> dict:
    pair = report.get("option_pair")
    if not pair:
        return dict(status="NOT_REQUESTED")
    if report.get("stopped_on_error") or not report["rounds"]:
        return dict(status="NOT_RUN", selection_id=pair["selection_id"])
    last = report["rounds"][-1]
    checks = {}
    for right in ("call", "put"):
        symbol = pair[right]["symbol"]
        checks[symbol] = last["observations"].get(symbol, {}).get("pair_terms") or dict(
            status="CONFLICT", reason=last["failures"].get(symbol, "PAIR_TERMS_UNAVAILABLE"))
    states = [check["status"] for check in checks.values()]
    return dict(status="FAIL" if "CONFLICT" in states else "PARTIAL" if "INCOMPLETE" in states
                else "MATCHED_REPORTED_FIELDS", selection_id=pair["selection_id"], by_symbol=checks,
                scope="Selected identity and reported size fields only; full deliverables NOT_ATTESTED.")


def diagnostic(client: TradierClient, equities, *, option_underlying=None, rounds=4, interval_seconds=60,
               sleep=time.sleep, option_pair=None, select_only=False) -> dict:
    report = dict(purpose=PURPOSE, status="UNAVAILABLE", environment=client.environment, requested=[],
                  option_selection={"status": "NOT_REQUESTED"}, rounds=[], advancing={},
                  coverage="NOT_ATTESTED", mapping="NOT_REVIEWED_BY_DIAGNOSTIC", decision_eligibility="NOT_EVALUATED",
                  note="Earlier rounds are history only; a failed later round leaves no current result.")
    from desk.risk import RiskLimits
    report["quote_policy"] = dict(max_age_seconds=RiskLimits().max_quote_age.total_seconds(),
                                 source="RiskLimits.max_quote_age", future_tolerance_seconds=0,
                                 greek_age_threshold="NONE_DEFINED")
    try:
        if (not isinstance(equities, (list, tuple)) or not 1 <= len(equities) <= 6
                or type(rounds) is not int or not 1 <= rounds <= 6
                or isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float))
                or not math.isfinite(interval_seconds) or not 0 <= interval_seconds <= 90):
            raise QuoteUnavailable("PROBE_ARGUMENTS_INVALID")
        names = [canonical(s, "Equity") for s in equities]
        if option_pair is not None:
            option_pair = validate_pair(option_pair, environment=client.environment, now=client.clock())
        underlying = (canonical(option_underlying, "Equity") if option_underlying else
                      option_pair["underlying"] if option_pair else None)
        if option_pair and underlying != option_pair["underlying"] or select_only and (not underlying or option_pair):
            raise QuoteUnavailable("PROBE_ARGUMENTS_INVALID")
        if underlying and underlying not in names:
            names.append(underlying)
        if len(set(names)) != len(names):
            raise QuoteUnavailable("PROBE_ARGUMENTS_INVALID")
        needed = (3 if underlying and not option_pair else 0) + (0 if select_only else rounds)
        if needed > client.max_requests:
            raise QuoteUnavailable("REQUEST_PLAN_EXCEEDS_BUDGET")
        report["request_plan"] = dict(planned=needed, budget=client.max_requests)
        started = aware(client.clock())
        report["started_at"] = started.isoformat()
        requested = [dict(symbol=n, wire=wire_symbol(n), kind="stock") for n in names]
        chain_terms = {}
        if option_pair:
            chosen = dict(call=option_pair["call"]["symbol"], put=option_pair["put"]["symbol"],
                          strike=option_pair["strike"], expiry=option_pair["expiry"], method="SHARED_EXPLICIT_PAIR")
        elif underlying:
            report["option_selection"] = {"status": "UNAVAILABLE", "underlying": underlying}
            seed = quotes(client, [r for r in requested if r["symbol"] == underlying], {})
            report["option_reference_observation"] = seed
            ref = seed["observations"].get(underlying, {}).get("prices", {})
            try:
                if ref["bid"]["state"] != "VALUE" or ref["ask"]["state"] != "VALUE" or ref.get("book") != "NORMAL":
                    raise KeyError
                mid = (Decimal(ref["bid"]["value"]) + Decimal(ref["ask"]["value"])) / 2
            except KeyError:
                raise QuoteUnavailable("OPTION_REFERENCE_UNAVAILABLE") from None
            chosen, chain_terms = selection(client, underlying, mid, aware(client.clock()))
            option_pair = make_pair(chosen, chain_terms, environment=client.environment, now=client.clock())
        if underlying:
            report["option_pair"] = option_pair
            chain_terms = {option_pair[right]["symbol"]: dict(
                contract_size=option_pair[right]["contract_size"], pair_expected=True) for right in ("call", "put")}
            report["option_selection"] = dict(chosen, status="SELECTED", underlying=underlying,
                                              note="Diagnostic selection only; never a trading plan.")
            requested += [dict(symbol=chosen["call"], wire=chosen["call"], kind="option"),
                          dict(symbol=chosen["put"], wire=chosen["put"], kind="option")]
        report["requested"] = requested
        for index in range(0 if select_only else rounds):
            if index:
                sleep(interval_seconds)
            try:
                report["rounds"].append(dict(round=index + 1, **quotes(client, requested, chain_terms)))
            except QuoteUnavailable as exc:
                report["rounds"].append(dict(round=index + 1, failed=str(exc)))
                raise
        report["advancing"] = {r["symbol"]: advancing(report["rounds"], r["symbol"]) for r in requested}
        last = report["rounds"][-1] if report["rounds"] else {"observations": {}, "failures": {}, "unexpected_rows": 0}
        options = [last["observations"][r["symbol"]] for r in requested
                   if r["kind"] == "option" and r["symbol"] in last["observations"]]
        if len(options) == 2:
            call, put = sorted(options, key=lambda o: o["terms"]["right"])
            if "fields" in call.get("greeks", {}) and "fields" in put.get("greeks", {}):
                report["greek_pair_check"] = oc.pair_check(call["greeks"], put["greeks"])
        report["status"] = ("OPTION_PAIR_SELECTED" if select_only else
                            "PARTIAL_OBSERVATIONS" if last["failures"] or last["unexpected_rows"] else "OBSERVATIONS_ONLY")
        report["finished_at"] = aware(client.clock()).isoformat()
    except QuoteUnavailable as exc:
        report.update(status="UNAVAILABLE", reason=str(exc), stopped_on_error=True,
                      current="NONE_AFTER_FAILURE")
        if str(exc) == "REST_REQUEST_BUDGET":
            report["failure_category"] = "LOCAL_REQUEST_BUDGET"
    report["verdicts"] = verdicts(report)
    report["option_pair_comparison"] = pair_comparison(report)
    if report["status"] == "OBSERVATIONS_ONLY" and report["option_pair_comparison"]["status"] in {"FAIL", "PARTIAL"}:
        report["status"] = "PARTIAL_OBSERVATIONS"
    report["requests"] = dict(count=client.requests, budget=client.max_requests, log=client.log)
    return report


def main(argv=None, env=None, prompt=getpass.getpass) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--environment", choices=tuple(HOSTS), required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--option-underlying")
    parser.add_argument("--select-only", action="store_true", help="Select one pair without capture rounds")
    parser.add_argument("--option-selection", type=Path, help="Reuse the exact pair from a select-only report")
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--max-requests", type=int, default=MAX_REQUESTS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output exists; choose a new path (reports are never overwritten)")
    env = dict(os.environ if env is None else env)
    token = env.get("TRADIER_ACCESS_TOKEN", "")
    if not token and sys.stdin.isatty():
        token = prompt("Tradier access token (hidden): ")
    env["TRADIER_ACCESS_TOKEN"] = token  # only so the report guard can refuse a leak
    try:
        client = TradierClient(token, environment=args.environment, max_requests=args.max_requests)
        pair = load_pair(args.option_selection, environment=args.environment, now=client.clock()) if args.option_selection else None
        report = diagnostic(client, args.symbols, option_underlying=args.option_underlying, rounds=args.rounds,
                            interval_seconds=args.interval_seconds, option_pair=pair, select_only=args.select_only)
    except QuoteUnavailable as exc:
        report = dict(purpose=PURPOSE, status="UNAVAILABLE", reason=str(exc), decision_eligibility="NOT_EVALUATED")
    try:
        report["commit"] = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True,
                                          text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        report["commit"] = "UNKNOWN"
    report["python"] = platform.python_version()
    report = write_report(report, args.output, env)
    summary = {k: report.get(k) for k in ("status", "reason", "code")}
    summary["verdicts"] = {k: v.get("verdict") for k, v in (report.get("verdicts") or {}).items() if isinstance(v, dict)}
    print(json.dumps(summary, indent=2))
    return 0 if report.get("status") in {"OBSERVATIONS_ONLY", "OPTION_PAIR_SELECTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
