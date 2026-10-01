"""Read-only split/dividend observations; no coverage or price profile is published.

Request: https://www.alphavantage.co/documentation/#dividends (also #splits).
Success schema checked against user-supplied direct REST observations, 2026-10-01.
Plan B: safe errors, no retries, no stale or incomplete evidence promotion.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
import re
from urllib import error, parse, request

from desk.bars import BarDataError

ENDPOINT = "https://www.alphavantage.co/query"
FUNCTIONS = ("SPLITS", "DIVIDENDS")
ERROR_FIELDS = ("Information", "Note", "Error Message")


class AlphaVantageError(BarDataError):
    """Only fixed diagnostic codes; never interpolate provider bodies or URLs."""
    def __init__(self, code: str):
        self.code = code
        super().__init__("Alpha Vantage: " + code)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # query contains the key; do not forward it to another URL


def _fetch(req, timeout):
    with request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
        return response.read()


def _date(value, *, optional=False):
    if optional and value in (None, "None", ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise AlphaVantageError("INVALID_EVENT_DATE")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise AlphaVantageError("INVALID_EVENT_DATE") from None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AlphaVantageError("INVALID_EVENT_VALUE")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise AlphaVantageError("INVALID_EVENT_VALUE") from None
    if not result.is_finite() or result < 0:
        raise AlphaVantageError("INVALID_EVENT_VALUE")
    return result


@dataclass(frozen=True)
class ActionObservation:
    event_date: date
    value: Decimal  # dividend amount or new shares per old share; basis unverified
    declaration_date: date | None = None
    record_date: date | None = None
    payment_date: date | None = None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionSnapshot:
    symbol: str
    function: str
    received_at: datetime
    rows: tuple[ActionObservation, ...]

    def report(self, *, include_records=False):
        dates = [row.event_date for row in self.rows]
        result = {"symbol": self.symbol, "function": self.function,
                  "received_at": self.received_at.isoformat(),
                  "status": "OBSERVATIONS_ONLY" if dates else "EMPTY_UNVERIFIED",
                  "row_count": len(self.rows), "coverage": "UNKNOWN",
                  "oldest_event": str(min(dates)) if dates else None,
                  "newest_event": str(max(dates)) if dates else None,
                  "issues": sorted({issue for row in self.rows for issue in row.issues})}
        if include_records:
            result["records"] = [
                {"event_date": row.event_date.isoformat(), "value": str(row.value),
                 "declaration_date": str(row.declaration_date) if row.declaration_date else None,
                 "record_date": str(row.record_date) if row.record_date else None,
                 "payment_date": str(row.payment_date) if row.payment_date else None,
                 "issues": list(row.issues)} for row in self.rows]
        return result


def _observations(function, rows):
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise AlphaVantageError("INVALID_DATA_ROWS")
        split = function == "SPLITS"
        event_date = _date(row.get("effective_date" if split else "ex_dividend_date"))
        value = _number(row.get("split_factor" if split else "amount"))
        if split:
            if value in (0, 1):
                raise AlphaVantageError("INVALID_SPLIT_FACTOR")
            result.append(ActionObservation(event_date, value))
        else:
            declared, recorded, paid = (_date(row.get(field), optional=True) for field in
                                        ("declaration_date", "record_date", "payment_date"))
            issues = []
            if value == 0:
                issues.append("ZERO_AMOUNT_IGNORED")
            if any(day is None for day in (declared, recorded, paid)):
                issues.append("MISSING_AUXILIARY_DATE")
            # Dates can legitimately precede/follow ex-date in unusual actions.
            # Preserve them; do not infer declaration semantics or reorder dates.
            result.append(ActionObservation(event_date, value, declared, recorded, paid, tuple(issues)))
    if len({row.event_date for row in result}) != len(result):
        raise AlphaVantageError("DUPLICATE_EVENT_DATE_REQUIRES_REVIEW")
    return tuple(sorted(result, key=lambda row: row.event_date))


class AlphaVantageActions:
    def __init__(self, api_key: str, *, transport=_fetch, timeout=15.0,
                 clock=lambda: datetime.now(timezone.utc)):
        if not isinstance(api_key, str) or not api_key.strip():
            raise AlphaVantageError("NOT_CONFIGURED")
        self._api_key = api_key.strip()
        self._transport, self._timeout, self._clock = transport, timeout, clock
        self._rate_limited = False

    @classmethod
    def from_env(cls, env=None, **kwargs):
        env = os.environ if env is None else env
        return cls(env.get("ALPHAVANTAGE_API_KEY", ""), **kwargs)

    def fetch(self, symbol: str, function: str) -> ActionSnapshot:
        if function not in FUNCTIONS or not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,19}", symbol):
            raise AlphaVantageError("INVALID_REQUEST")
        if self._rate_limited:
            raise AlphaVantageError("RATE_LIMITED")
        query = parse.urlencode(dict(function=function, symbol=symbol, datatype="json", apikey=self._api_key))
        req = request.Request(ENDPOINT + "?" + query, headers={"Accept": "application/json",
                                                              "User-Agent": "trading-desk/0.1"})
        try:
            raw = self._transport(req, self._timeout)
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 429:
                self._rate_limited = True
            raise AlphaVantageError("RATE_LIMITED" if status == 429 else "HTTP_FAILURE") from None
        except (error.URLError, TimeoutError, OSError):
            raise AlphaVantageError("TRANSPORT_FAILURE") from None
        try:
            payload = json.loads(raw, parse_float=Decimal)
        except (ValueError, TypeError):
            raise AlphaVantageError("INVALID_JSON") from None
        if not isinstance(payload, dict):
            raise AlphaVantageError("INVALID_ENVELOPE")
        if any(field in payload for field in ERROR_FIELDS):
            message = " ".join(str(payload.get(field, "")) for field in ERROR_FIELDS).lower()
            limited = any(term in message for term in ("rate limit", "call frequency", "requests per day"))
            self._rate_limited = limited
            raise AlphaVantageError("RATE_LIMITED" if limited else "PROVIDER_REJECTED")
        # Also reject unexpected credential echoes in otherwise successful bodies.
        if self._api_key in json.dumps(payload, default=str):
            raise AlphaVantageError("UNSAFE_RESPONSE")
        if payload.get("symbol") != symbol or not isinstance(payload.get("data"), list):
            raise AlphaVantageError("INVALID_ENVELOPE")
        rows = _observations(function, payload["data"])
        received_at = self._clock()
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise AlphaVantageError("INVALID_RECEIPT_CLOCK")
        return ActionSnapshot(symbol, function, received_at, rows)
