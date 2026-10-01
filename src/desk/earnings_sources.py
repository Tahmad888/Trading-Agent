"""Collect bounded source observations for Step 09 mapping; never run the scanner."""
import argparse
from datetime import datetime, timezone
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from desk.sec import SecData, SecError
from desk.symbols import canonical_symbol
from desk.webull import WebullData, EARNINGS_PATH, INCOME_PATH, FINANCIAL_ALERT_PATH

WEBULL_ROUTES = (("financial_alert", FINANCIAL_ALERT_PATH), ("earnings_calendar", EARNINGS_PATH),
                 ("quarterly_income", INCOME_PATH))


def safe_json(value, secrets=()):
    encoded = json.dumps(value, indent=2, allow_nan=False)
    for secret in secrets:
        if secret:
            encoded = encoded.replace(json.dumps(secret)[1:-1], "<redacted>")
    return encoded + "\n"


def save(path, value, secrets=()):
    encoded = safe_json(value, secrets).encode()
    with path.open("xb") as fh:
        path.chmod(0o600)
        fh.write(encoded)
    return {"file": str(path.resolve()), "sha256": hashlib.sha256(encoded).hexdigest()}


def webull_payload_status(payload):
    if not isinstance(payload, (dict, list)):
        raise ValueError("unexpected payload")
    if isinstance(payload, dict):
        if set(payload) & {"Information", "Note", "Error Message", "error", "errors", "error_code"}:
            raise ValueError("provider error envelope")
        # Unknown envelopes are retained for review, never interpreted as success data.
        if "code" in payload or "success" in payload:
            raise ValueError("unverified status envelope")
    return "EMPTY_UNVERIFIED" if not payload else "OBSERVATIONS_ONLY"


def sec_summary(kind, data):
    if kind == "submissions":
        recent = data["filings"]["recent"]
        keys = ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form", "items", "primaryDocument")
        if not isinstance(recent.get("accessionNumber"), list):
            raise SecError("FILINGS_SCHEMA_UNAVAILABLE")
        lengths = {len(recent[k]) for k in keys if isinstance(recent.get(k), list)}
        if len(lengths) > 1:
            raise SecError("FILINGS_COLUMNS_MISMATCH")
        return {"name": data.get("name"), "tickers": data["tickers"],
                "recent_rows": len(recent["accessionNumber"]),
                "sample": {k: recent[k][:12] for k in keys if isinstance(recent.get(k), list)}}
    facts = data["facts"]
    gaap = facts.get("us-gaap", {})
    if not isinstance(gaap, dict):
        raise SecError("FACTS_SCHEMA_UNAVAILABLE")
    names = [k for k in gaap if "EarningsPerShare" in k or k in {
        "Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax"}]
    concepts = {}
    for name in names:
        units = gaap[name].get("units", {})
        concepts[name] = {}
        for unit, rows in units.items():
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise SecError("FACTS_SCHEMA_UNAVAILABLE")
            # Display sample only; no inference that the last row is the latest quarter.
            sample = sorted(rows, key=lambda row: (str(row.get("end", "")), str(row.get("filed", ""))), reverse=True)[:12]
            concepts[name][unit] = {"rows": len(rows), "sample": sample}
    return {"entity_name": data.get("entityName"), "taxonomies": sorted(facts),
            "concept_observations": concepts, "normalized_quarter_pair": False}


