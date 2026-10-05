"""Reviewed Webull ↔ tastytrade identity mappings for current quotes (child 3, F7).

A canonical ticker is not proof that two providers describe the same issuer or share
class. Webull's metadata exposes its own instrument ID, name, exchange, sub-category
and currency, but no CUSIP/FIGI (docs/evidence/step06-native-channels.md); tastytrade
exposes a CUSIP. No shared identifier exists, so this module never derives a mapping
automatically: a person reviews captured metadata from both providers once, and the
record stays valid while both bound identities stay unchanged. The quote diagnostic
never writes one.

Run: python -m desk.quote_mapping review --store PATH --symbol NVDA \
        --webull-capture metadata.json --tastytrade-capture quotes.json --reviewer NAME
     python -m desk.quote_mapping list --store PATH
Plan B: without a verified mapping the symbol's current quote is unavailable; the
historical scanner is unaffected.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from desk.tastytrade_quotes import Instrument, QuoteUnavailable, canonical

SCHEMA = "desk-quote-mappings-v1"
METHOD = "reviewed-crosswalk-v1"
Text = Annotated[str, Field(min_length=1)]


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class WebullSide(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    host: Literal["api.sandbox.webull.com", "api.webull.com"]
    symbol: Text
    instrument_id: Text
    name: Text
    exchange_code: Text
    sub_category: Literal["COMMON_STOCK", "ETF"]
    currency: Literal["USD"]


class TastytradeSide(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    environment: Literal["production", "sandbox"]
    provider_symbol: Text
    streamer_symbol: Text
    instrument_id: Text          # the identifier the quote adapter binds (CUSIP when supplied)
    cusip: Text | None = None
    description: Text
    listed_market: Text | None = None
    is_etf: bool


class QuoteMapping(BaseModel):
    """One reviewed mapping. Names and listings are supporting evidence only."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    desk_symbol: Text
    method: Literal["reviewed-crosswalk-v1"] = METHOD
    webull: WebullSide
    tastytrade: TastytradeSide
    reviewed_by: Text
    reviewed_at: AwareDatetime
    webull_capture_sha256: Text
    tastytrade_capture_sha256: Text
    mapping_digest: Text

    @staticmethod
    def semantic(desk_symbol, webull: WebullSide, tasty: TastytradeSide) -> dict:
        """Bound identity fields; review time, names and capture digests are excluded."""
        return {"desk_symbol": desk_symbol, "method": METHOD,
                "webull": {"host": webull.host, "instrument_id": webull.instrument_id, "currency": webull.currency},
                "tastytrade": {"environment": tasty.environment, "provider_symbol": tasty.provider_symbol,
                               "streamer_symbol": tasty.streamer_symbol, "instrument_id": tasty.instrument_id}}

    @model_validator(mode="after")
    def _consistent(self):
        problems = consistency(self.desk_symbol, self.webull, self.tastytrade)
        if problems:
            raise ValueError("; ".join(problems))
        if self.mapping_digest != _digest(self.semantic(self.desk_symbol, self.webull, self.tastytrade)):
            raise ValueError("mapping digest does not match the bound identities")
        return self


def consistency(desk_symbol, webull: WebullSide, tasty: TastytradeSide) -> list[str]:
    """Automatic checks a review cannot override (necessary, not sufficient)."""
    problems = []
    try:
        if canonical(webull.symbol, "Equity") != desk_symbol:
            problems.append("Webull symbol and desk symbol differ, including share class")
        if canonical(tasty.provider_symbol, "Equity") != desk_symbol:
            problems.append("tastytrade symbol and desk symbol differ, including share class")
    except QuoteUnavailable:
        problems.append("symbol is not a supported equity/share-class form")
    if (webull.sub_category == "ETF") != tasty.is_etf:
        problems.append("ETF classification differs between providers")
    if tasty.cusip is not None and tasty.instrument_id != tasty.cusip:
        problems.append("tastytrade bound identifier is not the supplied CUSIP")
    return problems


