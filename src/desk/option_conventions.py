"""Provider-specific option field conventions (G5a CP3, Tradier preflight Step 2).

Analysis/diagnostic layer only: nothing here is read by the scanner, setups, the
quote service, risk, tickets or approval. Each provider field has its own mapping;
a definition is never transferred silently between providers or endpoints.

- Times: a provider timestamp is parsed by its (provider, field) contract. Tradier's
  naive ``greeks.updated_at`` is read as UTC only under that field's recorded
  evidence; the global parsers are unchanged. No receipt-time substitution.
- Greeks: the raw value is kept; a normalized value and its unit are exposed only
  where that source field's mapping supports it. Position exposure is
  ``raw × verified multiplier × signed contracts``, computed once.
- Sizes: raw quantities with an explicit unit status; never used in arithmetic here.

Offline command (no provider call)::

    python -m desk.option_conventions tastytrade-greeks --capture QUOTE_CHECK.json --output OUT.json
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
VERSION = "option-conventions/1"


class ConventionError(ValueError):
    """Internally generated, credential-free codes only."""


# ------------------------------------------------------------------ evidence ----

EVIDENCE = {
    "TRADIER_UPDATED_AT_UTC": dict(
        status="INFERRED",
        qualification=("Relayed vendor answer, not a current first-party schema: Tradier support, "
                       "2024-05-23, quoted on Wealth-Lab ('time is in UTC so 16:59 is about 13:00 EDT'). "
                       "Consistent with Tradier's 2025-09-15 Quotes example (updated_at 13:59:03 beside "
                       "bid_date 15:01:48Z)."),
        links=("https://www.wealth-lab.com/Discussion/New-Support-for-Option-Chains-with-Tradier-Extension-11283",
               "https://docs.tradier.com/docs/quotes")),
    "TRADIER_EPOCH_MS": dict(
        status="DOCUMENTED_EXAMPLE",
        qualification=("Current Quotes example uses epoch milliseconds; an older chain example (2021) used "
                       "seconds, so seconds-magnitude values are refused rather than rescaled."),
        links=("https://docs.tradier.com/docs/quotes",)),
    "DXFEED_EPOCH_MS": dict(
        status="DOCUMENTED", qualification="dxFeed: timestamp of this event in milliseconds.",
        links=("https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/option/Greeks.html",)),
    "DXFEED_GREEKS": dict(
        status="DOCUMENTED",
        qualification=("dxFeed: delta/gamma by underlying price; theta by a number of days to expiration; "
                       "rho by percentage interest rate; vega by percentage volatility."),
        links=("https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/option/Greeks.html",)),
    "ORATS_UPSTREAM": dict(
        status="INFERRED",
        qualification=("Tradier documents no units; its Greeks are 'courtesy of ORATS'. ORATS defines gamma per "
                       "'one dollar increase' of the underlying, theta "
                       "'for one day', vega for 'a one percent rise in the implied volatility' and rho for "
                       "'a one percent increase in interest rates' (one-minute product, another endpoint). "
                       "Calendar versus trading day is not stated. A Black-Scholes fit of Tradier's own "
                       "example corroborates the scale only."),
        links=("https://docs.tradier.com/docs/market-data", "https://orats.com/one-minute-data")),
    "ORATS_SHARED_STRIKE": dict(
        status="OBSERVED",
        qualification=("ORATS uses one delta, gamma, theta, vega, rho and phi per strike. Taz's 2026-10-07 "
                       "Tradier SPY 780 pair showed identical rho 0.0191 and phi -0.0192 on call and put, while "
                       "delta was signed per contract (0.449 / -0.551)."),
        links=("https://orats.com/blog/option-greeks-are-the-same-for-calls-and-puts",
               "https://orats.com/docs/option-scanner-api")),
    "IV_DECIMAL_OBSERVED": dict(
        status="OBSERVED",
        qualification="Decimal fractions in samples (Tradier smv_vol 0.358; dxFeed volatility 0.1087).",
        links=("https://docs.tradier.com/docs/quotes",)),
    "OPTION_SIZE_UNRESOLVED": dict(
        status="UNRESOLVED",
        qualification=("OPRA defines upstream option sizes in contracts, but Tradier's Quotes page says "
                       "'Size of bid (in hundreds)' for every quote and no Tradier option sample has been "
                       "matched to an OPRA-referenced source. Display hypothesis only."),
        links=("https://docs.tradier.com/docs/quotes", "https://docs.tradier.com/docs/streaming")),
    "TRADIER_STOCK_SIZE_SAMPLE": dict(
        status="OBSERVED",
        qualification=("Eight Tradier stock sides matched Alpaca SIP share sizes in the same UTC second on "
                       "2026-10-07 (sample, not universal encoding)."),
        links=("research/ai-trading/tradier-step2-alpaca-sip-quotes-2026-10-07/",)),
}


# -------------------------------------------------------------------- times ----

@dataclass(frozen=True)
class TimeContract:
    encoding: str        # NAIVE_UTC_TEXT | EPOCH_MS
    meaning: str
    evidence: str


TIME_FIELDS = {
    ("tradier", "greeks.updated_at"): TimeContract(
        "NAIVE_UTC_TEXT", "ORATS Greek/volatility calculation update time (quote-input time not documented)",
        "TRADIER_UPDATED_AT_UTC"),
    ("tradier", "bid_date"): TimeContract("EPOCH_MS", "time of the bid price", "TRADIER_EPOCH_MS"),
    ("tradier", "ask_date"): TimeContract("EPOCH_MS", "time of the ask price", "TRADIER_EPOCH_MS"),
    ("tradier", "trade_date"): TimeContract("EPOCH_MS", "most recent trade time", "TRADIER_EPOCH_MS"),
    ("tastytrade", "Greeks.time"): TimeContract("EPOCH_MS", "Greeks event time", "DXFEED_EPOCH_MS"),
}
NAIVE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
EXPLICIT = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
MS_FLOOR = 10 ** 11  # below this an epoch value is seconds-magnitude for any date after 1973


def et_text(at: datetime) -> str:
    return at.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S %Z")


def provider_time(provider: str, name: str, value) -> dict:
    """Parse one provider timestamp under its own field contract; raise on anything else."""
    contract = TIME_FIELDS.get((provider, name))
    if contract is None:
        raise ConventionError("TIME_FIELD_UNSUPPORTED")
    if value is None:
        raise ConventionError("TIME_MISSING")
    if contract.encoding == "EPOCH_MS":
        if isinstance(value, str) and re.fullmatch(r"\d{1,16}", value):
            value = int(value)  # saved reports may carry the digits as text
        if type(value) is not int:
            raise ConventionError("TIME_INVALID")
        if value == 0:
            raise ConventionError("TIME_UNAVAILABLE")
        if value < MS_FLOOR:
            raise ConventionError("TIME_UNIT_UNSUPPORTED")
        try:
            at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
        except OverflowError:
            raise ConventionError("TIME_INVALID") from None
        interpretation = "EPOCH_MILLISECONDS_UTC"
    else:
        if not isinstance(value, str):
            raise ConventionError("TIME_INVALID")
        if NAIVE.fullmatch(value):
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise ConventionError("TIME_INVALID") from None
            at, interpretation = parsed.replace(tzinfo=UTC), "NAIVE_TEXT_READ_AS_UTC"
        elif EXPLICIT.fullmatch(value):
            try:
                at = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                raise ConventionError("TIME_INVALID") from None
            interpretation = "EXPLICIT_OFFSET_RESPECTED"
        else:
            raise ConventionError("TIME_FORMAT_UNSUPPORTED")
        if at.year < 2000:
            raise ConventionError("TIME_UNAVAILABLE")
    evidence = EVIDENCE[contract.evidence]
    return dict(provider=provider, field=name, raw=value, utc=at.isoformat(), et=et_text(at),
                interpretation=interpretation, meaning=contract.meaning, evidence_status=evidence["status"],
                qualification=evidence["qualification"], evidence_links=list(evidence["links"]), version=VERSION)


def age_view(parsed: dict, checked_at: datetime) -> dict:
    """Age from the provider's own time; negative kept as an anomaly; no threshold applied."""
    if not isinstance(checked_at, datetime) or checked_at.utcoffset() is None:
        raise ConventionError("CHECK_TIME_INVALID")
    at = datetime.fromisoformat(parsed["utc"])
    seconds = (checked_at.astimezone(UTC) - at).total_seconds()
    return dict(checked_at=checked_at.astimezone(UTC).isoformat(), age_seconds=seconds,
                anomaly="FUTURE_AT_CHECK" if seconds < 0 else None, threshold="NONE_DEFINED")


