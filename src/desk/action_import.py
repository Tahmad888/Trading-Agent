"""Publish paired dated --include-records reports to the corporate-action ledger.

Offline import, no API requests. Operator-reviewed mapping/coverage required.
Keep review/ledger/channel files outside Git. Import receipt times are preserved.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from desk.action_ledger import ActionLedger, SecurityReview, snapshot_from_report
from desk.bars import BarDataError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True)
    parser.add_argument("--splits", required=True)
    parser.add_argument("--dividends", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--accept-removals", action="store_true",
                        help="only after verifying removed actions/corrected dates with source evidence")
    args = parser.parse_args(argv)
    ledger = review = None
    try:
        review = SecurityReview.model_validate_json(Path(args.review).read_text())
        ledger = ActionLedger(args.database)
        splits = snapshot_from_report(json.loads(Path(args.splits).read_text()))
        dividends = snapshot_from_report(json.loads(Path(args.dividends).read_text()))
        result = ledger.publish(review, splits, dividends, datetime.now(timezone.utc),
                                accept_removals=args.accept_removals)
    except (OSError, ValueError, TypeError, BarDataError):
        if ledger is not None and review is not None:
            ledger.unavailable(review.symbol)
        print(json.dumps({"status": "UNAVAILABLE", "reason": "IMPORT_FAILED"}))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
