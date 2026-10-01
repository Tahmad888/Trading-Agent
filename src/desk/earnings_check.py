"""Evaluate a reviewed local earnings file without market requests or scanner activation."""
import argparse
from datetime import datetime, timezone
import json

from desk.earnings import ReviewedEarningsSource, evaluate
from desk.symbols import canonical_symbol


def aware_time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("use an ISO timestamp with timezone") from None


def check(path, symbol, security_id, at, *, replay=False):
    report = {
        "purpose": "fundamental component evaluation only; no chart/order eligibility",
        "mode": "historical replay" if replay else "current review",
        "symbol": canonical_symbol(symbol), "security_id": security_id,
        "evaluated_at": at.isoformat(), "provider_requests": 0,
    }
    try:
        evidence = ReviewedEarningsSource(None, path).earnings_evidence(symbol)
        results = {name: evaluate(setup, symbol, security_id, evidence, at, trigger_at=at)
                   for name, setup in (("cup", "3_oneil_cup_with_handle"),
                                       ("ep", "5_qullamaggie_episodic_pivot"))}
        # A missing catalyst is a valid gate result, not a failed API request.
        # Evidence identity, parsing and review-window failures remain unavailable.
        report["status"] = "EVALUATED" if all("evidence_id" in r for r in results.values()) else "UNAVAILABLE"
        report["checks"] = {name: {key: value for key, value in result.items() if key != "evidence"}
                            for name, result in results.items()}
    except Exception:
        report.update(status="UNAVAILABLE", reason="review file unavailable or malformed")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--security-id", required=True, help="independently verified instrument ID")
    parser.add_argument("--at", type=aware_time, help="explicit historical replay time; otherwise use now")
    args = parser.parse_args(argv)
    result = check(args.file, args.symbol, args.security_id,
                   args.at or datetime.now(timezone.utc), replay=args.at is not None)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "EVALUATED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
