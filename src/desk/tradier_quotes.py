"""Persistent, GET-only Tradier quote evidence. No orders, sizes or Greek arithmetic.

Captures and request outcomes are append-only. Healthy REST refreshes share a durable
health generation, unlike a dxLink connection. A failure cannot be cleared by a
request that started before it, and an unfinished newer request blocks old data.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import uuid

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from desk.diagnostic_pair import occ_terms, contract_size
from desk.instruments import _identity, canonical_symbol
from desk.quote_mapping import WebullSide
from desk.quote_secrets import credential_free
from desk.risk_terms import QuoteProvenance
from desk.tastytrade_quotes import QuoteUnavailable, aware, canonical
from desk.tradier_client import listed, wire_symbol
from desk.tradier_fields import observe_row, latest_policy_view

SOURCE = "tradier-rest"
SCHEMA = "tradier-quote-evidence-v1"


def decimal_json(value):
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise TypeError("Unsupported capture value")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class TradierSide(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    environment: str
    symbol: str = Field(min_length=1)
    type: str
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid(self):
        if self.environment not in {"production", "sandbox"} or self.type not in {"stock", "etf"}:
            raise ValueError("Unsupported quote identity")
        return self


class ReviewedMapping(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbol: str
    webull: WebullSide
    tradier: TradierSide
    reviewed_by: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    webull_capture_sha256: str = Field(min_length=1)
    tradier_capture_sha256: str = Field(min_length=1)
    method: str = "human-reviewed-reference-v1"

    @model_validator(mode="after")
    def valid(self):
        if (canonical(self.symbol, "Equity") != self.symbol or self.webull.symbol != self.symbol
                or self.tradier.symbol != wire_symbol(self.symbol)
                or (self.tradier.type == "etf") != (self.webull.sub_category == "ETF")
                or self.method != "human-reviewed-reference-v1"):
            raise ValueError("Contradictory reviewed mapping")
        return self


def requested(symbol, kind):
    if kind not in {"stock", "option"}:
        raise QuoteUnavailable("QUOTE_REQUEST_INVALID")
    name = canonical(symbol, "Equity" if kind == "stock" else "Equity Option")
    if kind == "option":
        occ_terms(name)  # malformed calendar dates stop before any broker request.
    return {"symbol": name, "wire": wire_symbol(name) if kind == "stock" else name, "kind": kind}


class QuoteStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.local = threading.local()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL, action TEXT NOT NULL, start_id INTEGER, payload TEXT);
                CREATE INDEX IF NOT EXISTS quote_events_key ON events(key,id);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
            """)
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if old is not None and old[0] != SCHEMA:
                raise QuoteUnavailable("TRADIER_STORE_SCHEMA")
            db.execute("INSERT OR IGNORE INTO meta VALUES('schema',?)", (SCHEMA,))
            db.execute("INSERT OR IGNORE INTO meta VALUES('instance',?)", (uuid.uuid4().hex,))
            db.execute("COMMIT")
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def held(self):
        if getattr(self.local, "db", None) is not None:
            yield self.local.db
            return
        with self.connection() as db:
            db.execute("BEGIN EXCLUSIVE")
            self.local.db = db
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                self.local.db = None

    @staticmethod
    def add(db, key, action, start=None, payload=None):
        text = json.dumps(payload, allow_nan=False, default=decimal_json) if payload is not None else None
        if text is not None and not credential_free(text):
            raise QuoteUnavailable("TRADIER_CAPTURE_CREDENTIAL_MATCH")
        return db.execute("INSERT INTO events(key,action,start_id,payload) VALUES(?,?,?,?)",
                          (key, action, start, text)).lastrowid

    @staticmethod
    def global_event(db, environment):
        return db.execute("SELECT * FROM events WHERE key=? ORDER BY id DESC LIMIT 1", (environment + "|*",)).fetchone()

    def resume(self, environment):
        if environment not in {"production", "sandbox"}:
            raise QuoteUnavailable("ENVIRONMENT_INVALID")
        with self.held() as db:
            self.add(db, environment + "|*", "RESUME")

    def mapping(self, record: ReviewedMapping):
        record = ReviewedMapping.model_validate(record.model_dump())
        with self.held() as db:
            self.add(db, record.tradier.environment + "|mapping|" + record.symbol,
                     "MAP", payload=record.model_dump(mode="json"))

    def revoke(self, environment, symbol):
        with self.held() as db:
            self.add(db, environment + "|mapping|" + symbol, "REVOKE")

    def review(self, db, environment, symbol, price_basis, webull, identity):
        row = db.execute("SELECT * FROM events WHERE key=? ORDER BY id DESC LIMIT 1",
                         (environment + "|mapping|" + symbol,)).fetchone()
        if row is None or row["action"] != "MAP":
            raise QuoteUnavailable("QUOTE_MAPPING_MISSING")
        record = ReviewedMapping.model_validate_json(row["payload"])
        w, t = record.webull, record.tradier
        b = price_basis or {}
        if (b.get("host"), str(b.get("security_id")), b.get("currency"), b.get("symbol")) != (
                w.host, w.instrument_id, w.currency, symbol):
            raise QuoteUnavailable("QUOTE_MAPPING_WEBULL_MISMATCH")
        if not isinstance(webull, dict) or (str(webull.get("instrument_id")), webull.get("currency"),
                                          webull.get("sub_category"), webull.get("exchange_code")) != (w.instrument_id, w.currency, w.sub_category, w.exchange_code):
            raise QuoteUnavailable("QUOTE_MAPPING_WEBULL_IDENTITY_UNAVAILABLE")
        if identity != t.model_dump():
            raise QuoteUnavailable("QUOTE_MAPPING_TRADIER_MISMATCH")
        return digest(record.model_dump(mode="json")), row["id"]

    def begin(self, environment, names):
        with self.held() as db:
            stop = self.global_event(db, environment)
            if stop and stop["action"] == "STOP":
                raise QuoteUnavailable("TRADIER_STOPPED_EXPLICIT_RESUME_REQUIRED")
            return {r["symbol"]: self.add(db, environment + "|" + r["symbol"], "START") for r in names}

    def finish(self, environment, names, starts, rows, received):
        with self.held() as db:
            for want in names:
                key = environment + "|" + want["symbol"]
                check = starts.get(want["symbol"])
                opened = db.execute("SELECT key,action FROM events WHERE id=?", (check,)).fetchone() if type(check) is int else None
                completed = db.execute("SELECT 1 FROM events WHERE start_id=? AND action IN ('OK','FAILED') LIMIT 1",
                                       (check,)).fetchone() if type(check) is int else None
                if opened is None or opened["key"] != key or opened["action"] != "START" or completed is not None:
                    raise QuoteUnavailable("TRADIER_CHECK_ID_INVALID_OR_COMPLETED")
                matches = [r for r in rows if isinstance(r, dict) and r.get("symbol") == want["wire"]]
                try:
                    if len(matches) != 1:
                        raise QuoteUnavailable("QUOTE_MISSING_OR_DUPLICATE")
                    raw = {k: v for k, v in matches[0].items() if k != "greeks"}
                    # Never retain provider error bodies, request headers or credentials.
                    view = observe_row(raw, want, received, None)
                    if want["kind"] == "stock":
                        identity = TradierSide(environment=environment, symbol=raw["symbol"], type=raw["type"],
                                               description=raw.get("description")).model_dump()
                        mapped = db.execute("SELECT action,payload FROM events WHERE key=? ORDER BY id DESC LIMIT 1",
                                            (environment + "|mapping|" + want["symbol"],)).fetchone()
                        if mapped and mapped["action"] == "MAP":
                            reviewed = ReviewedMapping.model_validate_json(mapped["payload"])
                            if identity != reviewed.tradier.model_dump():
                                raise QuoteUnavailable("TRADIER_REVIEWED_IDENTITY_CHANGED")
                    else:
                        identity = {**view["terms"], "contract_size": contract_size(raw.get("contract_size"))}
                        if identity["root_symbol"] != identity["underlying"] or identity["contract_size"] != 100:
                            # The risk engine supports verified standard 100-share contracts only.
                            # This rejects contrary/missing metadata; it never supplies a default.
                            raise QuoteUnavailable("TRADIER_CONTRACT_METADATA_UNSUPPORTED")
                    payload = {"view": view, "identity": identity}
                    policy = latest_policy_view(view, received)
                    for component in ("trade", "quote"):
                        fields = ("last",) if component == "trade" else ("bid", "ask")
                        okay = (policy[component]["verdict"] == "PASS" and all(
                            math.isfinite(float(view["prices"][name]["value"])) for name in fields))
                        self.add(db, key + "|" + component, "OK" if okay else "FAILED", starts[want["symbol"]],
                                 payload if okay else {"reason": "TRADIER_" + component.upper() + "_INVALID"})
                except (ValueError, TypeError, KeyError, ArithmeticError):
                    for component in ("trade", "quote"):
                        self.add(db, key + "|" + component, "FAILED", starts[want["symbol"]],
                                 {"reason": "TRADIER_IDENTITY_OR_ROW_INVALID"})

    def fetch(self, client, names):
        if getattr(self.local, "db", None) is not None:
            raise QuoteUnavailable("NETWORK_UNDER_QUOTE_FENCE_FORBIDDEN")
        if not names or len({r["symbol"] for r in names}) != len(names):
            raise QuoteUnavailable("QUOTE_REQUEST_INVALID")
        starts = self.begin(client.environment, names)
        try:
            sent = aware(client.clock())
            reply = client.get("quotes", {"symbols": ",".join(r["wire"] for r in names), "greeks": "false"})
            body = reply.get("quotes")
            if not isinstance(body, dict):
                raise QuoteUnavailable("REPLY_SHAPE_INVALID")
            rows = listed(body.get("quote"))
            received = aware(client.clock())
            if received < sent:
                raise QuoteUnavailable("CLOCK_MOVED_BACKWARDS")
            self.finish(client.environment, names, starts, rows, received)
        except Exception as exc:
            # Fixed code only. STOP persists across process restarts until explicit resume.
            code = str(exc) if isinstance(exc, QuoteUnavailable) else "TRADIER_FETCH_FAILURE"
            allowed = {"REST_HTTP_400", "REST_HTTP_401", "REST_HTTP_403", "REST_HTTP_429", "REST_HTTP_500",
                       "REST_HTTP_502", "REST_HTTP_503", "REST_ERROR", "REST_REQUEST_BUDGET",
                       "REST_TRANSPORT_FAILURE", "REPLY_SHAPE_INVALID", "CLOCK_MOVED_BACKWARDS"}
            code = code if code in allowed else "TRADIER_FETCH_FAILURE"
            with self.held() as db:
                self.add(db, client.environment + "|*", "STOP", payload={"reason": code})
            raise QuoteUnavailable(code) from None

    def read(self, db, environment, symbol, component, at):
        key = environment + "|" + symbol
        global_row = self.global_event(db, environment)
        if global_row and global_row["action"] == "STOP":
            raise QuoteUnavailable("TRADIER_STOPPED_EXPLICIT_RESUME_REQUIRED")
        okay = db.execute("SELECT * FROM events WHERE key=? AND action='OK' ORDER BY start_id DESC,id DESC LIMIT 1",
                          (key + "|" + component,)).fetchone()
        failure = db.execute("SELECT MAX(id) FROM events WHERE key=? AND action='FAILED'", (key + "|" + component,)).fetchone()[0] or 0
        start = db.execute("SELECT MAX(id) FROM events WHERE key=? AND action='START'", (key,)).fetchone()[0] or 0
        if (okay is None or okay["start_id"] <= max(failure, global_row["id"] if global_row else 0)
                or start > okay["start_id"]):
            raise QuoteUnavailable("TRADIER_LATEST_CHECK_UNAVAILABLE")
        data = json.loads(okay["payload"])
        if (datetime.fromisoformat(data["view"]["received_at"]) > aware(at)
                or latest_policy_view(data["view"], aware(at))[component]["verdict"] != "PASS"):
            raise QuoteUnavailable("TRADIER_" + component.upper() + "_STALE_OR_INVALID")
        instance = db.execute("SELECT value FROM meta WHERE key='instance'").fetchone()[0]
        stop_id = db.execute("SELECT MAX(id) FROM events WHERE key=? AND action='STOP'", (environment + "|*",)).fetchone()[0] or 0
        return data, digest([instance, stop_id, failure])

    def proof(self, db, environment, symbol, component, at, mapping_digest, mapping_version, contract=None):
        data, generation = self.read(db, environment, symbol, component, at)
        if contract is not None:
            _identity(contract)
            terms = occ_terms(symbol)
            ident = data["identity"]
            if (canonical_symbol(contract.symbol) != symbol or contract.underlying != terms["underlying"]
                    or contract.expiry.isoformat() != terms["expiry"] or contract.right != terms["right"]
                    or contract.strike != terms["strike"] or ident.get("root_symbol") != contract.underlying
                    or ident.get("contract_size") != contract.multiplier):
                raise QuoteUnavailable("TRADIER_CONTRACT_METADATA_MISMATCH")
            identity = digest([data["identity"], contract.model_dump(mode="json")])
        else:
            identity = digest(data["identity"])
        proof = QuoteProvenance(source=SOURCE, environment=environment, symbol=symbol, instrument_id=symbol,
                                streamer_symbol=data["view"]["wire_symbol"], identity_digest=identity,
                                generation=digest([generation, mapping_version]), mapping_digest=mapping_digest)
        return data["view"], proof