# ------------------------------------------------------------------- numbers ----

def value_of(raw) -> Decimal:
    """A finite decimal; zero and negative values are values. Booleans are refused."""
    if raw is None:
        raise ConventionError("MISSING")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str, Decimal)):
        raise ConventionError("INVALID_TYPE")
    try:
        number = Decimal(str(raw))
    except InvalidOperation:
        raise ConventionError("INVALID_NUMBER") from None
    if not number.is_finite():
        raise ConventionError("NONFINITE")
    return number


# -------------------------------------------------------------------- Greeks ----

@dataclass(frozen=True)
class GreekField:
    meaning: str
    unit: str            # per-share unit of the raw value
    exposure: str | None  # position unit after × multiplier × contracts
    status: str          # DOCUMENTED | OBSERVED | INFERRED | RAW_ONLY
    evidence: str


SUPPORTED = {"DOCUMENTED", "OBSERVED"}
EXPOSURE_UNITS = {"delta": "share equivalents", "gamma": "share equivalents per $1 underlying move",
                  "theta": "$ per day", "vega": "$ per 1 IV percentage point", "rho": "$ per 1 rate percentage point"}

GREEKS = {
    ("tradier", "rest"): {
        "delta": GreekField("dPrice/dUnderlying, signed per contract", "per share", "delta", "OBSERVED",
                            "ORATS_SHARED_STRIKE"),
        "gamma": GreekField("dDelta for a $1 underlying move", "per share per $1", "gamma", "INFERRED",
                            "ORATS_UPSTREAM"),
        "theta": GreekField("price change for one day", "$ per share per day", "theta", "INFERRED", "ORATS_UPSTREAM"),
        "vega": GreekField("price change for 1 IV point", "$ per share per IV point", "vega", "INFERRED",
                           "ORATS_UPSTREAM"),
        "rho": GreekField("ORATS shared strike value", "unstated", None, "RAW_ONLY", "ORATS_SHARED_STRIKE"),
        "phi": GreekField("ORATS shared strike value; meaning not established", "unstated", None, "RAW_ONLY",
                          "ORATS_SHARED_STRIKE"),
    },
    ("tastytrade", "dxlink"): {
        "delta": GreekField("dPrice/dUnderlying", "per share", "delta", "DOCUMENTED", "DXFEED_GREEKS"),
        "gamma": GreekField("d2Price/dUnderlying2", "per share", "gamma", "DOCUMENTED", "DXFEED_GREEKS"),
        "theta": GreekField("dPrice/d(days to expiration)", "$ per share per day", "theta", "DOCUMENTED",
                            "DXFEED_GREEKS"),
        "vega": GreekField("dPrice/d(percentage volatility)", "$ per share per IV point", "vega", "DOCUMENTED",
                           "DXFEED_GREEKS"),
        "rho": GreekField("dPrice/d(percentage interest rate)", "$ per share per rate point", "rho", "DOCUMENTED",
                          "DXFEED_GREEKS"),
    },
}
IV_FIELDS = {
    ("tradier", "rest"): ("bid_iv", "mid_iv", "ask_iv", "smv_vol"),
    ("tastytrade", "dxlink"): ("volatility",),
}


