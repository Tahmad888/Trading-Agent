"""Indexed Greek calculations for analysis, never executable prices or stop valuations.

dxFeed IndexedEvent/Greeks govern transactions, snapshots and index encoding.
No Greek age threshold has been approved: expose age, never claim fresh eligibility.
The current getter requires the same live generation and healthy resolved option.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from threading import RLock

from desk.option_conventions import ConventionError, normalize_greeks, value_of
from desk.tastytrade_quotes import QuoteUnavailable, aware, event_time

TX, REMOVE, BEGIN, END, SNIP, MODE = 1, 2, 4, 8, 16, 64
MAX_SEQUENCE = (1 << 22) - 1
FIELDS = ("eventType", "eventSymbol", "eventFlags", "index", "time", "sequence")
VALUES = ("price", "volatility", "delta", "gamma", "theta", "rho", "vega")


@dataclass
class _State:
    identity_digest: str
    identity_epoch: int
    rows: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    snapshot: bool = False
    replacement: bool = False
    truncated: bool = False
    removed_head: int | None = None
    reason: str | None = None
    last_receipt: datetime | None = None
    requires_snapshot: bool = False
    highest_index: int | None = None
    await_boundary: bool = False
    await_snapshot_end: bool = False

    def reset(self, reason="GREEKS_NOT_RECEIVED", *, identity_digest=None, identity_epoch=None):
        """Discard values, retaining only same-stream recovery metadata."""
        same_identity = identity_digest is None or identity_digest == self.identity_digest
        return _State(
            self.identity_digest if identity_digest is None else identity_digest,
            self.identity_epoch if identity_epoch is None else identity_epoch,
            reason=(self.reason or "GREEK_SNAPSHOT_TRUNCATED") if
                   (self.requires_snapshot or self.truncated) else reason,
            last_receipt=self.last_receipt,
            requires_snapshot=self.requires_snapshot or self.truncated,
            highest_index=self.highest_index if same_identity else None,
            await_boundary=self.await_boundary or bool(self.pending or self.snapshot),
            await_snapshot_end=self.await_snapshot_end or self.snapshot)


def indexed_time(row, received):
    """Exact JSONLong decoding; never route a 64-bit index through float."""
    index, sequence = row.get("index"), row.get("sequence")
    if type(index) is not int or not 0 <= index < 1 << 63:
        raise QuoteUnavailable("GREEK_INDEX_INVALID")
    if type(sequence) is not int or not 0 <= sequence <= MAX_SEQUENCE:
        raise QuoteUnavailable("GREEK_SEQUENCE_INVALID")
    millis = (index >> 22) & 0x3ff
    encoded_time = (index >> 32) * 1000 + millis
    if (millis > 999 or row.get("time") != encoded_time or type(row.get("time")) is not int
            or (index & MAX_SEQUENCE) != sequence):
        raise QuoteUnavailable("GREEK_INDEX_TIME_MISMATCH")
    return index, event_time(encoded_time, received)


class GreekState:
    """One measurement-channel generation, per-symbol transactional state.

    Bounds protect memory, not trading eligibility. Exceeding them withholds the
    symbol instead of silently evicting records and certifying incomplete history.
    """
    def __init__(self, service, *, max_entries=4096):
        if type(max_entries) is not int or not 1 <= max_entries <= 100000:
            raise QuoteUnavailable("GREEK_STATE_BOUND_INVALID")
        self.service, self.max_entries = service, max_entries
        self._lock = RLock()
        self.generation, self.ready, self.reason = "", False, "NO_GREEK_SCHEMA"
        self._states = {}

    def begin(self, generation):
        with self._lock:
            self.generation, self.ready, self.reason = generation, False, "NO_GREEK_SCHEMA"
            self._states.clear()

    def schema(self, *, changed=False):
        with self._lock:
            if changed:
                self._states = {symbol: state.reset() for symbol, state in self._states.items()}
            self.ready, self.reason = True, "WAITING_FOR_GREEKS"

    def down(self, reason):
        with self._lock:
            self.ready, self.reason = False, reason
            self._states = {symbol: state.reset(reason) for symbol, state in self._states.items()}

    def _withdrawn_state(self, symbol, reason):
        """Allocate only recovery metadata for a previously resolved option.

        The registered identity is not treated as healthy here. A healthy
        identity_state check remains mandatory before accepting any calculation.
        Epoch -1 ensures the first healthy event rebinds this metadata.
        """
        identity = self.service.identities.get(symbol)
        if identity is None or identity.kind != "Equity Option":
            return None
        state = self._states.get(symbol)
        state = (state.reset(reason) if state is not None else
                 _State(identity.identity_digest, -1, reason=reason))
        self._states[symbol] = state
        return state

    def unknown_gap(self, symbol=None, *, reason="GREEK_LOST_BOUNDARY_UNKNOWN"):
        """Malformed/undecodable data may hide BEGIN; TX-clear cannot repair it.

        Isolate a resolved option when known. Otherwise the affected option cannot
        be identified, so withhold registered options without allocating strangers.
        """
        with self._lock:
            if not self.generation or self.generation != self.service.generation:
                return
            identity = self.service.identities.get(symbol)
            symbols = ((symbol,) if identity is not None and identity.kind == "Equity Option"
                       else list(self.service.identities))
            for affected in symbols:
                state = self._withdrawn_state(affected, reason)
                if state is not None:
                    state.requires_snapshot = True

    def _discarded(self, symbol, row, reason):
        """Follow decoded flags while unavailable; never retain values/indexes."""
        state = self._withdrawn_state(symbol, reason)
        if state is None:
            return
        identity = self.service.identities[symbol]
        flags = row.get("eventFlags") if isinstance(row, dict) else None
        if (not isinstance(row, dict) or row.get("eventType") != "Greeks"
                or row.get("eventSymbol") != identity.streamer_symbol
                or type(flags) is not int or flags < 0
                or flags & ~(TX | REMOVE | BEGIN | END | SNIP | MODE)):
            state.requires_snapshot = True
            state.reason = "GREEK_LOST_BOUNDARY_UNKNOWN"
            return
        if flags & BEGIN:
            state.await_boundary = state.await_snapshot_end = True
        if flags & TX:
            state.await_boundary = True
        if flags & SNIP:
            state.requires_snapshot = True
            state.reason = "GREEK_SNAPSHOT_TRUNCATED"
        if state.await_snapshot_end and flags & (END | SNIP):
            state.await_snapshot_end = False
        if state.await_boundary and not state.await_snapshot_end and not flags & TX:
            state.await_boundary = False  # the decoded closing row was itself discarded

    def reject(self, symbol, reason, *, needs_snapshot=False):
        with self._lock:
            state = self._states.get(symbol)
            if (state is None and needs_snapshot and self.generation
                    and self.generation == self.service.generation):
                state = self._withdrawn_state(symbol, reason)
            if state is not None:
                state.requires_snapshot |= bool(state.pending or state.snapshot or needs_snapshot)
                state.rows.clear()
                state.pending.clear()
                state.snapshot = state.replacement = state.truncated = False
                state.reason = reason

    def feed(self, symbol, row, received):
        with self._lock:
            try:
                received = aware(received)
                if self.generation != self.service.generation or not self.generation:
                    return False  # do not attach old-session loss to a new generation
                if not self.ready:
                    self._discarded(symbol, row, self.reason)
                    return False
                try:
                    identity, identity_epoch = self.service.identity_state(symbol, received)
                except QuoteUnavailable as exc:
                    self._discarded(symbol, row, str(exc))
                    return False
                if not isinstance(row, dict):
                    self.unknown_gap(symbol, reason="GREEK_ROW_INVALID")
                    return False
                if row.get("eventType") != "Greeks":
                    self.unknown_gap(symbol, reason="GREEK_EVENT_TYPE_MISMATCH")
                    return False
                if identity.kind != "Equity Option" or row.get("eventSymbol") != identity.streamer_symbol:
                    self.unknown_gap(symbol, reason="GREEK_IDENTITY_MISMATCH")
                    return False
                state = self._states.get(symbol)
                if state is None:
                    state = self._states[symbol] = _State(identity.identity_digest, identity_epoch)
                elif state.identity_digest != identity.identity_digest or state.identity_epoch != identity_epoch:
                    state = self._states[symbol] = state.reset(
                        identity_digest=identity.identity_digest, identity_epoch=identity_epoch)
                if state.last_receipt is not None and received < state.last_receipt:
                    raise QuoteUnavailable("GREEK_RECEIPT_MOVED_BACKWARDS")
                state.last_receipt = received
                flags = row.get("eventFlags")
                if type(flags) is not int or flags < 0 or flags & ~(TX | REMOVE | BEGIN | END | SNIP | MODE):
                    self.unknown_gap(symbol, reason="GREEK_FLAGS_INVALID")
                    return False
                if type(row.get("index")) is not int or not 0 <= row["index"] < 1 << 63:
                    raise QuoteUnavailable("GREEK_INDEX_INVALID")
                if state.requires_snapshot and not flags & BEGIN:
                    return False  # a tail cannot repair a corrupt multi-event update
                if state.await_boundary and not flags & BEGIN:
                    if flags & SNIP:
                        state.requires_snapshot = True
                        state.reason = "GREEK_SNAPSHOT_TRUNCATED"
                        return False
                    if state.await_snapshot_end and flags & (END | SNIP):
                        state.await_snapshot_end = False
                    if not state.await_snapshot_end and not flags & TX:
                        state.await_boundary = False
                        state.reason = "GREEK_RESET_BOUNDARY_DISCARDED"
                    else:
                        state.reason = "GREEK_RESET_TRANSACTION_INCOMPLETE"
                    return False  # discard the whole lost transaction, closing row included
                # A removal/control marker has no calculation values. Its index
                # still identifies the record to remove; no zero timestamp is fabricated.
                if flags & REMOVE:
                    operation = (row["index"], None)
                else:
                    index, source = indexed_time(row, received)
                    operation = (index, dict(raw={k: row.get(k) for k in VALUES},
                                             index=index, sequence=row["sequence"],
                                             source_at=source, received_at=received))
                if flags & BEGIN:
                    state.pending.clear()  # discard residues of overlapping snapshots
                    state.snapshot, state.replacement, state.truncated = True, True, False
                    state.requires_snapshot = False
                    state.await_boundary = False
                    state.await_snapshot_end = False
                if state.snapshot and flags & (END | SNIP):
                    state.snapshot = False
                    state.truncated = bool(flags & SNIP)
                state.reason = None
                state.pending.append(operation)
                if len(state.pending) > self.max_entries:
                    raise QuoteUnavailable("GREEK_STATE_BOUND_EXCEEDED")
                if state.snapshot or flags & TX:
                    return False
                rows = {} if state.replacement else dict(state.rows)
                removed = None if state.replacement else state.removed_head
                for index, value in state.pending:
                    if value is None:
                        if rows and index == max(rows):
                            removed = index if removed is None else max(removed, index)
                        rows.pop(index, None)
                    else:
                        rows[index] = value  # same-index corrections replace atomically
                        if removed is not None and index >= removed:
                            removed = None
                if len(rows) > self.max_entries:
                    raise QuoteUnavailable("GREEK_STATE_BOUND_EXCEEDED")
                newest = max(rows, default=None)
                seen = [index for index, value in state.pending if value is not None]
                if newest is not None:
                    seen.append(newest)
                if state.replacement and not state.truncated:
                    state.highest_index = max(seen, default=None)  # full replacement is authoritative
                elif seen:
                    state.highest_index = max(state.highest_index or 0, max(seen))
                state.rows, state.removed_head = rows, removed
                state.pending.clear()
                state.replacement, state.reason = False, None
                return True
            except QuoteUnavailable as exc:
                flag = row.get("eventFlags") if isinstance(row, dict) else None
                self.reject(symbol, str(exc), needs_snapshot=type(flag) is int and flag >= 0
                            and bool(flag & (BEGIN | TX)))
                return False

    def current(self, symbol, at):
        with self._lock:
            at = aware(at)
            if not self.ready or self.generation != self.service.generation:
                raise QuoteUnavailable(self.reason if not self.ready else "GREEK_SESSION_CHANGED")
            try:
                identity, identity_epoch = self.service.identity_state(symbol, at)
            except QuoteUnavailable as exc:
                state = self._states.get(symbol)
                if state is not None:
                    self._states[symbol] = state.reset(str(exc))
                raise
            state = self._states.get(symbol)
            if state is None:
                raise QuoteUnavailable("GREEKS_NOT_RECEIVED")
            if state.identity_digest != identity.identity_digest or state.identity_epoch != identity_epoch:
                raise QuoteUnavailable("GREEK_IDENTITY_CHANGED")
            if state.reason:
                raise QuoteUnavailable(state.reason)
            if state.snapshot or state.pending:
                raise QuoteUnavailable("GREEK_TRANSACTION_INCOMPLETE")
            if state.truncated:
                raise QuoteUnavailable("GREEK_SNAPSHOT_TRUNCATED")
            if state.removed_head is not None:
                raise QuoteUnavailable("LATEST_GREEK_CALCULATION_REMOVED")
            if not state.rows:
                raise QuoteUnavailable("GREEKS_EMPTY")
            row = state.rows[max(state.rows)]  # calculation time/sequence, not arrival order
            if state.highest_index is not None and row["index"] < state.highest_index:
                raise QuoteUnavailable("GREEK_NEWER_CALCULATION_REQUIRED")
            if row["source_at"] > at or row["received_at"] > at:
                raise QuoteUnavailable("GREEK_CURRENT_CLOCK_INVALID")
            fields = normalize_greeks("tastytrade", "dxlink", row["raw"])
            # price/IV are separately identified input fields, not executable quotes.
            inputs = {}
            for name in ("price", "volatility"):
                try:
                    value = value_of(row["raw"][name])
                    if value < 0:
                        raise ConventionError("INVALID_NUMBER")
                    inputs[name] = dict(state="VALUE", value=str(value))
                except ConventionError as exc:
                    inputs[name] = dict(state=str(exc))
            partial = any(f["state"] != "VALUE" for f in fields["fields"].values()) or any(
                f["state"] != "VALUE" for f in inputs.values())
            return dict(status="PARTIAL_CALCULATION" if partial else "CURRENT_CALCULATION",
                        source="tastytrade-dxlink-greeks", environment=self.service.environment,
                        generation=self.generation, identity=identity.capture(),
                        index=row["index"], sequence=row["sequence"],
                        source_at=row["source_at"].isoformat(), received_at=row["received_at"].isoformat(),
                        checked_at=at.isoformat(),
                        age_seconds=str(Decimal((at - row["source_at"]) // timedelta(microseconds=1)) / 1000000),
                        receipt_age_seconds=str(Decimal((row["received_at"] - row["source_at"]) //
                                                       timedelta(microseconds=1)) / 1000000),
                        freshness_policy="NO_APPROVED_GREEK_AGE_LIMIT", fields=fields["fields"], inputs=inputs,
                        coverage="LATEST_INDEXED_CALCULATION_ONLY", decision_eligibility="NOT_ATTESTED",
                        position_exposure="NOT_COMPUTED", stop_valuation="NOT_COMPUTED")

    def report(self, at):
        checks = {}
        for symbol, identity in list(self.service.identities.items()):
            if identity.kind != "Equity Option":
                continue
            try:
                checks[symbol] = self.current(symbol, at)
            except QuoteUnavailable as exc:
                checks[symbol] = dict(status="UNAVAILABLE", reason=str(exc))
        return dict(purpose="indexed Greek analysis state; no quote, ticket or valuation eligibility",
                    generation=self.generation, checked_at=aware(at).isoformat(), checks=checks)
