"""Diagnostic-only quote measurements (G5a CP3 live-run package 3). No orders.

Evidence for later decisions, never eligibility: nothing here is read by the risk
bridge, setups, scanner or tickets, and the quote service's acceptance rules, the
quote-age policy and the future-time refusal are unchanged.

- Bid/ask: the requested and server-accepted Quote/Trade maps and the raw state of
  ``bidTime``/``askTime`` per decoded row, with UTC receipt and decision times kept
  apart from provider source time (observed on the core channel).
- Volume and Greeks: a separate measurement FEED channel (``MEASURE_CHANNEL``) for
  Trade ``dayId``/``dayVolume`` and option Greeks; its rows never reach the service.
- Clock: ``python -m desk.quote_measure clock`` runs ``sntp`` read-only on this host.

Commands (env var NAMES only: TASTYTRADE_CLIENT_SECRET, TASTYTRADE_REFRESH_TOKEN)::

    python -m desk.quote_measure clock --output CLOCK.json
    python -m desk.quote_measure volume --environment production --symbols SPY NVDA \
        --seconds 2400 --output VOLUME.json [--host-clock CLOCK.json]
    python -m desk.quote_measure compare --capture VOLUME.json \
        --alpaca-result PROBE_DIR/result.json --output COMPARE.json

The volume capture is a separately bounded design: consecutive segments of at most
600 s each through the unchanged ``capture`` (whose 600-second bound stays), at most
``MAX_VOLUME_SECONDS`` in total. Engineering bounds, not trading rules.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess

from desk.tastytrade_quotes import CORE, FIELDS, QuoteUnavailable, canonical
from desk.tastytrade_transport import MEASURE_CHANNEL, Credentials, ReadClient, capture, utcnow

UTC = timezone.utc
# Requested on the measurement channel only; the core FIELDS are unchanged.
MEASURE_FIELDS = {
    "Trade": ("eventType", "eventSymbol", "time", "price", "size", "dayId", "dayVolume"),
    "Greeks": ("eventType", "eventSymbol", "eventFlags", "index", "time", "sequence", "price", "volatility",
               "delta", "gamma", "theta", "rho", "vega"),
}
# dayId is requested but not required: a map without it records dayId ABSENT.
MEASURE_REQUIRED = {"Trade": ("eventType", "eventSymbol", "time", "dayVolume"),
                    "Greeks": ("eventType", "eventSymbol", "time")}
MEASURE_TARGET = {"Trade": "Equity", "Greeks": "Equity Option"}
# dxFeed IndexedEvent flag bits (Greeks is indexed).
FLAGS = ((0x01, "TX_PENDING"), (0x02, "REMOVE_EVENT"), (0x04, "SNAPSHOT_BEGIN"), (0x08, "SNAPSHOT_END"),
         (0x10, "SNAPSHOT_SNIP"), (0x40, "SNAPSHOT_MODE"))
GREEK_VALUES = ("price", "volatility", "delta", "gamma", "theta", "rho", "vega")
MAX_RECORDS = 4000        # streamed records kept per record type; counters stay complete (engineering bound)
SAMPLES = 3               # first raw decoded rows kept per channel/type
SEGMENT_SECONDS = 600     # = capture's own maximum, unchanged
MAX_VOLUME_SECONDS = 3000
MAX_VOLUME_SYMBOLS = 5
from desk.quote_secrets import CREDENTIAL_NAMES, CREDENTIAL_MARKERS, credential_free
VOLUME_NOTE = ("Trade.dayVolume is the provider's day volume including regular and extended hours (dxFeed "
               "Trade documentation); the Trade time is the last regular-hours trade time and can stay fixed "
               "while day volume changes, so it is never the volume update time. Receipt times are local "
               "observation times, not exchange cutoffs. No RTH window total is derived.")
GREEKS_NOTE = ("Raw Greeks observations only, not a current validated value: no snapshot/transaction "
               "reduction is applied. Greeks.price is the option market price used for the calculation; "
               "theoretical price is a separate TheoPrice event, not requested. Presence does not prove "
               "accuracy or executable liquidity. No Greeks age cutoff exists.")


# ------------------------------------------------------------------ raw field states ----

def time_state(row: dict, name: str, received: datetime) -> dict:
    """Raw state of an epoch-millisecond field; never substituted or clamped."""
    if name not in row:
        return {"state": "ABSENT"}
    value = row[name]
    if value is None:
        return {"state": "NULL"}
    if isinstance(value, str) and value == "NaN" or isinstance(value, float) and math.isnan(value):
        return {"state": "NAN"}
    if type(value) is not int:
        return {"state": "INVALID", "type": type(value).__name__}
    if value == 0:
        return {"state": "ZERO", "raw": 0}
    if value < 0:
        return {"state": "INVALID", "raw": value}
    try:
        at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
    except OverflowError:
        return {"state": "INVALID", "raw": value}
    lead = (at - received) / timedelta(milliseconds=1)
    if lead > 0:
        return {"state": "FUTURE", "raw": value, "at": at.isoformat(), "lead_ms": lead}
    return {"state": "VALUE", "raw": value, "at": at.isoformat(), "receipt_minus_source_ms": -lead}


def number_state(row: dict, name: str) -> dict:
    """Raw state of a JSONDouble field; zero and negative values are values.

    The DXLink transport decodes JSON numbers as Decimal (``json_read``), so Decimal
    is a value type here, not a corrupt one.
    """
    if name not in row:
        return {"state": "ABSENT"}
    value = row[name]
    if value is None:
        return {"state": "NULL"}
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return {"state": "INVALID", "type": type(value).__name__}
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return {"state": "INVALID"}
    if number.is_nan():
        return {"state": "NAN"}
    if not number.is_finite():
        return {"state": "INFINITE"}
    return {"state": "VALUE", "value": str(number)}


def flags(value) -> list[str] | None:
    if type(value) is not int or value < 0:
        return None
    return [name for bit, name in FLAGS if value & bit]


# ------------------------------------------------------------------------- recorder ----

class Recorder:
    """Bounded measurement records for one diagnostic run; no current-state claims.

    ``schema``/``row`` observe the core decoder; ``configure``/``data`` decode the
    separate measurement channel without touching the quote service.
    """

    def __init__(self, service, *, clock=utcnow, max_age=None, volume=True, greeks=True, max_records=MAX_RECORDS,
                 boundaries=None):
        if max_age is None:
            from desk.risk import RiskLimits
            max_age = RiskLimits().max_quote_age
        self.service, self.clock, self.max_age, self.max_records = service, clock, max_age, max_records
        self.kinds = tuple(k for k, on in (("Trade", volume), ("Greeks", greeks)) if on)
        self.channel, self.fields, self.state = MEASURE_CHANNEL, {}, "NOT_REQUESTED"
        self.boundaries = boundaries  # optional (start, end) receipt boundaries for volume brackets
        self.schema_log: list[dict] = []
        self._schema_last: dict[tuple[int, str], str] = {}
        self.revisions: dict[tuple[int, str], int] = {}
        self.records: list[dict] = []
        self.kept: dict[str, int] = {}
        self.dropped: dict[str, int] = {}
        self.samples: dict[str, list] = {}
        self.counts: dict[str, dict] = {}
        self.generations: list[dict] = []
        self.channel_events: list[dict] = []
        self.undecodable: dict[str, int] = {}
        self.fault = None
        self._volume: dict[str, dict] = {}
        self._brackets: dict[str, dict] = {}
        self._greeks: dict[str, dict] = {}

    # ---- helpers
    def _count(self, group: str, *keys):
        node = self.counts.setdefault(group, {})
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = node.get(keys[-1], 0) + 1

    def _keep(self, record: dict):
        kind = record["type"]
        if self.kept.get(kind, 0) < self.max_records:
            self.records.append(record)
            self.kept[kind] = self.kept.get(kind, 0) + 1
        else:
            self.dropped[kind] = self.dropped.get(kind, 0) + 1

    def _sample(self, label: str, row: dict):
        rows = self.samples.setdefault(label, [])
        if len(rows) < SAMPLES:
            rows.append(dict(row))

    def _symbol(self, wire) -> str | None:
        return self.service.symbol_for(wire) if isinstance(wire, str) else None

    def failed(self, name: str):
        self.fault = f"RECORDER_FAULT:{name}"

    # ---- session lifecycle (measurement channel)
    def begin(self, generation: str, at: datetime):
        self.fields.clear()  # a new connection needs a new accepted map
        self.state = "NOT_REQUESTED"
        self.generations.append({"generation": generation, "started_at": at.isoformat()})

    def requested(self):
        self.state = "REQUESTED"

    def opened(self):
        self.state = "OPEN"

    def down(self, code: str, at: datetime):
        self.state = "UNAVAILABLE"
        self.fields.clear()
        self.channel_events.append({"at": at.isoformat(), "code": code, "generation": self.service.generation})

    def wants(self, kind: str, instrument_kind: str) -> bool:
        return kind in self.kinds and MEASURE_TARGET[kind] == instrument_kind

    def setup_message(self) -> dict:
        return {"type": "FEED_SETUP", "channel": self.channel, "acceptAggregationPeriod": 0.1,
                "acceptDataFormat": "COMPACT", "acceptEventFields": {k: list(MEASURE_FIELDS[k]) for k in self.kinds}}

    # ---- schema history (both channels)
    def schema(self, channel: int, kind: str, accepted, outcome: str, at: datetime | None = None, offered=None):
        key = (channel, kind)
        previous = self._schema_last.get(key)
        if outcome in ("ACCEPTED", "CHANGED") and previous == "WITHDRAWN":
            outcome = "RESTORED"
        self._schema_last[key] = "WITHDRAWN" if outcome.startswith("WITHDRAWN") else outcome
        if accepted is not None:
            self.revisions[key] = self.revisions.get(key, 0) + 1
        requested = FIELDS.get(kind) if channel != MEASURE_CHANNEL else MEASURE_FIELDS.get(kind)
        entry = {"channel": channel, "kind": kind, "outcome": outcome, "revision": self.revisions.get(key, 0),
                 "generation": self.service.generation, "requested": list(requested or ()),
                 "accepted": list(accepted) if accepted is not None else None,
                 "at": (at or self.clock()).isoformat()}
        if kind == "Quote" and accepted is not None:
            entry["side_times_in_accepted_map"] = {n: n in accepted for n in ("bidTime", "askTime")}
        if isinstance(offered, list):  # a refused server map: kept so a schema cause is visible
            entry["offered"] = [n if isinstance(n, str) else repr(n)[:40] for n in offered[:40]]
            if kind == "Quote":
                entry["side_times_in_offered_map"] = {n: n in offered for n in ("bidTime", "askTime")}
        self.schema_log.append(entry)

    # ---- core channel rows (observed after the service decided)
    def row(self, channel: int, kind: str, row: dict, received: datetime, accepted: bool):
        symbol = self._symbol(row.get("eventSymbol"))
        self._sample(f"{channel}:{kind}", row)
        if kind == "Quote":
            self._bbo(channel, row, received, symbol, accepted)
        elif kind == "Trade":
            state = time_state(row, "time", received)
            self._count("trade_time", symbol or "UNMAPPED", state["state"])
            if state["state"] != "VALUE":
                self._keep({"type": "TRADE_TIME", "channel": channel, "symbol": symbol,
                            "wire_symbol": row.get("eventSymbol"), "generation": self.service.generation,
                            "received_at": received.isoformat(), "time": state, "accepted_by_service": accepted})

    def _bbo(self, channel, row, received, symbol, accepted):
        decided = self.clock()
        verdict = {"status": "UNAVAILABLE", "reason": "UNMAPPED_SYMBOL"}
        if symbol is not None:
            try:
                self.service.quote(symbol, decided, self.max_age)
                verdict = {"status": "AVAILABLE"}
            except QuoteUnavailable as exc:
                verdict = {"status": "UNAVAILABLE", "reason": str(exc)}
        bid, ask = time_state(row, "bidTime", received), time_state(row, "askTime", received)
        self._count("bbo_times", symbol or "UNMAPPED", f"bid={bid['state']},ask={ask['state']}")
        self._count("bbo_verdict", symbol or "UNMAPPED", verdict.get("reason", "AVAILABLE"))
        self._keep({"type": "BBO", "channel": channel, "environment": self.service.environment,
                    "generation": self.service.generation, "revision": self.revisions.get((channel, "Quote"), 0),
                    "wire_symbol": row.get("eventSymbol"), "symbol": symbol,
                    "received_at": received.isoformat(), "decision_at": decided.isoformat(),
                    "bidTime": bid, "askTime": ask,
                    **{name: number_state(row, name) for name in ("bidPrice", "askPrice", "bidSize", "askSize")},
                    "accepted_by_service": accepted, "getter": verdict})

    # ---- measurement channel
    def configure(self, message: dict, at: datetime):
        if message.get("dataFormat") not in (None, "COMPACT"):
            for kind in self.kinds:
                self.fields.pop(kind, None)
                self.schema(self.channel, kind, None, "WITHDRAWN_UNSUPPORTED_FORMAT", at)
            return
        fields = message.get("eventFields")
        if fields is None:
            return
        if not isinstance(fields, dict):
            for kind in self.kinds:
                self.fields.pop(kind, None)
                self.schema(self.channel, kind, None, "WITHDRAWN_INVALID_MAP", at)
            return
        for kind in self.kinds:
            if kind not in fields:
                continue
            names = fields[kind]
            if (not isinstance(names, list) or not all(isinstance(n, str) for n in names)
                    or len(set(names)) != len(names) or not set(MEASURE_REQUIRED[kind]) <= set(names)):
                self.fields.pop(kind, None)
                self.schema(self.channel, kind, None, "WITHDRAWN_INVALID_MAP", at)
                continue
            names = tuple(names)
            previous = self.fields.get(kind)
            self.fields[kind] = names
            self.schema(self.channel, kind, names, "ACCEPTED" if previous in (None, names) else "CHANGED", at)

    def data(self, message: dict, received: datetime):
        data = message.get("data")
        if not isinstance(data, list) or len(data) % 2:
            raise QuoteUnavailable("MEASURE_DATA_INVALID")
        for i in range(0, len(data), 2):
            kind, values = data[i:i+2]
            names = self.fields.get(kind) if isinstance(kind, str) else None
            if names is None or not isinstance(values, list):
                label = kind if isinstance(kind, str) else "OTHER"
                self.undecodable[label] = self.undecodable.get(label, 0) + 1
                continue
            if len(values) % len(names):
                self.fields.pop(kind, None)
                self.schema(self.channel, kind, None, "WITHDRAWN_DATA_INVALID", received)
                raise QuoteUnavailable("MEASURE_DATA_INVALID")
            for j in range(0, len(values), len(names)):
                row = dict(zip(names, values[j:j+len(names)], strict=True))
                self._sample(f"{self.channel}:{kind}", row)
                if row.get("eventType") != kind:
                    self._count("measure_rejected", kind, "EVENT_TYPE_MISMATCH")
                    continue
                symbol = self._symbol(row.get("eventSymbol"))
                if symbol is None:
                    self._count("measure_rejected", kind, "UNMAPPED_SYMBOL")
                    continue
                (self._volume_row if kind == "Trade" else self._greeks_row)(symbol, row, received)

    # ---- volume
    def _volume_row(self, symbol: str, row: dict, received: datetime):
        generation = self.service.generation
        volume, trade_time = number_state(row, "dayVolume"), time_state(row, "time", received)
        day = row.get("dayId") if type(row.get("dayId")) is int else None
        last = self._volume.get(symbol)
        record = {"type": "VOLUME", "symbol": symbol, "wire_symbol": row.get("eventSymbol"),
                  "generation": generation, "received_at": received.isoformat(),
                  "day_id": day, "day_id_raw_state": "VALUE" if day is not None else
                  ("ABSENT" if "dayId" not in row else "INVALID"),
                  "day_volume": volume, "rth_trade_time": trade_time}
        value = Decimal(volume["value"]) if volume["state"] == "VALUE" else None
        if value is None:
            transition = "VOLUME_UNAVAILABLE"
        elif value < 0:
            transition = "INVALID_NEGATIVE"
            value = None
        elif last is None:
            transition = "FIRST_OBSERVATION"
        elif last["generation"] != generation:
            transition = "RECONNECT_SNAPSHOT"
        elif day is not None and last["day"] is not None and day != last["day"]:
            transition = "DAY_RESET" if day > last["day"] else "DAY_ID_DECREASED"
        elif last["value"] is None:
            transition = "AFTER_UNAVAILABLE"
        elif value > last["value"]:
            transition = "INCREASE"
        elif value == last["value"]:
            transition = "UNCHANGED"
        else:
            transition = "DECREASE_OR_CORRECTION"
        if last is not None and last["generation"] != generation:
            # Updates between the last receipt of the earlier connection and this snapshot
            # were not observed; the change across them is never attributed to one update.
            record["unobserved_gap_ms"] = (received - last["received"]) / timedelta(milliseconds=1)
        if last is not None and value is not None and last["value"] is not None:
            record["change_from_previous_observation"] = str(value - last["value"])
            record["trade_time_fixed_while_volume_changed"] = (
                value != last["value"] and trade_time.get("raw") is not None
                and trade_time.get("raw") == last["trade_raw"])
        record["transition"] = transition
        self._count("volume_transitions", symbol, transition)
        self._keep(record)
        self._volume[symbol] = {"generation": generation, "received": received, "day": day, "value": value,
                                "trade_raw": trade_time.get("raw")}
        if value is not None:
            self._bracket(symbol, received, value, day, generation, trade_time)

    def _bracket(self, symbol, received, value, day, generation, trade_time):
        """Last receipt before and first at/after each boundary (receipt brackets only)."""
        if not self.boundaries:
            return
        point = {"received_at": received.isoformat(), "day_volume": str(value), "day_id": day,
                 "generation": generation, "rth_trade_time": trade_time}
        edges = self._brackets.setdefault(symbol, {"generations_between": []})
        start, end = self.boundaries
        for label, boundary in (("open", start), ("end", end)):
            if received < boundary:
                edges[f"before_{label}"] = point
            elif f"after_{label}" not in edges:
                edges[f"after_{label}"] = point
        if start <= received < end or ("after_open" in edges and "after_end" not in edges):
            seen = edges["generations_between"]
            if generation not in seen:
                seen.append(generation)

    # ---- Greeks
    def _greeks_row(self, symbol: str, row: dict, received: datetime):
        decided = self.clock()
        source = time_state(row, "time", received)
        record = {"type": "GREEKS", "label": "RAW_OBSERVATION", "symbol": symbol,
                  "wire_symbol": row.get("eventSymbol"), "generation": self.service.generation,
                  "source_time": source, "received_at": received.isoformat(), "decision_at": decided.isoformat(),
                  "event_flags": row.get("eventFlags"), "flags": flags(row.get("eventFlags")),
                  "index": row.get("index"), "sequence": row.get("sequence"),
                  **{name: number_state(row, name) for name in GREEK_VALUES}}
        if source["state"] in ("VALUE", "FUTURE"):
            at = datetime.fromisoformat(source["at"])
            record["receipt_age_ms"] = (received - at) / timedelta(milliseconds=1)   # may be negative
            record["decision_age_ms"] = (decided - at) / timedelta(milliseconds=1)
        self._count("greeks_source_time", symbol, source["state"])
        for name in record["flags"] or ():
            self._count("greeks_flags", symbol, name)
        self._keep(record)
        summary = self._greeks.setdefault(symbol, {"observations": 0, "receipt_age_ms": [None, None],
                                                    "decision_age_ms": [None, None]})
        summary["observations"] += 1
        for key in ("receipt_age_ms", "decision_age_ms"):
            if key in record:
                low, high = summary[key]
                summary[key] = [record[key] if low is None else min(low, record[key]),
                                record[key] if high is None else max(high, record[key])]
        summary["last_raw_observation"] = record

    # ---- report
    def report(self) -> dict:
        volume = {}
        for symbol, counts in self.counts.get("volume_transitions", {}).items():
            volume[symbol] = {"transitions": counts, "receipt_brackets": self._brackets.get(symbol)}
        return {"purpose": "diagnostic measurements only; never eligibility, setup or risk evidence",
                "fault": self.fault, "measurement_channel": self.channel, "measurement_state": self.state,
                "measurement_kinds": list(self.kinds),
                "requested_fields": {"core": {k: list(FIELDS[k]) for k in CORE},
                                     "measurement": {k: list(MEASURE_FIELDS[k]) for k in self.kinds}},
                "schema_history": self.schema_log, "generations": self.generations,
                "channel_events": self.channel_events, "undecodable": dict(self.undecodable),
                "counts": self.counts, "samples": self.samples,
                "records": self.records, "records_kept": dict(self.kept), "records_dropped": dict(self.dropped),
                "records_bound_per_type": self.max_records,
                "volume": {"note": VOLUME_NOTE, "rth_window_total": "NOT_DERIVED",
                           "boundaries": [b.isoformat() for b in self.boundaries] if self.boundaries else None,
                           "by_symbol": volume},
                "greeks": {"note": GREEKS_NOTE, "current_state": "NOT_REDUCED_RAW_OBSERVATIONS_ONLY",
                           "by_symbol": self._greeks},
                "bbo_note": ("Side times are the provider's last bid/ask change times; receipt and decision "
                             "times are local and never substituted. Eligibility is the unchanged getter's.")}


# ---------------------------------------------------------------------------- clock ----

SNTP = re.compile(r"(?P<offset>[+-]\d+(?:\.\d+)?)\s+\+/-\s+(?P<uncertainty>\d+(?:\.\d+)?)")


def parse_sntp(text: str) -> dict | None:
    """Offset and ``+/-`` value as printed by sntp; sign recorded, not interpreted."""
    match = SNTP.search(text or "")
    if not match:
        return None
    return {"offset_seconds_as_printed": match["offset"], "plus_minus_seconds_as_printed": match["uncertainty"]}


def measure_clock(server="time.apple.com", *, run=subprocess.run, clock=utcnow, timeout=30) -> dict:
    """Read-only sntp query on this host (no -s/-S/-j clock-setting flags)."""
    command = ["sntp", server]
    started = clock()
    base = {"source": f"sntp {server}", "command": " ".join(command), "applied": False, "tolerance": "NONE",
            "host": {"system": platform.system(), "release": platform.release()},
            "measured_started_at": started.isoformat()}
    try:
        result = run(command, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return dict(base, status="UNAVAILABLE", code="SNTP_NOT_FOUND", measured_finished_at=clock().isoformat())
    except subprocess.TimeoutExpired:
        return dict(base, status="UNAVAILABLE", code="SNTP_TIMEOUT", measured_finished_at=clock().isoformat())
    finished = clock()
    raw = ((result.stdout or "") + (result.stderr or ""))[:1000]
    parsed = parse_sntp(result.stdout or "")
    out = dict(base, measured_finished_at=finished.isoformat(), exit_status=result.returncode, raw_output=raw)
    if result.returncode != 0:
        return dict(out, status="UNAVAILABLE", code="SNTP_FAILED")
    if parsed is None:
        return dict(out, status="UNAVAILABLE", code="SNTP_OUTPUT_UNRECOGNIZED")
    return dict(out, status="MEASURED", **parsed)


def load_host_clock(path: Path | None) -> dict:
    """A separately measured host-clock result, or why there is none. Never applied."""
    if path is None:
        return {"status": "NOT_MEASURED", "applied": False, "tolerance": "NONE"}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"status": "UNAVAILABLE", "code": "HOST_CLOCK_FILE_UNREADABLE", "applied": False, "tolerance": "NONE"}
    keys = ("status", "source", "measured_started_at", "measured_finished_at", "offset_seconds_as_printed",
            "plus_minus_seconds_as_printed", "exit_status", "raw_output", "code", "host", "command")
    if not isinstance(data, dict) or data.get("status") not in ("MEASURED", "UNAVAILABLE"):
        return {"status": "UNAVAILABLE", "code": "HOST_CLOCK_FILE_INVALID", "applied": False, "tolerance": "NONE"}
    clean = {k: data[k] for k in keys if k in data}
    if clean.get("status") == "MEASURED" and (not clean.get("measured_finished_at")
                                              or not parse_sntp(f"{clean.get('offset_seconds_as_printed')} +/- "
                                                                f"{clean.get('plus_minus_seconds_as_printed')}")):
        return {"status": "UNAVAILABLE", "code": "HOST_CLOCK_FILE_INVALID", "applied": False, "tolerance": "NONE"}
    if isinstance(clean.get("raw_output"), str):
        clean["raw_output"] = clean["raw_output"][:1000]
    return dict(clean, applied=False, tolerance="NONE",
                note="Measured separately on its own host and time; never added to event timestamps.")


# ---------------------------------------------------------------- credential guard ----


def write_report(report: dict, path: Path, env=None) -> dict:
    text = json.dumps(report, indent=2, default=str) + "\n"
    if not credential_free(text, env):
        report = {"status": "REPORT_REJECTED", "code": "REPORT_CREDENTIAL_MATCH",
                  "purpose": report.get("purpose")}
        text = json.dumps(report, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return report


# ------------------------------------------------------------------- volume window ----

def window_boundaries(day: date) -> tuple[datetime, datetime] | None:
    """The session's first 30 RTH minutes, as receipt boundaries (not exchange cutoffs)."""
    from desk.calendar import session, trading_day
    if not trading_day(day):
        return None
    opened = session(day)[0].to_pydatetime().astimezone(UTC)
    return opened, opened + timedelta(minutes=30)


