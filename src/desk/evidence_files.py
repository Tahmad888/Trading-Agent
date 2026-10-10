"""Expected evidence files for the host command scripts: present, readable, right shape.

Used by docs/TASTYTRADE_QUOTES.md (Part 3) and tools/g5_tradier_comparison.sh
(G5 backlog R4). A command's exit status and its file's state are reported separately;
neither implies the other. A VALID file is collected evidence, never a live-data PASS.
States: VALID, MISSING, MALFORMED, and for a host-clock file UNAVAILABLE (a well-formed
record that no measurement was made). Only a VALID clock file is passed to captures.
"""
import json
from pathlib import Path
import sys

from desk.quote_measure import load_host_clock

KINDS = ("report", "clock", "selection")


def state(path, kind="report"):
    if kind not in KINDS:
        raise ValueError("unknown evidence kind")
    path = Path(path)
    if not path.is_file():
        return "MISSING"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return "MALFORMED"
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        return "MALFORMED"
    if kind == "clock":
        clock = load_host_clock(path)
        if clock["status"] == "MEASURED":
            return "VALID"
        return "MALFORMED" if clock.get("code", "").startswith("HOST_CLOCK_FILE_") else "UNAVAILABLE"
    if kind == "selection":
        pair = data.get("option_pair")
        if not (isinstance(pair, dict) and isinstance(pair.get("selection_id"), str) and pair["selection_id"]
                and all(isinstance(pair.get(right), dict) and isinstance(pair[right].get("symbol"), str)
                        for right in ("call", "put"))):
            return "MALFORMED"
    return "VALID"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in KINDS:
        print("usage: python -m desk.evidence_files {report|clock|selection} FILE", file=sys.stderr)
        return 2
    print(state(argv[1], argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