def normalize_greeks(provider: str, endpoint: str, raw: dict) -> dict:
    """Per-field views; a bad or missing field affects only itself."""
    mapping = GREEKS.get((provider, endpoint))
    if mapping is None:
        raise ConventionError("GREEK_SOURCE_UNSUPPORTED")
    if not isinstance(raw, dict):
        raise ConventionError("GREEKS_INVALID")
    fields = {}
    for name, spec in mapping.items():
        view = dict(raw=raw.get(name), meaning=spec.meaning, unit=spec.unit, status=spec.status,
                    evidence=spec.evidence, evidence_status=EVIDENCE[spec.evidence]["status"])
        try:
            number = value_of(raw.get(name))
        except ConventionError as exc:
            fields[name] = dict(view, state=str(exc), normalized=False)
            continue
        view.update(state="VALUE", value=str(number))
        if spec.exposure is None:
            view.update(normalized=False, exposure="RAW_ONLY_UNSUPPORTED_MAPPING")
        else:
            view.update(normalized=spec.status in SUPPORTED,
                        exposure="SUPPORTED" if spec.status in SUPPORTED else "PROVISIONAL")
        fields[name] = view
    ivs = {}
    for name in IV_FIELDS[(provider, endpoint)]:
        if name not in raw:
            continue
        try:
            number = value_of(raw.get(name))
        except ConventionError as exc:
            ivs[name] = dict(raw=raw.get(name), state=str(exc))
            continue
        ivs[name] = dict(raw=raw.get(name), state="VALUE", unit="DECIMAL_FRACTION_OBSERVED",
                         display=iv_display(number, "DECIMAL_FRACTION_OBSERVED"), evidence="IV_DECIMAL_OBSERVED")
    return dict(provider=provider, endpoint=endpoint, version=VERSION, fields=fields, implied_volatility=ivs)


