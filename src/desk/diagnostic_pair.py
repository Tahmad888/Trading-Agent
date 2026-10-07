"""Shared option selection for read-only diagnostics, never identity approval or sizing.

A selection file is untrusted input. Both diagnostics re-check provider metadata;
this module only prevents independent nearest-strike selection from changing the pair.
"""
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from desk.tastytrade_quotes import QuoteUnavailable, aware, canonical

SCHEMA = "diagnostic-option-pair/1"


def occ_terms(symbol: str) -> dict:
    symbol = canonical(symbol, "Equity Option")
    match = re.fullmatch(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})", symbol)
    if not match:
        raise QuoteUnavailable("OPTION_IDENTITY_INVALID")
    try:
        expiry = datetime.strptime(match[2], "%y%m%d").date().isoformat()
    except ValueError:
        raise QuoteUnavailable("OPTION_IDENTITY_INVALID") from None
    return dict(underlying=match[1], expiry=expiry, right="call" if match[3] == "C" else "put",
                strike=Decimal(match[4]) / 1000)


def contract_size(value):
    """A reported integer only; unknown remains unknown and no default is supplied."""
    if isinstance(value, str) and re.fullmatch(r"\d{1,6}", value):
        value = int(value)
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise QuoteUnavailable("OPTION_PAIR_SIZE_INVALID")
    return value


def validate_pair(data: dict, *, environment: str, now: datetime) -> dict:
    from desk.calendar import clock, session, trading_day
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise QuoteUnavailable("OPTION_PAIR_FILE_INVALID")
    if environment not in {"production", "sandbox"} or data.get("environment") != environment:
        raise QuoteUnavailable("OPTION_PAIR_ENVIRONMENT_MISMATCH")
    try:
        selected_at = aware(datetime.fromisoformat(data["selected_at"]))
        now = aware(now)
        if selected_at > now:
            raise QuoteUnavailable("OPTION_PAIR_SELECTION_IN_FUTURE")
        if clock(selected_at).date() != clock(now).date():
            raise QuoteUnavailable("OPTION_PAIR_NOT_CURRENT_SESSION")
        underlying = canonical(data["underlying"], "Equity")
        expiry = date.fromisoformat(data["expiry"])
        if not isinstance(data["strike"], str):
            raise ValueError
        strike = Decimal(data["strike"])
        if not strike.is_finite() or strike <= 0:
            raise ValueError
        today = clock(now).date()
        if expiry < today or (expiry == today and (not trading_day(today) or clock(now) >= session(today)[1])):
            raise QuoteUnavailable("OPTION_PAIR_EXPIRED")
        rows = {}
        for right in ("call", "put"):
            row = data[right]
            symbol = canonical(row["symbol"], "Equity Option")
            terms = occ_terms(symbol)
            if (terms["underlying"] != underlying or terms["expiry"] != expiry.isoformat()
                    or terms["strike"] != strike or terms["right"] != right):
                raise QuoteUnavailable("OPTION_PAIR_IDENTITY_MISMATCH")
            rows[right] = dict(symbol=symbol, contract_size=contract_size(row.get("contract_size")))
    except QuoteUnavailable:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise QuoteUnavailable("OPTION_PAIR_FILE_INVALID") from None
    sizes = [row["contract_size"] for row in rows.values() if row["contract_size"] is not None]
    if len(set(sizes)) > 1:
        raise QuoteUnavailable("OPTION_PAIR_TERMS_CONFLICT")
    result = dict(schema=SCHEMA, environment=environment, underlying=underlying, expiry=expiry.isoformat(),
                  strike=str(strike), selected_at=selected_at.isoformat(), **rows)
    result["selection_id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    result["note"] = "Diagnostic input, rechecked at each provider; not an approved mapping or ContractBook."
    return result


def make_pair(selection, terms, *, environment, now):
    data = dict(schema=SCHEMA, environment=environment, underlying=occ_terms(selection["call"])["underlying"],
                expiry=selection["expiry"], strike=selection["strike"], selected_at=aware(now).isoformat())
    for right in ("call", "put"):
        symbol = selection[right]
        data[right] = dict(symbol=symbol, contract_size=terms.get(symbol, {}).get("contract_size"))
    return validate_pair(data, environment=environment, now=now)


def load_pair(path: Path, *, environment, now):
    try:
        if path.stat().st_size > 65536:
            raise ValueError
        data = json.loads(path.read_text())
        return validate_pair(data.get("option_pair", data), environment=environment, now=now)
    except QuoteUnavailable:
        raise
    except (OSError, TypeError, ValueError, AttributeError):
        raise QuoteUnavailable("OPTION_PAIR_FILE_INVALID") from None


def size_match(expected, observed, *, standard_root=True):
    """Reported size consistency only; deliverables and premium units are not certified."""
    try:
        observed = contract_size(observed)
    except QuoteUnavailable:
        return dict(status="CONFLICT", reason="INVALID_PROVIDER_SIZE", expected=expected, observed=None)
    status = ("CONFLICT" if not standard_root or observed is not None and expected is not None and observed != expected
              else "INCOMPLETE" if expected is None or observed is None else "MATCHED_REPORTED_FIELDS")
    return dict(status=status, expected=expected, observed=observed,
                full_deliverable="NOT_ATTESTED", note="Reported size fields only; no multiplier assumption.")