def main(argv=None, *, stdin=None, stdout=None):
    """Local store administration and one read-only quote GET. No ticket activation."""
    import argparse
    import os
    import sys
    from datetime import timezone
    from desk.tradier_client import TradierClient
    from desk.tradier_fields import write_report
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    parser = argparse.ArgumentParser(description="Tradier durable evidence; no account/order calls")
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--environment', choices=('production','sandbox'), required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    refresh = sub.add_parser('refresh')
    refresh.add_argument('--symbols', nargs='+', required=True)
    refresh.add_argument('--options', nargs='*', default=[])
    refresh.add_argument('--output', type=Path, required=True)
    review = sub.add_parser('review')
    review.add_argument('--record', type=Path, required=True)
    revoke = sub.add_parser('revoke')
    revoke.add_argument('--symbol', required=True)
    sub.add_parser('resume')
    args = parser.parse_args(argv)
    try:
        # Validate user input before creating or changing a store, and before GETs.
        record = None
        if args.command == 'review':
            record = ReviewedMapping.model_validate_json(args.record.read_text())
            if record.tradier.environment != args.environment:
                raise QuoteUnavailable('ENVIRONMENT_MISMATCH')
            if not stdin.isatty():
                raise QuoteUnavailable('INTERACTIVE_MAPPING_REVIEW_REQUIRED')
            if not credential_free(record.model_dump_json()):
                raise QuoteUnavailable('TRADIER_CAPTURE_CREDENTIAL_MATCH')
            print(record.model_dump_json(indent=2), file=stdout)
            print('No shared immutable issuer ID is supplied by the Tradier stock quote. '
                  'Review issuer and share class against the captured Webull metadata.', file=stdout)
            print(f"Type MAP {record.symbol} to record this crosswalk: ", end='', file=stdout, flush=True)
            if stdin.readline().strip() != 'MAP ' + record.symbol:
                raise QuoteUnavailable('MAPPING_REVIEW_NOT_CONFIRMED')
        elif args.command == 'refresh':
            names = [requested(s, 'stock') for s in args.symbols] + [requested(s, 'option') for s in args.options]
            if args.output.exists():
                raise QuoteUnavailable('REPORT_ALREADY_EXISTS')
            client = TradierClient(os.environ.get('TRADIER_ACCESS_TOKEN',''), environment=args.environment, max_requests=1)
        elif args.command == 'revoke':
            symbol = canonical(args.symbol,'Equity')
        else:
            if not stdin.isatty():
                raise QuoteUnavailable('INTERACTIVE_RESUME_REQUIRED')
            print('Resume does not revive old captures. Type RESUME to permit a new request: ',
                  end='', file=stdout, flush=True)
            if stdin.readline().strip() != 'RESUME':
                raise QuoteUnavailable('RESUME_NOT_CONFIRMED')
        store = QuoteStore(args.store)
        if record is not None:
            store.mapping(record)
        elif args.command == 'revoke':
            store.revoke(args.environment,symbol)
        elif args.command == 'resume':
            store.resume(args.environment)
        else:
            store.fetch(client,names)
            at = datetime.now(timezone.utc)
            checks = {}
            with store.held() as db:
                for want in names:
                    parts = {}
                    for component in ('quote','trade'):
                        try:
                            data,_ = store.read(db,args.environment,want['symbol'],component,at)
                            parts[component] = dict(status='FRESH_FIELDS',**data)
                        except QuoteUnavailable as exc:
                            parts[component] = dict(status='UNAVAILABLE',reason=str(exc))
                    checks[want['symbol']] = parts
            report = dict(purpose='read-only durable quote capture; no signal/order activation',
                          environment=args.environment,requests=client.requests,checked_at=at.isoformat(),
                          checks=checks,decision_eligibility='NOT_EVALUATED',G5='OPEN')
            report = write_report(report,args.output)
            print(json.dumps(report,indent=2),file=stdout)
            if report.get('status') == 'REPORT_REJECTED':
                return 1
            return 0 if all(checks[r['symbol']]['quote']['status']=='FRESH_FIELDS' and
                            (r['kind']=='option' or checks[r['symbol']]['trade']['status']=='FRESH_FIELDS')
                            for r in names) else 1
        print(json.dumps(dict(status='RECORDED',command=args.command,provider_calls=0,G5='OPEN')),file=stdout)
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc,QuoteUnavailable) else 'TRADIER_STORE_COMMAND_INVALID'
        print(json.dumps(dict(status='UNAVAILABLE',reason=code,G5='OPEN')),file=stdout)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
