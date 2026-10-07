"""Small read-only retail route check. No live profiles or action coverage claims.

Run on the credentialed host: python -m desk.provider_check
Output contains only counts/field names and sanitized status, not response values.
The unavailable Display Solution route is described, not retried on guessed hosts.
"""
import argparse
import json

from desk.webull import WebullData, WebullError, WebullHTTPError, DIVIDENDS_PATH, FUND_SPLITS_PATH


def check(source: WebullData) -> dict:
    report = {"host": source._host, "minimum_interval_seconds": source._min_interval,
              "purpose": "partial-evidence connectivity only; no decision eligibility",
              "corporate_action_coverage": "UNKNOWN",
              "display_action_route": "Separate Display Solution product; retail replacement/access unverified",
              "checks": []}
    for symbol, path, fetch in (("NVDA", DIVIDENDS_PATH, source.dividend_calendar),
                                ("SPY", FUND_SPLITS_PATH, source.fund_splits)):
        item = {"symbol": symbol, "path": path, "received_at": None}
        try:
            rows = fetch(symbol)
            item.update(status="PARTIAL_EVIDENCE_ONLY", row_count=len(rows),
                        fields=sorted({key for row in rows for key in row}))
        except WebullHTTPError as exc:
            item.update(status="UNAVAILABLE", http_status=exc.status, reason=str(exc))
        except WebullError:
            item.update(status="UNKNOWN", reason="Transport or reply validation failed; no coverage attested")
        item["received_at"] = source._clock().isoformat()
        report["checks"].append(item)
    return report


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        source = WebullData.from_env()
    except WebullError:
        print(json.dumps({"status": "NOT_CONFIGURED", "reason": "Check local Webull environment; do not paste secrets"}))
        return 1
    report = check(source)
    print(json.dumps(report, indent=2))
    return 0 if all(r["status"] == "PARTIAL_EVIDENCE_ONLY" for r in report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
