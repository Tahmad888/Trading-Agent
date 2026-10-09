"""Bounded Webull snapshot observations; never quote/ticket eligibility.

G5a parent Checkpoint 3, children 4/5. Fresh metadata, snapshot identity, numeric
fields and separately bracketed source/receipt times. No account/order routes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import re
import time
from urllib import request

from desk.quote_measure import load_host_clock, number_state, time_state, write_report
from desk.security import securities
from desk.symbols import canonical_symbol
from desk.tastytrade_quotes import session_label
from desk.webull import INSTRUMENTS_PATH, SNAPSHOT_PATH, WebullData, WebullError

UTC = timezone.utc
FIELDS = ("bid", "ask", "bid_size", "ask_size", "price", "volume", "delay_minutes")
MAX_REQUESTS = 12  # Diagnostic bound only; does not restrict the trading system.


class ProbeStop(WebullError):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    """Never forward a signed request or spend uncounted redirect requests."""

    def http_error_301(self, req, fp, code, msg, headers):
        fp.close()
        raise ProbeStop("HTTP_REDIRECT_REFUSED")

    http_error_302 = http_error_303 = http_error_307 = http_error_308 = http_error_301


def _read_once(req, timeout):
    # Default HTTPSHandler keeps TLS certificate and hostname verification on.
    with request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
        return response.read()


class BoundedData(WebullData):
    """Count actual calls, including metadata pagination, before transmission."""

    def __init__(self, *args, max_requests=5, **kwargs):
        if type(max_requests) is not int or not 1 <= max_requests <= MAX_REQUESTS:
            raise ProbeStop("INVALID_REQUEST_BUDGET")
        self.max_requests, self.requests = max_requests, 0
        kwargs.setdefault("transport", _read_once)
        super().__init__(*args, **kwargs)

    def _call(self, method, path, query=None, payload=None):
        if method != "GET" or path not in {INSTRUMENTS_PATH, SNAPSHOT_PATH}:
            raise ProbeStop("ROUTE_NOT_ALLOWED")
        if self.requests >= self.max_requests:
            raise ProbeStop("REQUEST_BUDGET_EXHAUSTED")
        self.requests += 1
        return super()._call(method, path, query, payload)


def _now(clock):
    at = clock()
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ProbeStop("INVALID_LOCAL_CLOCK")
    return at.astimezone(UTC)


def _source_time(row, field, received):
    """Observed epoch-ms representation, not a side-change-time attestation."""
    value = row.get(field)
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        row = {field: int(value)}
    return dict(time_state(row, field, received), interpretation="OBSERVED_EPOCH_MILLISECONDS",
                raw_type=type(value).__name__ if field in row else "absent")


def _row(row, expected, sent, received):
    item = dict(symbol=expected.symbol, provider_symbol=expected.provider_symbol or expected.symbol,
                expected_instrument_id=expected.instrument_id,
                request_started_at=sent.isoformat(), received_at=received.isoformat(),
                fields={k: number_state(row, k) for k in FIELDS},
                quote_time=_source_time(row, "quote_time", received),
                last_trade_time=_source_time(row, "last_trade_time", received), issues=[])
    actual_id = row.get("instrument_id")
    item["observed_instrument_id"] = actual_id if isinstance(actual_id, str) else None
    item["identity"] = "MATCH" if actual_id == expected.instrument_id else "MISMATCH"
    if item["identity"] != "MATCH":
        item["issues"].append("INSTRUMENT_ID_MISMATCH_OR_MISSING")
    bid, ask = item["fields"]["bid"], item["fields"]["ask"]
    if bid["state"] != "VALUE" or ask["state"] != "VALUE":
        item["issues"].append("BID_ASK_NONNUMERIC_OR_MISSING")
    elif Decimal(bid["value"]) <= 0 or Decimal(ask["value"]) <= 0:
        item["issues"].append("BID_ASK_NONPOSITIVE")
    elif Decimal(bid["value"]) > Decimal(ask["value"]):
        item["issues"].append("CROSSED_BID_ASK")
    if item["quote_time"]["state"] != "VALUE":
        item["issues"].append("QUOTE_TIME_" + item["quote_time"]["state"])
    item["status"] = "FIELDS_OBSERVED" if not item["issues"] else "SOURCE_ISSUE"
    return item


def check(source, symbols, *, rounds=2, interval_seconds=5, clock=lambda: datetime.now(UTC),
          sleep=time.sleep, host_clock=None):
    """Resolve once, then inspect each snapshot; preserve peers but never attest BBO."""
    report = dict(purpose="read-only Webull snapshot observations; no decision/order activation",
                  status="UNAVAILABLE", checks=[], metadata_issues={}, identity_capture=[], rounds=[], requests=0,
                  host=getattr(source, "_host", "unspecified"), host_clock=host_clock or load_host_clock(None),
                  nbbo_coverage="NOT_ESTABLISHED", option_coverage="NOT_TESTED",
                  bid_ask_side_time_contract="NOT_ESTABLISHED",
                  quote_time_note="Snapshot field only; never assigned to bidTime/askTime or used as eligibility.",
                  volume_note="Raw snapshot observations only; no RTH30 volume or share-basis attestation.",
                  LIVE_TIMING="NOT_TESTED", stopped_on_error=False)
    try:
        if (type(rounds) is not int or not 1 <= rounds <= 3
                or isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float))
                or not math.isfinite(interval_seconds) or not 0 <= interval_seconds <= 30
                or not isinstance(symbols, (list, tuple)) or not 1 <= len(symbols) <= 5
                or any(not isinstance(s, str) for s in symbols)):
            raise ProbeStop("INVALID_PROBE_ARGUMENTS")
        names = list(dict.fromkeys(canonical_symbol(s) for s in symbols))
        if any(not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", s) for s in names):
            raise ProbeStop("INVALID_PROBE_SYMBOL")
        report.update(requested_symbols=names, rounds_requested=rounds)
        report["started_at"] = _now(clock).isoformat()
        raw_metadata = source.security_metadata(names)

        class Captured:
            def security_metadata(self, _names):
                return raw_metadata

        metadata = securities(Captured(), names, report["metadata_issues"])
        if not metadata:
            raise ProbeStop("NO_VERIFIED_WEBULL_METADATA")
        report['identity_capture'] = [dict(host=report['host'], **m.model_dump(mode='json'))
                                      for m in metadata.values()]
        for round_id in range(1, rounds + 1):
            if round_id > 1:
                sleep(interval_seconds)
            group_report = dict(round=round_id, groups=[])
            report["rounds"].append(group_report)
            for category in ("US_STOCK", "US_ETF"):
                grouped = [n for n in names if n in metadata and metadata[n].bar_category == category]
                if not grouped:
                    continue
                sent = _now(clock)
                rows = source.snapshot(grouped, category=category)
                received = _now(clock)
                if received < sent:
                    raise ProbeStop("LOCAL_CLOCK_MOVED_BACKWARDS")
                if not isinstance(rows, list):
                    raise ProbeStop("SNAPSHOT_REPLY_NOT_LIST")
                capture = dict(category=category, symbols=grouped, row_count=len(rows),
                               request_started_at=sent.isoformat(), received_at=received.isoformat(),
                               request_session=session_label(sent), receipt_session=session_label(received),
                               request_elapsed_ms=(received - sent).total_seconds() * 1000,
                               unrequested_or_unattributable_rows=0)
                group_report["groups"].append(capture)
                by_symbol = {name: [] for name in grouped}
                for row in rows:
                    symbol = row.get("symbol") if isinstance(row, dict) else None
                    normalized = canonical_symbol(symbol) if isinstance(symbol, str) else None
                    if normalized not in by_symbol:
                        capture["unrequested_or_unattributable_rows"] += 1
                    else:
                        by_symbol[normalized].append(row)
                for name, matches in by_symbol.items():
                    if len(matches) != 1:
                        item = dict(symbol=name, status="SOURCE_ISSUE", issues=[
                            "SNAPSHOT_MISSING" if not matches else "SNAPSHOT_IDENTITY_AMBIGUOUS"])
                    else:
                        item = _row(matches[0], metadata[name], sent, received)
                    if capture["unrequested_or_unattributable_rows"]:
                        item["status"] = "SOURCE_ISSUE"
                        item["issues"].append("SNAPSHOT_BATCH_UNATTRIBUTABLE")
                    report["checks"].append(dict(item, round=round_id, category=category))
        report["finished_at"] = _now(clock).isoformat()
        groups = [g for r in report["rounds"] for g in r["groups"]]
        report["LIVE_TIMING"] = ("REGULAR_SESSION_OBSERVATIONS_REQUIRE_REVIEW"
                                 if groups and all(g["request_session"] == g["receipt_session"] == "RTH" for g in groups)
                                 else "NOT_TESTED_MARKET_CLOSED_OR_SESSION_BOUNDARY")
        report["status"] = ("OBSERVATIONS_ONLY" if not report["metadata_issues"]
                            and all(c["status"] == "FIELDS_OBSERVED" for c in report["checks"]) else "PARTIAL")
    except Exception as exc:
        # Never disclose transport exception text, headers, URLs or raw bodies.
        report.update(status="PARTIAL" if report["checks"] else "UNAVAILABLE", stopped_on_error=True,
                      error_type=type(exc).__name__, http_status=getattr(exc, "status", None))
        if isinstance(exc, ProbeStop):
            report["stop_reason"] = str(exc)
    report["requests"] = getattr(source, "requests", None)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "NVDA"])
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--max-requests", type=int, default=5)
    parser.add_argument("--host-clock", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        source = BoundedData.from_env(max_requests=args.max_requests)
        result = check(source, args.symbols, rounds=args.rounds, interval_seconds=args.interval_seconds,
                       host_clock=load_host_clock(args.host_clock))
    except Exception as exc:
        result = dict(purpose="read-only Webull snapshot observations; no decision/order activation",
                      status="UNAVAILABLE", error_type=type(exc).__name__)
    result = write_report(result, args.output)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "OBSERVATIONS_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