class MappingStore:
    """Reviewed records in one JSON file. Read on every verify; written atomically."""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self) -> tuple[dict[str, QuoteMapping], dict[str, str]]:
        """(valid records, per-symbol problems). One bad record never disables peers."""
        if not self.path.exists():
            return {}, {}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            raise QuoteUnavailable("QUOTE_MAPPING_STORE_UNREADABLE") from None
        if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("mappings"), list):
            raise QuoteUnavailable("QUOTE_MAPPING_STORE_UNREADABLE")
        records, problems, seen = {}, {}, {}
        for raw in data["mappings"]:
            symbol = raw.get("desk_symbol") if isinstance(raw, dict) else None
            try:
                record = QuoteMapping.model_validate(raw)
            except (ValidationError, ValueError, TypeError):
                if isinstance(symbol, str):
                    problems[symbol] = "QUOTE_MAPPING_INVALID"
                continue
            if record.desk_symbol in records or record.desk_symbol in problems:
                problems[record.desk_symbol] = "QUOTE_MAPPING_AMBIGUOUS"
                records.pop(record.desk_symbol, None)
                continue
            key = (record.tastytrade.environment, record.tastytrade.instrument_id)
            if key in seen and seen[key] != record.desk_symbol:
                problems[record.desk_symbol] = problems[seen[key]] = "QUOTE_MAPPING_AMBIGUOUS"
                records.pop(seen[key], None)
                continue
            seen[key] = record.desk_symbol
            records[record.desk_symbol] = record
        return records, problems

    def records(self) -> list[QuoteMapping]:
        return list(self._load()[0].values())

    def verify(self, symbol: str, *, price_basis: dict | None, identity: Instrument,
               environment: str) -> QuoteMapping:
        """The reviewed mapping, only if both providers' current identities match it.

        Raises QuoteUnavailable with a QUOTE_MAPPING_* code otherwise. Local file read,
        no network: safe inside the final ticket fence.
        """
        records, problems = self._load()
        if symbol in problems:
            raise QuoteUnavailable(problems[symbol])
        record = records.get(symbol)
        if record is None:
            raise QuoteUnavailable("QUOTE_MAPPING_MISSING")
        basis = price_basis if isinstance(price_basis, dict) else {}
        if not basis.get("host"):
            # Only the G3 vendor basis records which Webull host produced the signal.
            raise QuoteUnavailable("QUOTE_MAPPING_WEBULL_HOST_UNKNOWN")
        if (basis.get("host"), str(basis.get("security_id")), basis.get("currency"), basis.get("symbol")) != (
                record.webull.host, record.webull.instrument_id, record.webull.currency, symbol):
            raise QuoteUnavailable("QUOTE_MAPPING_WEBULL_MISMATCH")
        tasty = record.tastytrade
        if (environment, identity.kind, identity.symbol, identity.provider_symbol, identity.streamer_symbol,
                identity.instrument_id) != (tasty.environment, "Equity", symbol, tasty.provider_symbol,
                                            tasty.streamer_symbol, tasty.instrument_id):
            raise QuoteUnavailable("QUOTE_MAPPING_TASTYTRADE_MISMATCH")
        return record

    def add(self, record: QuoteMapping):
        """Atomic replace of the whole file; the same symbol's record is replaced."""
        records, problems = self._load()
        if record.desk_symbol in problems:
            raise QuoteUnavailable("QUOTE_MAPPING_AMBIGUOUS")
        records[record.desk_symbol] = record
        for other in records.values():
            if (other.desk_symbol != record.desk_symbol and (other.tastytrade.environment, other.tastytrade.instrument_id)
                    == (record.tastytrade.environment, record.tastytrade.instrument_id)):
                raise QuoteUnavailable("QUOTE_MAPPING_AMBIGUOUS")
        payload = {"schema": SCHEMA, "mappings": [r.model_dump(mode="json") for r in
                                                   sorted(records.values(), key=lambda r: r.desk_symbol)]}
        if self.path.is_symlink():
            raise QuoteUnavailable("QUOTE_MAPPING_STORE_SYMLINK")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".quote-mappings-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def build(symbol: str, webull_report: dict, tasty_report: dict, reviewer: str, now: datetime) -> QuoteMapping:
    """A candidate record from a `desk.metadata_check` report and a `desk.quote_check` report."""
    desk_symbol = canonical(symbol, "Equity")
    rows = [c for c in webull_report.get("checks", []) if c.get("symbol") == desk_symbol]
    captures = [c for c in tasty_report.get("identity_capture", []) if c.get("desk_symbol") == desk_symbol
                and c.get("kind") == "Equity"]
    if len(rows) != 1 or len(captures) != 1:
        raise ValueError("each capture must contain exactly one row for the symbol")
    w, t = rows[0], captures[0]
    fields = t.get("provider_fields", {})
    webull = WebullSide(host=webull_report.get("host"), symbol=w.get("symbol"), instrument_id=str(w.get("instrument_id")),
                        name=w.get("name"), exchange_code=w.get("exchange_code"), sub_category=w.get("security_type"),
                        currency=w.get("currency"))
    tasty = TastytradeSide(environment=tasty_report.get("environment"), provider_symbol=t.get("provider_symbol"),
                           streamer_symbol=t.get("streamer_symbol"), instrument_id=t.get("instrument_id"),
                           cusip=fields.get("cusip"), description=fields.get("description") or "",
                           listed_market=fields.get("listed-market"), is_etf=fields.get("is-etf"))
    return QuoteMapping(desk_symbol=desk_symbol, webull=webull, tastytrade=tasty, reviewed_by=reviewer,
                        reviewed_at=now, webull_capture_sha256=_digest(w), tastytrade_capture_sha256=_digest(t),
                        mapping_digest=_digest(QuoteMapping.semantic(desk_symbol, webull, tasty)))


