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
from desk.quote_measure import write_report
from desk.tastytrade_quotes import QuoteUnavailable, aware, canonical, session_label
from desk.tastytrade_transport import request_json, select_pair, utcnow

HOSTS = {"production": "https://api.tradier.com", "sandbox": "https://sandbox.tradier.com"}
ROUTES = {"quotes": "/v1/markets/quotes", "expirations": "/v1/markets/options/expirations",
          "chains": "/v1/markets/options/chains"}
STOPS = {"REST_HTTP_401", "REST_HTTP_403", "REST_HTTP_429", "REST_ERROR", "REST_REQUEST_BUDGET",
         "REST_TRANSPORT_FAILURE", "REST_HTTP_500", "REST_HTTP_502", "REST_HTTP_503"}
MAX_REQUESTS = 12   # diagnostic workload bound, not a trading limit
PURPOSE = "read-only Tradier quote and Greek observations; never eligibility, no orders"
EXCH = re.compile(r"[A-Z0-9]{1,4}")


class TradierClient:
    """GET-only market-data client on the audited transport (TLS, no redirects, safe errors)."""

    def __init__(self, token: str, *, environment="production", max_requests=MAX_REQUESTS, request=request_json,
                 clock=utcnow):
        if environment not in HOSTS:
            raise QuoteUnavailable("ENVIRONMENT_INVALID")
        if not isinstance(token, str) or not token.strip():
            raise QuoteUnavailable("CREDENTIALS_MISSING")
        if type(max_requests) is not int or not 1 <= max_requests <= MAX_REQUESTS:
            raise QuoteUnavailable("REQUEST_BUDGET_INVALID")
        self._token, self.environment, self.max_requests = token, environment, max_requests
        self._request, self.clock, self.requests, self.log = request, clock, 0, []

    def get(self, route: str, query: dict) -> dict:
        if route not in ROUTES:
            raise QuoteUnavailable("ROUTE_NOT_ALLOWED")
        if self.requests >= self.max_requests:
            raise QuoteUnavailable("REST_REQUEST_BUDGET")
        self.requests += 1
        entry = {"route": ROUTES[route], "sent_at": aware(self.clock()).isoformat()}
        self.log.append(entry)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                   "User-Agent": "trading-desk-tradier-check/1"}
        try:
            reply = self._request("GET", HOSTS[self.environment] + ROUTES[route] + "?" + urlencode(query),
                                  headers, None)
        except QuoteUnavailable as exc:
            entry.update(received_at=aware(self.clock()).isoformat(), outcome=str(exc))
            raise
        entry["received_at"] = aware(self.clock()).isoformat()
        # Tradier can answer HTTP 200 with a fault/errors body: never an observation.
        if not isinstance(reply, dict) or "fault" in reply or "errors" in reply:
            entry["outcome"] = "REST_ERROR"
            raise QuoteUnavailable("REST_ERROR")
        entry["outcome"] = "OK"
        return reply


def listed(value, *, text=False):
    """Tradier returns one item or a list; null means none. Text items only where documented."""
    if value is None:
        return []
    if isinstance(value, dict) or (text and isinstance(value, str)):
        return [value]
    if isinstance(value, list):
        return value
    raise QuoteUnavailable("REPLY_SHAPE_INVALID")


def wire_symbol(symbol: str) -> str:
    return symbol.replace(".", "/")  # observed: Tradier returned BRK/B (diagnostic pairing only)


def occ(symbol: str) -> dict:
    match = re.fullmatch(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})", symbol)
    if not match:
        raise QuoteUnavailable("OPTION_IDENTITY_INVALID")
    return dict(underlying=match[1], expiry=datetime.strptime(match[2], "%y%m%d").date().isoformat(),
                right="call" if match[3] == "C" else "put", strike=Decimal(match[4]) / 1000)


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
    by_strike, terms = {}, {}
    for row in rows:
        if not isinstance(row, dict) or row.get("root_symbol") != underlying or row.get("underlying") != underlying:
            continue  # nonstandard roots are never selected
        try:
            ident = occ(row.get("symbol") if isinstance(row.get("symbol"), str) else "")
        except QuoteUnavailable:
            continue
        if ident["expiry"] != live[0].isoformat() or row.get("option_type") != ident["right"]:
            continue
        by_strike.setdefault(ident["strike"], {})[ident["right"]] = row["symbol"]
        terms[row["symbol"]] = {"contract_size": row.get("contract_size")}
    options = [(live[0], strike, pair["call"], pair["put"]) for strike, pair in by_strike.items()
               if "call" in pair and "put" in pair]
    if not options:
        raise QuoteUnavailable("OPTION_CHAIN_UNAVAILABLE")
    chosen = select_pair(options, now=now, reference=reference)
    return dict(chosen, reference="TRADIER_STOCK_QUOTE_MIDPOINT", chain_rows=len(rows)), terms


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


