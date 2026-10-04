"""Explicit, validated history scopes (G5a checkpoint 3). Serves the watch step.

Only the weekly leader build (discovery) has a proven finite daily dependency: the
existing 260-observation gate, returns at lags 21/63/126, the weighted proxy at lag
252 (253 rows), SMA200 21 sessions back (221 rows), the 252-session high/low and RS
window on matched SPY dates, and the 50 final SIP liquidity sessions. Every other
consumer keeps its full request: recursive indicators (EMA, Wilder RSI/ATR/ADX) and
shape searches whose range depends on supplied history are not proven by a window.

A scope is never inferred from a row count. It binds consumer, policy, instrument,
timeframe, completed-session cutoff and the required sessions, and its validator
recomputes the window from the policy, so a caller cannot shorten it.

Plan B: without a scope-capable adapter, discovery uses the unchanged strict
full-history path (and reports that it did).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from desk.bars import BarDataError, BarRowError, describe, parse_time, row_defect, sanitized
from desk.calendar import ET, latest_closed_session, session, sessions, trading_day

DISCOVERY_CONSUMER = "weekly-leader-discovery"
DISCOVERY_POLICY = "discovery-history-v1"
FULL_HISTORY = "full-history"
DISCOVERY_SESSIONS = 260     # the existing leader_scan gate; covers every discovery read (see docstring)
LIQUIDITY_SESSIONS = 50      # watchlist.VOLUME_AVG_BARS: final SIP sessions read by the liquidity rule
MARGIN_SESSIONS = 5          # requested before the window: proves a missing first session is a gap
MAX_DEFECTS = 20             # stored per ticker per response; the total is counted


class ScopeUnsupported(BarDataError):
    """The adapter cannot honor a history scope; nothing was requested."""


def _back(end: date, n: int) -> date:
    """The n-th exchange session counting back from ``end`` (``end`` is the first)."""
    days = sessions(end - timedelta(days=2 * n + 30), end)
    if len(days) < n or days[-1] != end:
        raise BarDataError("History scope end is not an exchange session")
    return days[-n]


def discovery_window(cutoff: date, liquidity_through: date | None = None) -> tuple[date, date]:
    """Required (start, end) sessions for discovery at a completed-session cutoff."""
    start = _back(cutoff, DISCOVERY_SESSIONS)
    if liquidity_through is not None:
        if liquidity_through > cutoff:
            raise BarDataError("Liquidity sessions end after the price cutoff")
        start = min(start, _back(liquidity_through, LIQUIDITY_SESSIONS))
    return start, cutoff


class HistoryScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    consumer: Literal["weekly-leader-discovery"]
    policy: Literal["discovery-history-v1"]
    timeframe: Literal["D"]
    symbol: Annotated[str, Field(min_length=1)]
    instrument_id: Annotated[str, Field(min_length=1)]
    cutoff_session: date
    required_start: date
    required_end: date
    liquidity_through: date | None = None

    @model_validator(mode="after")
    def proven(self):
        if not trading_day(self.cutoff_session):
            raise ValueError("Cutoff is not an exchange session")
        start, end = discovery_window(self.cutoff_session, self.liquidity_through)
        if (self.required_start, self.required_end) != (start, end):
            raise ValueError("Scope does not match the discovery policy window; it cannot be shortened")
        return self

    @property
    def scope_id(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()

    def required_sessions(self) -> list[date]:
        return sessions(self.required_start, self.required_end)


def discovery_scope(symbol: str, instrument_id: str, now, *, liquidity_through: date | None = None) -> HistoryScope:
    cutoff = latest_closed_session(now)
    start, end = discovery_window(cutoff, liquidity_through)
    return HistoryScope(consumer=DISCOVERY_CONSUMER, policy=DISCOVERY_POLICY, timeframe="D", symbol=symbol,
                        instrument_id=instrument_id, cutoff_session=cutoff, required_start=start,
                        required_end=end, liquidity_through=liquidity_through)


class ScopeWindow(BaseModel):
    """What one bounded provider request asks for (shared by a batch of one scope policy)."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    policy: Literal["discovery-history-v1"]
    required_start: date
    required_end: date
    request_start: date

    @model_validator(mode="after")
    def ordered(self):
        if not self.request_start <= self.required_start <= self.required_end:
            raise ValueError("Scope request interval is not ordered")
        return self

    @classmethod
    def for_scope(cls, scope: HistoryScope) -> "ScopeWindow":
        return cls(policy=scope.policy, required_start=scope.required_start, required_end=scope.required_end,
                   request_start=_back(scope.required_start, MARGIN_SESSIONS + 1))

    @classmethod
    def discovery(cls, now, liquidity_through: date | None = None) -> "ScopeWindow":
        start, end = discovery_window(latest_closed_session(now), liquidity_through)
        return cls(policy=DISCOVERY_POLICY, required_start=start, required_end=end,
                   request_start=_back(start, MARGIN_SESSIONS + 1))

    @property
    def count(self) -> int:
        return len(sessions(self.request_start, self.required_end))

    def start_ms(self) -> int:
        return int(session(self.request_start)[0].normalize().timestamp() * 1000)

    def end_ms(self) -> int:
        # One millisecond before the cutoff session's close: the forming bar is excluded.
        return int(session(self.required_end)[1].timestamp() * 1000) - 1

    def request(self) -> dict:
        return {"start": self.request_start.isoformat(), "count": self.count,
                "required_start": self.required_start.isoformat(), "required_end": self.required_end.isoformat()}


def classify_daily(rows: Sequence[Mapping], window: ScopeWindow, *, timestamp_unit: str | None = None):
    """Split provider rows by session before OHLCV parsing.

    Returns (required_rows, report). Rows dated outside the required interval are
    excluded and checked only for diagnostics. A timestamp that cannot be read, or a
    daily label that is not an ET session midnight, cannot prove a row is outside the
    interval: the ticker is rejected (``BarRowError``).
    """
    required, report = [], {"rows_returned": len(rows), "before": 0, "after": 0, "excluded_defects": [],
                            "excluded_defect_count": 0, "earliest": None}
    for i, row in enumerate(rows):
        try:
            stamp = parse_time(row["time"], timestamp_unit=timestamp_unit)
        except (KeyError, TypeError, ValueError, OverflowError) as e:
            defect = {"index": i, "time": None, "field": "time", "reason": f"invalid bar time: {e}",
                      "rows": len(rows), "row": sanitized(row)}
            raise BarRowError("SCOPED_TIMESTAMP_INVALID: " + describe(defect, len(rows)), defect) from None
        local = stamp.tz_convert(ET)
        if local != local.normalize():
            defect = {"index": i, "time": stamp.isoformat(), "field": "time",
                      "reason": "daily label is not an ET session date", "rows": len(rows), "row": sanitized(row)}
            raise BarRowError("SCOPED_TIMESTAMP_AMBIGUOUS: " + describe(defect, len(rows)), defect)
        day = local.date()
        report["earliest"] = min(filter(None, [report["earliest"], day.isoformat()]))
        if window.required_start <= day <= window.required_end:
            required.append(row)
            continue
        report["before" if day < window.required_start else "after"] += 1
        found = row_defect(row)
        if found:
            report["excluded_defect_count"] += 1
            if len(report["excluded_defects"]) < MAX_DEFECTS:
                report["excluded_defects"].append({"index": i, "time": stamp.isoformat(), "session": day.isoformat(),
                    "field": found[0], "reason": found[1], "status": "EXCLUDED_OUTSIDE_SCOPE",
                    "row": sanitized(row)})
    return required, report
