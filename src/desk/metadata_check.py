"""Read-only metadata/bar identity probe. No price acceptance, signals or orders.

Run: python -m desk.metadata_check --symbols NVDA SPY BRK.B
One metadata lookup, then bars grouped by category/timeframe; stop on fetch error.
"""
import argparse
from datetime import datetime, timezone
import json

from desk.security import securities
from desk.symbols import canonical_symbol
from desk.webull import WebullData


def check(source, symbols, *, timespans=("D", "M15")):
    symbols = list(dict.fromkeys(canonical_symbol(s) for s in symbols))
    report = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "purpose": "metadata and bar identity only; no decision eligibility",
              "host": getattr(source, "_host", "unspecified"), "checks": []}
    try:
        if not 1 <= len(symbols) <= 20 or not timespans or any(t not in {"D", "M15"} for t in timespans):
            raise ValueError("unsupported probe request")
        rows = source.security_metadata(symbols)

        class Captured:
            def security_metadata(self, names):
                return rows

        skipped = {}
        metadata = securities(Captured(), symbols, skipped)
        report["skipped"] = skipped
        checks = {name: {"symbol": name, "provider_symbol": item.provider_symbol,
                         "instrument_id": item.instrument_id, "name": item.name,
                         "security_type": item.sub_category, "bar_category": item.bar_category,
                         "metadata_observed_at": item.observed_at.isoformat(),
                         "bars": {}, "status": "PENDING"}
                  for name, item in metadata.items()}
        report["checks"] = list(checks.values())
        for timespan in dict.fromkeys(timespans):
            for category in ("US_STOCK", "US_ETF"):
                names = [n for n, item in metadata.items() if item.bar_category == category]
                if not names:
                    continue
                frames = source.bars(names, category=category, timespan=timespan, count=2)
                for name in names:
                    frame = frames.get(name)
                    identity = frame.attrs.get("provider_identity") if frame is not None else None
                    matches = identity == {"symbol": name, "instrument_id": metadata[name].instrument_id}
                    checks[name]["bars"][timespan] = {
                        "identity": identity,
                        "provider_identity_raw": frame.attrs.get("provider_identity_raw") if frame is not None else None,
                        "status": "PASS" if matches else "FAIL"}
        for item in checks.values():
            item["status"] = "PASS" if all(b["status"] == "PASS" for b in item["bars"].values()) else "FAIL"
        report["status"] = "PASS" if not skipped and len(checks) == len(symbols) and all(
            c["status"] == "PASS" for c in checks.values()) else "FAIL"
    except Exception as exc:
        # Do not expose raw transport bodies, URLs, exception messages or credentials.
        report.update(status="FAIL", error_type=type(exc).__name__,
                      http_status=getattr(exc, "status", None), stopped_on_error=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["NVDA", "SPY", "BRK.B"])
    parser.add_argument("--timespans", nargs="+", choices=["D", "M15"], default=["D", "M15"])
    args = parser.parse_args(argv)
    try:
        result = check(WebullData.from_env(), args.symbols, timespans=args.timespans)
    except Exception as exc:
        result = {"status": "FAIL", "error_type": type(exc).__name__}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