def volume_capture(client, service, symbols, *, seconds, capture_fn=capture, clock=utcnow, reconnects=1,
                   recorder=None) -> dict:
    """Consecutive bounded segments; stops at the first segment that does not complete."""
    if type(seconds) is not int or not 1 <= seconds <= MAX_VOLUME_SECONDS:
        raise QuoteUnavailable("INVALID_VOLUME_BOUNDS")
    started = clock()
    from desk.calendar import ET
    recorder = recorder or Recorder(service, clock=clock, greeks=False,
                                    boundaries=window_boundaries(started.astimezone(ET).date()))
    issues = {}
    for symbol in symbols:
        try:
            client.resolve(symbol, "Equity", service)
        except QuoteUnavailable as exc:
            issues[symbol] = str(exc)
            if str(exc) in {"REST_HTTP_401", "REST_HTTP_403", "REST_HTTP_429", "REST_REQUEST_BUDGET"}:
                raise
    if not service.identities:
        raise QuoteUnavailable("NO_ACCEPTED_IDENTITIES")
    segments, remaining, stop = [], seconds, None
    while remaining > 0:
        length = min(SEGMENT_SECONDS, remaining)
        begun = clock()
        result = capture_fn(client, service, seconds=length, reconnects=reconnects, measure=recorder)
        final = result.get("final_attempt") or {}
        segments.append({"segment": len(segments) + 1, "seconds": length, "started_at": begun.isoformat(),
                         "finished_at": clock().isoformat(), "stop_reason": result.get("stop_reason"),
                         "attempts": result.get("attempts"), "faults": result.get("faults", []),
                         "terminal_eligibility": final.get("usable_at_end", {}),
                         "terminal_health": final.get("health")})
        remaining -= length
        if result.get("stop_reason") != "CAPTURE_COMPLETE":
            stop = result.get("stop_reason")
            break
    return {"purpose": "opening-window volume observations; diagnostic only, no signal/order activation",
            "environment": client.environment, "started_at": started.isoformat(),
            "finished_at": clock().isoformat(), "requested_seconds": seconds,
            "segment_bound_seconds": SEGMENT_SECONDS, "segments": segments, "stopped": stop,
            "status": "OBSERVATIONS_ONLY" if stop is None else "STOPPED", "identity_issues": issues,
            "requests": client.requests, "measurements": recorder.report(),
            "source_roles": {"decision_volume": "Alpaca SIP unchanged", "ep_rule": "unchanged (50 sessions, 0.5)"}}


