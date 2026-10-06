"""Timestamped REST quote observations; no ticket/source eligibility integration.

A provider quote-update time is distinct from DXLink bid/ask side-change times.
This producer is stateless: failure cannot restore a cached earlier observation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import re

from desk.tastytrade_quotes import Instrument, QuoteUnavailable, UTC, aware, canonical, number

SOURCE = "tastytrade-rest-snapshot"
TIME_MEANING = "PROVIDER_QUOTE_LAST_UPDATE"
ALIASES = {"instrument-type": "instrumentType", "updated-at": "updatedAt",
           "bid-size": "bidSize", "ask-size": "askSize", "is-trading-halted": "tradingHalted"}


def field(row, key):
    other = ALIASES.get(key)
    if other in row and key in row and (type(row[key]) is not type(row[other]) or row[key] != row[other]):
        raise QuoteUnavailable("SNAPSHOT_ALIAS_CONFLICT")
    return row.get(key, row.get(other))


def update_time(value, received):
    # ISO-8601 with an explicit timezone, never the request/receipt clock.
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise QuoteUnavailable("SNAPSHOT_TIME_INVALID")
    try:
        at = aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        raise QuoteUnavailable("SNAPSHOT_TIME_INVALID") from None
    if at <= datetime(1970, 1, 1, tzinfo=UTC):
        raise QuoteUnavailable("SNAPSHOT_TIME_UNAVAILABLE")
    if at > aware(received):
        raise QuoteUnavailable("SNAPSHOT_TIME_FUTURE")
    return at


@dataclass(frozen=True)
class SnapshotQuote:
    identity: Instrument
    environment: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    updated_at: datetime
    requested_at: datetime
    received_at: datetime
    halted: bool | None
    wire_time_field: str
    evidence_digest: str

    def view(self, now, max_age: timedelta):
        now = aware(now)
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise QuoteUnavailable("SNAPSHOT_AGE_POLICY_INVALID")
        age = now - self.updated_at
        return dict(symbol=self.identity.symbol, provider_symbol=self.identity.provider_symbol,
                    instrument_id=self.identity.instrument_id, kind=self.identity.kind,
                    identity_digest=self.identity.identity_digest, source=SOURCE, environment=self.environment,
                    bid=str(self.bid), ask=str(self.ask), bid_size=str(self.bid_size), ask_size=str(self.ask_size),
                    size_units="PROVIDER_UNITS_NOT_ATTESTED", quote_updated_at=self.updated_at.isoformat(),
                    timestamp_meaning=TIME_MEANING, wire_time_field=self.wire_time_field,
                    side_change_times="NOT_PROVIDED_NOT_INFERRED", requested_at=self.requested_at.isoformat(),
                    received_at=self.received_at.isoformat(), checked_at=now.isoformat(),
                    request_duration_seconds=(self.received_at - self.requested_at).total_seconds(),
                    quote_update_age_seconds=age.total_seconds(),
                    age_policy_seconds=max_age.total_seconds(),
                    age_status="WITHIN_EXISTING_QUOTE_POLICY" if timedelta(0) <= age <= max_age
                               else "FUTURE_AT_CHECK" if age < timedelta(0) else "OUTSIDE_EXISTING_QUOTE_POLICY",
                    trading_status="HALTED" if self.halted else "ACTIVE" if self.halted is False else "UNKNOWN",
                    coverage="NOT_ATTESTED", evidence_digest=self.evidence_digest,
                    status="NORMALIZED_OBSERVATION", decision_eligibility="NOT_EVALUATED")


def normalize(row, identity, environment, sent, received):
    sent, received = aware(sent), aware(received)
    if environment not in {"production", "sandbox"} or received < sent:
        raise QuoteUnavailable("SNAPSHOT_CONTEXT_INVALID")
    if not isinstance(row, dict) or not isinstance(identity, Instrument):
        raise QuoteUnavailable("SNAPSHOT_ROW_INVALID")
    if (canonical(row.get("symbol"), identity.kind) != identity.symbol
            or field(row, "instrument-type") != identity.kind):
        raise QuoteUnavailable("SNAPSHOT_IDENTITY_MISMATCH")
    nested = row.get("instrument")
    if nested is not None and (not isinstance(nested, dict)
            or canonical(nested.get("symbol"), identity.kind) != identity.symbol
            or field(nested, "instrument-type") != identity.kind):
        raise QuoteUnavailable("SNAPSHOT_IDENTITY_MISMATCH")
    updated = update_time(field(row, "updated-at"), received)
    size_values = (field(row, "bid-size"), field(row, "ask-size"))
    try:
        bid, ask = (number(row.get(k), positive=True) for k in ("bid", "ask"))
        bs, az = (number(value, positive=True) for value in size_values)
    except QuoteUnavailable:
        raise QuoteUnavailable("SNAPSHOT_PRICE_OR_SIZE_INVALID") from None
    if bid >= ask:
        raise QuoteUnavailable("SNAPSHOT_LOCKED" if bid == ask else "SNAPSHOT_CROSSED")
    halted = field(row, "is-trading-halted")
    if halted is not None and type(halted) is not bool:
        raise QuoteUnavailable("SNAPSHOT_HALT_INVALID")
    # The identity digest and environment bind the capture; it is not a reviewed
    # Webull↔tastytrade mapping or an approval proof.
    payload = dict(source=SOURCE, environment=environment, identity=identity.identity_digest,
                   bid=str(bid), ask=str(ask), bid_size=str(bs), ask_size=str(az),
                   updated_at=updated.isoformat(), requested_at=sent.isoformat(), received_at=received.isoformat(),
                   halted=halted, timestamp_meaning=TIME_MEANING)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    wire = "updated-at" if "updated-at" in row else "updatedAt"
    return SnapshotQuote(identity, environment, bid, ask, bs, az, updated, sent, received, halted, wire, digest)


def normalize_batch(rows, identities, environment, sent, received):
    """Match by the requested identity's kind, then validate the returned type.

    Unexpected/unmappable rows are counted without storing arbitrary provider text.
    Duplicates withhold only the affected symbol; a malformed peer never wins.
    """
    if not isinstance(rows, list):
        raise QuoteUnavailable("SNAPSHOT_REPLY_INVALID")
    expected = {i.symbol: i for i in identities}
    if len(expected) != len(identities):
        raise QuoteUnavailable("SNAPSHOT_REQUEST_INVALID")
    grouped = {s: [] for s in expected}
    unexpected = 0
    for row in rows:
        matches = []
        if isinstance(row, dict):
            for symbol, identity in expected.items():
                try:
                    if canonical(row.get("symbol"), identity.kind) == symbol:
                        matches.append(symbol)
                except QuoteUnavailable:
                    pass
        if len(matches) != 1:
            unexpected += 1
        else:
            grouped[matches[0]].append(row)
    values, failures = {}, {}
    for symbol, matches in grouped.items():
        if len(matches) != 1:
            failures[symbol] = "SNAPSHOT_DUPLICATE" if matches else "SNAPSHOT_MISSING"
            continue
        try:
            values[symbol] = normalize(matches[0], expected[symbol], environment, sent, received)
        except QuoteUnavailable as exc:
            failures[symbol] = str(exc)
    return values, failures, unexpected
