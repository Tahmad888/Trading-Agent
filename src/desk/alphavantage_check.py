"""Bounded direct REST check. No retries, raw payloads, profiles or orders.

Default: four calls maximum. --symbols NVDA --functions SPLITS makes only one.
Exit 0 means responses parsed, never that corporate-action coverage was accepted.
"""
import argparse
import json

from desk.alphavantage import AlphaVantageActions, AlphaVantageError, FUNCTIONS


def check(source, symbols=("NVDA", "SPY"), functions=FUNCTIONS, *, include_records=False):
    report = {"purpose": "direct REST observations; no decision eligibility",
              "coverage": "UNKNOWN", "checks": [], "stopped_on_error": False}
    for symbol in symbols:
        for function in functions:
            try:
                item = source.fetch(symbol, function).report(include_records=include_records)
            except AlphaVantageError as exc:
                report["checks"].append({"symbol": symbol, "function": function,
                                         "status": "UNAVAILABLE", "reason": exc.code})
                report["stopped_on_error"] = True
                return report
            report["checks"].append(item)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", choices=("NVDA", "SPY"), default=["NVDA", "SPY"])
    parser.add_argument("--functions", nargs="+", choices=FUNCTIONS, default=list(FUNCTIONS))
    parser.add_argument("--include-records", action="store_true", help="include parsed observation fields; never raw replies")
    args = parser.parse_args(argv)
    try:
        source = AlphaVantageActions.from_env()
    except AlphaVantageError as exc:
        print(json.dumps({"status": "UNAVAILABLE", "reason": exc.code}))
        return 1
    report = check(source, tuple(dict.fromkeys(args.symbols)), tuple(dict.fromkeys(args.functions)),
                   include_records=args.include_records)
    print(json.dumps(report, indent=2))
    return 1 if report["stopped_on_error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