def collect(symbols, directory, *, webull=None, sec=None, initial_errors=None,
            clock=lambda: datetime.now(timezone.utc)):
    symbols = list(dict.fromkeys(canonical_symbol(s) for s in symbols))
    if not 1 <= len(symbols) <= 5 or any(not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", s) for s in symbols):
        raise ValueError("provide 1..5 stock symbols")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    run_dir = directory / (clock().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(mode=0o700)
    secrets = [getattr(webull, k, None) for k in ("_key", "_secret", "_token")]
    report = {"purpose": "source access and schema observations; no decision eligibility",
              "checked_at": clock().isoformat(), "symbols": symbols,
              "run_directory": str(run_dir.resolve()), "checks": [],
              "source_errors": initial_errors or [], "coverage": "UNKNOWN"}
    if webull is not None:
        stopped = False
        for symbol in symbols:
            for kind, route in WEBULL_ROUTES:
                row = {"provider": "webull", "symbol": symbol, "kind": kind,
                       "host": getattr(webull, "_host", "unspecified"), "route": route}
                if stopped:
                    row.update(status="NOT_RUN", reason="stopped after earlier source failure")
                else:
                    try:
                        payload = getattr(webull, kind)(symbol)
                        received = clock().isoformat()
                        status = webull_payload_status(payload)
                        observed = {"symbol": symbol, "kind": kind, "received_at": received,
                                    "host": row["host"], "route": route, "payload": payload}
                        stored = save(run_dir / f"webull-{symbol}-{kind}.json", observed, secrets)
                        row.update(status=status, received_at=received, **stored,
                                   sample=payload[:2] if isinstance(payload, list) else payload)
                    except Exception as exc:
                        row.update(status="UNAVAILABLE", reason="request or schema failed",
                                   http_status=getattr(exc, "status", None))
                        stopped = True
                report["checks"].append(row)
    if sec is not None:
        stopped = False
        try:
            index = sec.ticker_index()
            report["sec_identity_index"] = save(run_dir / "sec-tickers.json", index)
        except Exception as exc:
            report["source_errors"].append({"provider": "sec", "stage": "ticker_index",
                "reason": getattr(exc, "code", "SOURCE_FAILURE"), "http_status": getattr(exc, "http_status", None)})
            stopped = True
        for symbol in symbols:
            cik = None
            if not stopped:
                try:
                    cik = sec.resolve(index, symbol)
                except SecError as exc:
                    report["checks"].append({"provider": "sec", "symbol": symbol,
                        "kind": "identity", "status": "UNAVAILABLE", "reason": exc.code})
                    continue  # A missing ticker does not imply a host outage.
            for kind in ("submissions", "companyfacts"):
                row = {"provider": "sec", "symbol": symbol, "kind": kind, "cik": cik}
                if stopped:
                    row.update(status="NOT_RUN", reason="stopped after earlier source failure")
                else:
                    try:
                        observed = sec.observations(cik, kind, symbol)
                        summary = sec_summary(kind, observed["payload"])
                        stored = save(run_dir / f"sec-{symbol}-{kind}.json", observed)
                        row.update(status="OBSERVATIONS_ONLY", received_at=observed["received_at"],
                                   **stored, summary=summary)
                    except Exception as exc:
                        row.update(status="UNAVAILABLE", reason=getattr(exc, "code", "SOURCE_FAILURE"),
                                   http_status=getattr(exc, "http_status", None))
                        stopped = True
                report["checks"].append(row)
    report["status"] = "INCOMPLETE" if report["source_errors"] or any(
        row["status"] in {"UNAVAILABLE", "NOT_RUN", "EMPTY_UNVERIFIED"} for row in report["checks"]
    ) or not report["checks"] else "OBSERVATIONS_ONLY"
    # Redact before returning or storing any Webull/provider text.
    report = json.loads(safe_json(report, secrets))
    save(run_dir / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="NVDA,AAPL,MSFT", help="1..5 comma-separated stock tickers")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--sources", choices=("webull", "sec", "both"), default="both")
    args = parser.parse_args(argv)
    symbols = [canonical_symbol(s) for s in args.symbols.split(",")]
    if not 1 <= len(set(symbols)) <= 5 or any(not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", s) for s in symbols):
        parser.error("provide 1..5 stock symbols")
    webull, sec, errors = None, None, []
    if args.sources in {"webull", "both"}:
        try:
            webull = WebullData.from_env()
        except Exception:
            errors.append({"provider": "webull", "reason": "CONFIGURATION_UNAVAILABLE"})
    if args.sources in {"sec", "both"}:
        try:
            ua = os.environ.get("SEC_USER_AGENT")
            if not ua:
                if not sys.stdin.isatty():
                    raise SecError("SEC_USER_AGENT_REQUIRED")
                ua = getpass("SEC application/contact (e.g. TradingDesk your-email; hidden, not saved): ")
            sec = SecData(ua)
        except (ValueError, EOFError, KeyboardInterrupt):
            errors.append({"provider": "sec", "reason": "SEC_USER_AGENT_REQUIRED"})
    try:
        result = collect(symbols, args.output_dir, webull=webull, sec=sec, initial_errors=errors)
    except (OSError, ValueError):
        result = {"status": "INCOMPLETE", "reason": "invalid request or local output unavailable"}
    print(safe_json(result), end="")
    return 0 if result["status"] == "OBSERVATIONS_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