# ------------------------------------------------------------------------ compare ----

def compare(capture_report: dict, alpaca_result: dict) -> dict:
    """Side by side, never reconciled: definitions, bounds and the raw difference."""
    from desk.calendar import ET
    started = datetime.fromisoformat(capture_report["started_at"]).astimezone(ET).date()
    entry = alpaca_result.get("entry_session")
    out = {"purpose": "volume definition comparison; no tolerance, no reconciliation, no eligibility",
           "session": started.isoformat(), "alpaca_entry_session": entry, "tolerance": "NONE"}
    if entry != started.isoformat():
        return dict(out, status="NOT_COMPARABLE", code="SESSION_MISMATCH")
    params = ((alpaca_result.get("requests") or {}).get("RTH30") or {}).get("request", {}).get("params", {})
    out["alpaca_definition"] = {"feed": params.get("feed"), "adjustment": params.get("adjustment"),
                                "timeframe": params.get("timeframe"), "start": params.get("start"),
                                "end": params.get("end")}
    out["tastytrade_definition"] = VOLUME_NOTE
    by_symbol = (capture_report.get("measurements") or {}).get("volume", {}).get("by_symbol", {})
    rows = {}
    for symbol, ticker in (alpaca_result.get("tickers") or {}).items():
        row = {}
        if ticker.get("status") == "AVAILABLE":
            row["alpaca_first30_volume"] = str(ticker["first30_volume"])
            row["alpaca_intervals"] = ticker.get("first30_intervals")
        else:
            row["alpaca"] = {"status": "UNAVAILABLE", "code": ticker.get("code")}
        edges = (by_symbol.get(symbol) or {}).get("receipt_brackets") or {}
        needed = ("before_open", "after_open", "before_end", "after_end")
        if not all(k in edges for k in needed):
            row["tastytrade"] = {"status": "BRACKETS_INCOMPLETE", "present": sorted(k for k in needed if k in edges)}
        elif any(edges[k]["day_id"] is None for k in needed):
            row["tastytrade"] = {"status": "NOT_COMPARABLE_DAY_ID_UNAVAILABLE", "edges": edges}
        elif len({edges[k]["day_id"] for k in needed}) > 1:
            row["tastytrade"] = {"status": "NOT_COMPARABLE_DAY_ID_CHANGED", "edges": edges}
        else:
            outer = Decimal(edges["after_end"]["day_volume"]) - Decimal(edges["before_open"]["day_volume"])
            inner = Decimal(edges["before_end"]["day_volume"]) - Decimal(edges["after_open"]["day_volume"])
            width = lambda a, b: (datetime.fromisoformat(edges[b]["received_at"])
                                  - datetime.fromisoformat(edges[a]["received_at"])) / timedelta(milliseconds=1)
            # dayVolume is the provider's cumulative counter: a reconnect or segment change
            # between the edges does not break it (same dayId required); it is context only.
            generations = {edges[k]["generation"] for k in needed} | set(edges.get("generations_between", []))
            row["tastytrade"] = {"status": "RECEIPT_BRACKETS", "label": "NOT_AN_RTH_TOTAL",
                                 "outer_change": str(outer), "inner_change": str(inner),
                                 "open_bracket_ms": width("before_open", "after_open"),
                                 "end_bracket_ms": width("before_end", "after_end"),
                                 "connections_spanned": len(generations), "edges": edges}
            if "alpaca_first30_volume" in row:
                alpaca = Decimal(row["alpaca_first30_volume"])
                row["difference_alpaca_minus_outer"] = str(alpaca - outer)
                row["difference_alpaca_minus_inner"] = str(alpaca - inner)
                row["status"] = ("WITHIN_RECEIPT_BRACKETS_NOT_PROOF" if min(inner, outer) <= alpaca <= max(inner, outer)
                                 else "OUTSIDE_RECEIPT_BRACKETS_UNRESOLVED")
        row.setdefault("status", "NOT_COMPARED")
        rows[symbol] = row
    return dict(out, status="COMPARED_UNRESOLVED_DIFFERENCES_REMAIN", by_symbol=rows,
                note="Units, odd-lot treatment, channel breadth and cutoff are not established by this "
                     "comparison; unexplained differences remain unresolved.")