def iv_display(value, unit: str) -> str:
    if unit != "DECIMAL_FRACTION_OBSERVED":
        raise ConventionError("IV_UNIT_UNKNOWN")  # never guessed from magnitude
    return f"{(value_of(value) * 100).normalize():f}%"


# ---------------------------------------------------------------- contracts ----

def multiplier_from(*values) -> dict:
    """A verified premium multiplier from contract metadata; never a default.

    Every supplied value must be a positive integer and all must agree.
    """
    seen = []
    for raw in values:
        if raw is None:
            continue
        if isinstance(raw, str) and re.fullmatch(r"\d{1,6}", raw):
            raw = int(raw)
        if type(raw) is not int or raw <= 0:
            return dict(status="INVALID", value=None)
        seen.append(raw)
    if not seen:
        return dict(status="MISSING", value=None)
    if len(set(seen)) != 1:
        return dict(status="CONFLICT", value=None, values=seen)
    return dict(status="VERIFIED_FROM_METADATA", value=seen[0])


def exposure(field_view: dict, multiplier: dict, contracts: int) -> dict:
    """``raw per-share × verified multiplier × signed contracts``, once."""
    if type(contracts) is not int or contracts == 0:
        raise ConventionError("CONTRACTS_INVALID")
    if field_view.get("state") != "VALUE":
        return dict(status="UNAVAILABLE", reason=field_view.get("state"))
    if field_view.get("exposure") not in {"SUPPORTED", "PROVISIONAL"}:
        return dict(status="RAW_ONLY", reason="MAPPING_UNSUPPORTED")
    if multiplier.get("status") != "VERIFIED_FROM_METADATA":
        return dict(status="UNAVAILABLE", reason=f"MULTIPLIER_{multiplier.get('status')}")
    value = Decimal(field_view["value"]) * multiplier["value"] * contracts
    return dict(status=field_view["exposure"], value=str(value), contracts=contracts,
                multiplier=multiplier["value"], label=("PROVISIONAL: unit inferred, not provider-documented"
                                                       if field_view["exposure"] == "PROVISIONAL" else None))


def position_view(greeks: dict, multiplier: dict, contracts: int) -> dict:
    out = {}
    for name, view in greeks["fields"].items():
        result = exposure(view, multiplier, contracts)
        spec = GREEKS[(greeks["provider"], greeks["endpoint"])][name]
        if spec.exposure:
            result["unit"] = EXPOSURE_UNITS[spec.exposure]
        out[name] = result
    return out


def pair_check(call: dict, put: dict) -> dict:
    """Same-strike call/put comparison; reports values, sets no tolerance."""
    out = {"identical_fields": [], "delta_call_minus_put": None}
    for name in call["fields"]:
        a, b = call["fields"][name], put["fields"].get(name, {})
        if a.get("state") == b.get("state") == "VALUE" and Decimal(a["value"]) == Decimal(b["value"]):
            out["identical_fields"].append(name)
    a, b = call["fields"].get("delta", {}), put["fields"].get("delta", {})
    if a.get("state") == b.get("state") == "VALUE":
        out["delta_call_minus_put"] = str(Decimal(a["value"]) - Decimal(b["value"]))
        out["put_delta_sign"] = "NEGATIVE" if Decimal(b["value"]) < 0 else "NOT_NEGATIVE"
    out["note"] = ("Identical call/put values are shared strike values (ORATS), not put-specific. "
                   "No put field is transformed here.")
    return out