def _show(record: QuoteMapping) -> str:
    w, t = record.webull, record.tastytrade
    return "\n".join([
        f"Desk symbol: {record.desk_symbol}",
        f"  Webull ({w.host}): {w.symbol}  instrument {w.instrument_id}  {w.name}  {w.exchange_code}  "
        f"{w.sub_category}  {w.currency}",
        f"  tastytrade ({t.environment}): {t.provider_symbol}  streamer {t.streamer_symbol}  CUSIP {t.cusip or '—'}  "
        f"{t.description}  {t.listed_market or '—'}  ETF={t.is_etf}",
        "Automatic checks passed (symbol/share class, ETF flag, CUSIP binding). They do not prove the issuer:",
        "confirm the names, listings and share class describe the same security."])


def main(argv=None, *, stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    parser = argparse.ArgumentParser(description="Reviewed Webull↔tastytrade quote mappings (no network).")
    parser.add_argument("--store", type=Path, default=os.environ.get("DESK_QUOTE_MAPPINGS"))
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review")
    review.add_argument("--symbol", required=True)
    review.add_argument("--webull-capture", type=Path, required=True)
    review.add_argument("--tastytrade-capture", type=Path, required=True)
    review.add_argument("--reviewer", required=True)
    sub.add_parser("list")
    args = parser.parse_args(argv)
    if args.store is None:
        parser.error("--store or DESK_QUOTE_MAPPINGS is required")
    store = MappingStore(args.store)
    if args.command == "list":
        try:
            for record in store.records():
                print(_show(record), file=stdout)
        except QuoteUnavailable as exc:
            print(str(exc), file=stdout)
            return 1
        return 0
    if not stdin.isatty():
        parser.error("Review needs an interactive terminal; a mapping is never approved automatically")
    try:
        record = build(args.symbol, json.loads(args.webull_capture.read_text()),
                       json.loads(args.tastytrade_capture.read_text()), args.reviewer, datetime.now(timezone.utc))
    except (OSError, ValueError, ValidationError, QuoteUnavailable) as exc:
        print(f"Mapping refused: {type(exc).__name__}: the captures do not describe one consistent security",
              file=stdout)
        return 1
    print(_show(record), file=stdout)
    print(f"Type 'MAP {record.desk_symbol}' to record this reviewed mapping: ", end="", file=stdout, flush=True)
    if stdin.readline().strip() != f"MAP {record.desk_symbol}":
        print("Not recorded.", file=stdout)
        return 1
    store.add(record)
    print("Recorded. No network call was made.", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
