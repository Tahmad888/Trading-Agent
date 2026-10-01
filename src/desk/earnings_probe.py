"""Read-only Webull earnings observations for mapping review; never qualifies a trade."""
import argparse
from datetime import datetime, timezone
import json

from desk.symbols import canonical_symbol
from desk.webull import WebullData


def probe(source, symbol):
    report = {"purpose": "earnings schema observations only; no decision eligibility",
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "host": getattr(source, "_host", "unspecified"),
              "symbol": canonical_symbol(symbol), "observations": []}
    try:
        for name, method in (("calendar", source.earnings_calendar), ("quarterly_income", source.quarterly_income)):
            payload = method(symbol)
            if isinstance(payload, dict) and (set(payload) & {"Information", "Note", "Error Message", "error", "error_code"}):
                report.update(status="UNAVAILABLE", stage=name, stopped_on_error=True)
                break
            report["observations"].append({"kind": name,
                "received_at": datetime.now(timezone.utc).isoformat(), "payload": payload})
        else:
            report["status"] = "OBSERVATIONS_ONLY"
    except Exception as exc:
        report.update(status="UNAVAILABLE", error_type=type(exc).__name__,
                      http_status=getattr(exc, "status", None), stopped_on_error=True)
    # Providers sometimes echo credentials in HTTP-200 bodies. Redact before any output.
    encoded = json.dumps(report, allow_nan=False)
    for attribute in ("_key", "_secret", "_token"):
        secret = getattr(source, attribute, None)
        if secret:
            encoded = encoded.replace(json.dumps(secret)[1:-1], "<redacted>")
    return json.loads(encoded)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="NVDA")
    args = parser.parse_args(argv)
    try:
        result = probe(WebullData.from_env(), args.symbol)
    except Exception as exc:
        result = {"status": "UNAVAILABLE", "error_type": type(exc).__name__}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "OBSERVATIONS_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