KEYS = ("stock_quote_freshness", "option_quote_freshness", "option_size_interpretation", "greek_normalization",
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
    rth = bool(rounds) and all(r["session"] == "RTH" for r in rounds)
    out = {}
    for kind, key in (("stock", "stock_quote_freshness"), ("option", "option_quote_freshness")):
        names = [r["symbol"] for r in report["requested"] if r["kind"] == kind]
        if not names:
            out[key] = dict(verdict="NOT_RUN", reason="NOT_REQUESTED")
        elif not rth:
            out[key] = dict(verdict="NOT_RUN", reason="NOT_A_REGULAR_SESSION_FOR_EVERY_ROUND")
        else:
            states = {n: report["advancing"].get(n, {}).get("state") for n in names}
            good = [n for n, s in states.items() if s == "ADVANCED"]
            out[key] = dict(verdict="PASS" if len(good) == len(names) else "PARTIAL" if good else "FAIL",
                            by_symbol=states, scope="provider times advanced within this capture only")
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


def diagnostic(client: TradierClient, equities, *, option_underlying=None, rounds=4, interval_seconds=60,
               sleep=time.sleep) -> dict:
    report = dict(purpose=PURPOSE, status="UNAVAILABLE", environment=client.environment, requested=[],
                  option_selection={"status": "NOT_REQUESTED"}, rounds=[], advancing={},
                  coverage="NOT_ATTESTED", mapping="NOT_REVIEWED_BY_DIAGNOSTIC", decision_eligibility="NOT_EVALUATED",
                  note="Earlier rounds are history only; a failed later round leaves no current result.")
    try:
        if (not isinstance(equities, (list, tuple)) or not 1 <= len(equities) <= 6
                or type(rounds) is not int or not 1 <= rounds <= 6
                or isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float))
                or not math.isfinite(interval_seconds) or not 0 <= interval_seconds <= 90):
            raise QuoteUnavailable("PROBE_ARGUMENTS_INVALID")
        names = [canonical(s, "Equity") for s in equities]
        underlying = canonical(option_underlying, "Equity") if option_underlying else None
        if underlying and underlying not in names:
            names.append(underlying)
        if len(set(names)) != len(names):
            raise QuoteUnavailable("PROBE_ARGUMENTS_INVALID")
        needed = (1 + 2 if underlying else 0) + rounds
        if needed > client.max_requests:
            raise QuoteUnavailable("REQUEST_PLAN_EXCEEDS_BUDGET")
        report["request_plan"] = dict(planned=needed, budget=client.max_requests)
        started = aware(client.clock())
        report["started_at"] = started.isoformat()
        requested = [dict(symbol=n, wire=wire_symbol(n), kind="stock") for n in names]
        chain_terms = {}
        if underlying:
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
            report["option_selection"] = dict(chosen, status="SELECTED", underlying=underlying,
                                              note="Diagnostic selection only; never a trading plan.")
            requested += [dict(symbol=chosen["call"], wire=chosen["call"], kind="option"),
                          dict(symbol=chosen["put"], wire=chosen["put"], kind="option")]
        report["requested"] = requested
        for index in range(rounds):
            if index:
                sleep(interval_seconds)
            try:
                report["rounds"].append(dict(round=index + 1, **quotes(client, requested, chain_terms)))
            except QuoteUnavailable as exc:
                report["rounds"].append(dict(round=index + 1, failed=str(exc)))
                raise
        report["advancing"] = {r["symbol"]: advancing(report["rounds"], r["symbol"]) for r in requested}
        last = report["rounds"][-1]
        options = [last["observations"][r["symbol"]] for r in requested
                   if r["kind"] == "option" and r["symbol"] in last["observations"]]
        if len(options) == 2:
            call, put = sorted(options, key=lambda o: o["terms"]["right"])
            if "fields" in call.get("greeks", {}) and "fields" in put.get("greeks", {}):
                report["greek_pair_check"] = oc.pair_check(call["greeks"], put["greeks"])
        report["status"] = ("PARTIAL_OBSERVATIONS" if last["failures"] or last["unexpected_rows"]
                            else "OBSERVATIONS_ONLY")
        report["finished_at"] = aware(client.clock()).isoformat()
    except QuoteUnavailable as exc:
        report.update(status="UNAVAILABLE", reason=str(exc), stopped_on_error=True,
                      current="NONE_AFTER_FAILURE")
    report["verdicts"] = verdicts(report)
    report["requests"] = dict(count=client.requests, budget=client.max_requests, log=client.log)
    return report


def main(argv=None, env=None, prompt=getpass.getpass) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--environment", choices=tuple(HOSTS), required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--option-underlying")
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
        report = diagnostic(client, args.symbols, option_underlying=args.option_underlying, rounds=args.rounds,
                            interval_seconds=args.interval_seconds)
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
    return 0 if report.get("status") == "OBSERVATIONS_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