# ---------------------------------------------------------------------------- CLI ----

def main(argv=None, env=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    clock_cmd = sub.add_parser("clock", help="read-only sntp query on this host")
    clock_cmd.add_argument("--server", default="time.apple.com")
    clock_cmd.add_argument("--output", type=Path, required=True)
    volume_cmd = sub.add_parser("volume", help="bounded opening-window volume capture (segments of <=600 s)")
    volume_cmd.add_argument("--environment", choices=("production", "sandbox"), required=True)
    volume_cmd.add_argument("--symbols", nargs="+", required=True)
    volume_cmd.add_argument("--seconds", type=int, required=True)
    volume_cmd.add_argument("--max-requests", type=int, default=20)
    volume_cmd.add_argument("--host-clock", type=Path)
    volume_cmd.add_argument("--output", type=Path, required=True)
    compare_cmd = sub.add_parser("compare", help="offline: a volume capture beside an alpaca_probe result")
    compare_cmd.add_argument("--capture", type=Path, required=True)
    compare_cmd.add_argument("--alpaca-result", type=Path, required=True)
    compare_cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    if args.command == "clock":
        report = write_report(measure_clock(args.server), args.output, env)
        print(json.dumps(report, indent=2))
        return 0 if report.get("status") == "MEASURED" else 1
    if args.command == "compare":
        report = compare(json.loads(args.capture.read_text()), json.loads(args.alpaca_result.read_text()))
        report = write_report(report, args.output, env)
        print(json.dumps({k: report.get(k) for k in ("status", "session", "code")}, indent=2))
        return 0 if report.get("status") == "COMPARED_UNRESOLVED_DIFFERENCES_REMAIN" else 1
    symbols = [canonical(s, "Equity") for s in args.symbols]
    if not 1 <= len(symbols) <= MAX_VOLUME_SYMBOLS or len(set(symbols)) != len(symbols):
        parser.error(f"Provide 1 to {MAX_VOLUME_SYMBOLS} distinct equity symbols")
    if not 1 <= args.seconds <= MAX_VOLUME_SECONDS:
        parser.error(f"Seconds must be 1..{MAX_VOLUME_SECONDS} (segments of at most {SEGMENT_SECONDS} s)")
    if not 1 <= args.max_requests <= 40:
        parser.error("Max requests must be 1..40")
    from desk.tastytrade_quotes import QuoteService
    try:
        client = ReadClient(Credentials(env.get("TASTYTRADE_CLIENT_SECRET", ""), env.get("TASTYTRADE_REFRESH_TOKEN", "")),
                            environment=args.environment, max_requests=args.max_requests)
        report = volume_capture(client, QuoteService(environment=args.environment), symbols, seconds=args.seconds)
    except QuoteUnavailable as exc:
        report = {"purpose": "opening-window volume observations; diagnostic only", "status": "UNAVAILABLE",
                  "reason": str(exc)}
    report["host_clock"] = load_host_clock(args.host_clock)
    report = write_report(report, args.output, env)
    print(json.dumps({k: report.get(k) for k in ("status", "stopped", "reason", "requests", "code")}, indent=2))
    return 0 if report.get("status") == "OBSERVATIONS_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