# --------------------------------------------------------------------- sizes ----

SIZES = {
    ("tradier", "rest", "option"): ("UNVERIFIED", "CONTRACTS_PROVISIONAL", "OPTION_SIZE_UNRESOLVED"),
    ("tradier", "stream", "option"): ("UNVERIFIED", "CONTRACTS_PROVISIONAL", "OPTION_SIZE_UNRESOLVED"),
    ("tradier", "rest", "stock"): ("OBSERVED_SAMPLE", "SHARES", "TRADIER_STOCK_SIZE_SAMPLE"),
}


def size_view(provider: str, channel: str, kind: str, raw) -> dict:
    status, unit, evidence = SIZES.get((provider, channel, kind), ("UNRESOLVED", "UNKNOWN", None))
    view = dict(raw=raw, size_unit_status=status, interpreted_unit=unit, evidence=evidence,
                arithmetic="EXCLUDED", label=("Display hypothesis only; not verified" if status == "UNVERIFIED"
                                              else None))
    try:
        number = value_of(raw)
        if number < 0 or number != number.to_integral_value():
            raise ConventionError("INVALID_NUMBER")
        view.update(state="VALUE", value=str(number))
    except ConventionError as exc:
        view["state"] = str(exc)
    return view


# ------------------------------------------------ offline tastytrade normalizer ----

def tastytrade_greek_records(capture: dict) -> dict:
    """Normalize the raw Greeks records of a ``desk.quote_check --measure`` report.

    Observations only: the recorder applies no indexed snapshot/transaction reduction,
    so nothing here is a current value. No multiplier is in the capture, so no
    position exposure is produced.
    """
    records = (((capture or {}).get("measurements") or {}).get("records")) or []
    out, failures = [], 0
    for record in records:
        if not isinstance(record, dict) or record.get("type") != "GREEKS":
            continue
        raw = {}
        for name in ("delta", "gamma", "theta", "vega", "rho", "volatility"):
            state = record.get(name) or {}
            raw[name] = state.get("value") if state.get("state") == "VALUE" else None
        source = record.get("source_time") or {}
        try:
            parsed = provider_time("tastytrade", "Greeks.time", source.get("raw"))
            received = datetime.fromisoformat(record["received_at"])
            timing = dict(parsed, receipt_age=age_view(parsed, received))
        except (ConventionError, KeyError, TypeError, ValueError) as exc:
            failures += 1
            timing = dict(state=str(exc) if isinstance(exc, ConventionError) else "TIME_RECORD_INVALID")
        out.append(dict(symbol=record.get("symbol"), generation=record.get("generation"),
                        flags=record.get("flags"), index=record.get("index"), sequence=record.get("sequence"),
                        time=timing, greeks=normalize_greeks("tastytrade", "dxlink", raw),
                        label="RAW_OBSERVATION_NORMALIZED_NOT_CURRENT"))
    return dict(purpose="offline normalization of recorded tastytrade Greeks; observations only",
                status="OBSERVATIONS_ONLY" if out else "NO_GREEKS_RECORDS", current_state="NOT_REDUCED",
                records=out, time_failures=failures, exposure="NOT_COMPUTED_MULTIPLIER_NOT_IN_CAPTURE",
                version=VERSION)


def main(argv=None) -> int:
    from desk.quote_measure import write_report
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("tastytrade-greeks", help="offline: normalize a quote_check --measure report")
    cmd.add_argument("--capture", type=Path, required=True)
    cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output exists; choose a new path (reports are never overwritten)")
    report = write_report(tastytrade_greek_records(json.loads(args.capture.read_text())), args.output)
    print(json.dumps({k: report.get(k) for k in ("status", "current_state", "time_failures")}, indent=2))
    return 0 if report.get("status") == "OBSERVATIONS_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
