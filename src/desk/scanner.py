"""The scanner: checks the watchlist for the 10 setups on a schedule and logs every scan.

Blueprint v2.4 sections 3 and 5, build step 4; serves the watch step. Runs on
the iMac (Taz, 29 Sep 2026). The scans:

- Ten minutes after the session close (normally 16:10 ET): daily bars for every watchlist name, the market
  filter from SPY and QQQ, the Trend Template, and the 10 daily checks. The
  setups armed for the next session are saved.
- In session, every 15 minutes from 09:45 until 15 minutes before the close: 15-minute bars for the
  armed names, to see whether each entry rule fired. At the last intraday slot the RSI(2) check
  also runs on the day's bars so far, as an estimate of the close.
- At 10:00, this morning's movers (Webull's top gainers and unusual volume)
  are checked for an episodic pivot, which is then watched like the rest.
- Fridays 40 minutes after the session close (normally 16:40 ET), the leader scan ranks Webull's top-200 lists (1 week to
  52-week gainers, most traded) on our own bars and writes next week's
  watchlist, with Taz's adds and removals from data/taz-picks.json.

Every scan, including a failed one, writes a line to the scan log, which the
Friday note reads for the funnel (scans run out of scheduled, setups armed,
entries triggered, and names skipped for bad data). A name with bad or stale
data is skipped and logged (fail closed); a scan that can't get SPY and QQQ
arms nothing.

Plan B: if the iMac misses scans, the free Microsoft VM runs the same command
from cron, and a missed scan shows in the Friday funnel as a drift flag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from desk.bars import BarDataError, require, validate
from desk.calendar import trading_day, next_trading_day, session, clock
from desk.bar_contract import (completed_daily, completed_intraday, provenance,
                               check_price_scale, developing_daily_from_m15)
from desk.indicators import daily_features
from desk.data_basis import price_basis, volume_basis
from desk.signal_state import SignalStore, SignalStateError, restore_signal
from desk.security import securities
from desk.symbols import canonical_symbol
from desk.playbook.cards import CARDS, INDEX_ETFS
from desk.playbook.filters import GateResult, MarketSize, market_filter, trend_template
from desk.playbook.triggers import Context, Signal, connors_rsi2, episodic_pivot, minimum_history, scan

ET = ZoneInfo("America/New_York")
DAILY_BARS = 1000                 # rule 6: warm-up for the 200-day and the 52-week values
MIN_DAILY_BARS = 260
CLOSE_SCAN = time(16, 10)
LEADER_SCAN = time(16, 40)        # Fridays: the weekly leader scan builds next week's watchlist
EP_SCAN = time(10, 0)             # the first 30 minutes are in: check this morning's movers
FIRST_INTRADAY, LAST_INTRADAY = time(9, 45), time(15, 45)

class BarSource(Protocol):
    def bars(self, symbols: Sequence[str], *, category: str, timespan: str, count: int = 1000,
             **kw) -> dict[str, pd.DataFrame]: ...


def scheduled_slots(d: date) -> list[datetime]:
    """Every scan due on a trading day, in Eastern time."""
    if not trading_day(d):
        return []
    opened, closed = session(d)
    slots, t = [], opened.to_pydatetime() + timedelta(minutes=15)
    while t < closed:
        slots.append(t)
        t += timedelta(minutes=15)
    slots.append((closed + timedelta(minutes=10)).to_pydatetime())
    if d.weekday() == 4:
        slots.append((closed + timedelta(minutes=40)).to_pydatetime())
    return slots


def fetch(source: BarSource, symbols: Sequence[str], timespan: str, count: int,
          skipped: dict[str, str]) -> dict[str, pd.DataFrame]:
    """Bars in batches of 20 per category. A name that fails is skipped and logged."""
    symbols = list(dict.fromkeys(canonical_symbol(s) for s in symbols))
    out: dict[str, pd.DataFrame] = {}
    metadata = securities(source, symbols, skipped)
    for cat in ("US_ETF", "US_STOCK"):
        names = [s for s in symbols if s in metadata and metadata[s].bar_category == cat]
        for i in range(0, len(names), 20):
            batch = names[i:i + 20]
            try:
                out.update(source.bars(batch, category=cat, timespan=timespan, count=count))
            except BarDataError as e:
                if len(batch) == 1:
                    skipped[batch[0]] = str(e)
                    continue
                # One bad name fails the whole call, so retry the batch one name at a time.
                for s in batch:
                    try:
                        out.update(source.bars([s], category=cat, timespan=timespan, count=count))
                    except BarDataError as e1:
                        skipped[s] = str(e1)
    for symbol in list(out):
        if symbol not in metadata:
            out.pop(symbol)
            continue
        observed = out[symbol].attrs.get("provider_identity")
        if observed and observed != {"symbol": symbol, "instrument_id": metadata[symbol].instrument_id}:
            out.pop(symbol)
            skipped[symbol] = "bar identity disagrees with security metadata"
            continue
        out[symbol].attrs["security_metadata"] = metadata[symbol].model_dump(mode="json")
    for symbol in symbols:
        if symbol not in out:
            skipped.setdefault(symbol, "no bars returned")
    return out


@dataclass
class ScanRecord:
    kind: str                     # "close" or "intraday"
    at: str                       # ISO time, Eastern
    slot: str | None
    scanned: int = 0
    market: str | None = None
    market_why: str | None = None
    armed: list[dict] = field(default_factory=list)
    triggered: list[dict] = field(default_factory=list)
    leaders: list[dict] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    discovery: dict = field(default_factory=dict)


def _sig(s: Signal, **extra) -> dict:
    d = asdict(s)
    d["as_of"] = s.as_of.isoformat()
    return {**d, **extra}


def _event_signal(sig: Signal, event: dict) -> dict:
    return _sig(sig, event_id=event["id"], trigger_at=event["trigger_at"],
                observed_at=event["observed_at"], expires_at=event["expires_at"],
                valid_until=event["valid_until"], entry_level=event["entry_level"], why=event["reason"])


def close_scan(source: BarSource, watchlist: Sequence[str], now: datetime, *, decision_clock: Callable[[], datetime] | None = None,
               preparing: bool = False) -> tuple[ScanRecord, list[Signal]]:
    watchlist = list(dict.fromkeys(canonical_symbol(s) for s in watchlist))
    rec = ScanRecord("close", now.astimezone(ET).isoformat(), None)
    symbols = sorted(set(watchlist) | {"SPY", "QQQ"})
    bars = fetch(source, symbols, "D", DAILY_BARS, rec.skipped)
    now = decision_clock() if decision_clock else now
    rec.at = clock(now).isoformat()
    for sym in symbols:
        if sym not in bars:
            rec.skipped.setdefault(sym, "no bars returned")
    feats: dict[str, pd.DataFrame] = {}
    for sym, df in bars.items():
        try:
            if not preparing and clock(now) < session(clock(now).date())[1]:
                raise BarDataError("close scan requires a completed session")
            df = completed_daily(df, now)
            price_basis(df, now, symbol=sym)
            require(df, min_bars=200 if sym in {"SPY", "QQQ"} else 1)
            bars[sym] = df
            feats[sym] = daily_features(df)
        except BarDataError as e:
            rec.skipped[sym] = str(e)
    rec.scanned = len(feats)
    if "SPY" not in feats or "QQQ" not in feats:
        rec.error = "no SPY or QQQ bars: nothing armed" + ("; " + rec.skipped.get("SPY", rec.skipped.get("QQQ", "")) if rec.skipped else "")
        return rec, []
    try:
        size, why = market_filter(feats["SPY"], feats["QQQ"])
    except BarDataError as e:
        rec.error = f"market filter: {e}"
        return rec, []
    rec.market, rec.market_why = size.value, why
    spy = feats["SPY"]["close"]
    armed: list[Signal] = []
    for sym in sorted(set(watchlist)):
        if sym not in feats:
            continue
        try:
            gate = None
            if sym not in INDEX_ETFS:
                try:
                    gate = trend_template(feats[sym], spy)
                except BarDataError as exc:
                    gate = GateResult({"template history available": False})
                    rec.skipped[f"{sym}/trend_template"] = str(exc)
            armed += [replace(sig, price_scale_id=provenance(bars[sym], "D").price_scale_id,
                              price_basis=price_basis(bars[sym], now, symbol=sym).model_dump(mode="json"))
                      for sig in scan(feats[sym], Context(sym, size, gate, spy), skipped=rec.skipped)]
        except BarDataError as e:
            rec.skipped[sym] = str(e)
    rec.armed = [_sig(s) for s in armed]
    rec.discovery["prepared"] = sorted(s for s in watchlist if s in feats)
    return rec, armed


def _ep_entry_hit(sig: Signal, m15: pd.DataFrame, now: datetime) -> tuple[bool, float, str]:
    """Detect either completed opening-range breakout on the EP's first session.

    Bars use the repository's start-timestamp contract. This is a crossing
    detector; setup evidence, stop/chase checks and approval remain separate.
    """
    clock = pd.Timestamp(now)
    if clock.tzinfo is None or not isinstance(m15.index, pd.DatetimeIndex) or m15.index.tz is None:
        raise BarDataError("EP entries require timezone-aware clock and bar timestamps")
    clock = clock.tz_convert(ET)
    reference = pd.Timestamp(sig.as_of)
    if reference.tzinfo is None:
        raise BarDataError("EP reference bar needs a timezone")
    if next_trading_day(reference.tz_convert(ET).date()) != clock.date():
        return False, sig.trigger, "EP entry is only valid on its gap day"
    if not trading_day(clock.date()) or not session(clock.date())[0] <= clock < session(clock.date())[1]:
        return False, sig.trigger, "outside regular-session entry hours"

    opened = clock.normalize() + pd.Timedelta(hours=9, minutes=30)
    interval = pd.Timedelta(minutes=15)
    # A forming/future bar cannot create or change an earlier trigger.
    bars = m15[(m15.index >= opened) & (m15.index + interval <= clock)]
    if bars.empty:
        return False, sig.trigger, "no completed opening-range bar yet"
    validate(bars)
    latest = clock.floor("15min") - interval
    expected = pd.date_range(opened, latest, freq="15min").tz_convert(m15.index.tz)
    if not bars.index.equals(expected):
        raise BarDataError("missing, stale or misaligned completed EP session bars")

    candidates: list[tuple[pd.Timestamp, int, float]] = []
    for count in (1, 4):
        if len(bars) <= count:
            continue
        level = float(bars["high"].iloc[:count].max())
        crossed = bars.iloc[count:][bars["high"].iloc[count:] > level]
        if not crossed.empty:
            candidates.append((crossed.index[0], count * 15, level))
    if not candidates:
        return False, sig.trigger, "no breakout above a completed opening range"
    stamp, minutes, level = min(candidates)
    alternatives = ", ".join(f"{m}-minute" for _, m, _ in candidates)
    return True, level, (f"broke completed {minutes}-minute opening-range high in bar starting {stamp.isoformat()}; "
                         f"observed alternatives: {alternatives}")


def entry_hit(sig: Signal, m15: pd.DataFrame, now: datetime) -> tuple[bool, float, str]:
    """Did today's 15-minute bars fire this signal's entry rule? Returns (hit, level, why)."""
    stamp = clock(now)
    if not trading_day(stamp.date()) or not session(stamp.date())[0] <= stamp < session(stamp.date())[1]:
        return False, sig.trigger, "outside regular session"
    today = completed_intraday(m15, now)
    check_price_scale(sig.price_basis, today, now, symbol=sig.symbol)
    if sig.setup_id == "5_qullamaggie_episodic_pivot":
        return _ep_entry_hit(sig, today if not today.empty else m15, now)
    if today.empty:
        return False, sig.trigger, "no bars today yet"
    first, later = today.iloc[0], today.iloc[1:]
    setup = sig.setup_id
    if setup == "7_luk_pullback_reclaim":
        below = today["low"] < sig.trigger
        if below.any():
            after = today.loc[below.idxmax():]
            if (after["close"] > sig.trigger).any():
                return True, sig.trigger, "15-minute close back above the level"
        return False, sig.trigger, "no dip and reclaim yet"
    if setup == "9_weinstein_stage4_breakdown":
        return bool(first["close"] < sig.trigger), float(first["close"]), "first 15 minutes still under support"
    if sig.direction == "long":
        level = max(sig.trigger, float(first["high"]))
        return bool((later["high"] > level).any()), level, "took out the first 15-minute high and the trigger"
    level = min(sig.trigger, float(first["low"]))
    return bool((later["low"] < level).any()), level, "broke the first 15-minute low and the trigger"


def entry_observations(sig: Signal, bars: pd.DataFrame) -> list[dict]:
    """Per-completed-bar crossings for durable replay; never a whole-day .any().

    Step 06 validation precedes this function. EP alternatives share one candidate;
    the first crossing wins. OHLC cannot establish tick order or executable fills.
    """
    observations = []
    long = sig.direction == "long"
    first = bars.iloc[0]
    for i, (start, row) in enumerate(bars.iterrows()):
        entries = []
        previous = bars.iloc[i - 1] if i else None
        if sig.setup_id == "5_qullamaggie_episodic_pivot":
            for count in (1, 4):
                if i >= count:
                    level = float(bars.high.iloc[:count].max())
                    if previous.close <= level and row.high > level:
                        entries.append((level, f"fresh break of completed {count * 15}-minute range"))
        elif sig.setup_id == "7_luk_pullback_reclaim":
            if (bars.low.iloc[:i + 1] < sig.trigger).any() and row.close > sig.trigger:
                if previous is None or previous.close <= sig.trigger:
                    entries.append((sig.trigger, "fresh close reclaiming the level"))
        elif sig.setup_id == "9_weinstein_stage4_breakdown":
            if i == 0 and row.close < sig.trigger:
                entries.append((float(row.close), "first 15 minutes under support"))
            elif i and previous.close >= sig.trigger and row.low < sig.trigger:
                entries.append((sig.trigger, "fresh support breakdown after reset"))
        elif sig.setup_id == "10_connors_rsi2":
            # The near-close producer supplies only the completed observation
            # corresponding to its developing-daily estimate.
            if i == len(bars) - 1:
                entries.append((sig.trigger, "near-close RSI(2) estimate"))
        elif i:
            level = max(sig.trigger, float(first.high)) if long else min(sig.trigger, float(first.low))
            crossed = (previous.close <= level and row.high > level) if long else (previous.close >= level and row.low < level)
            if crossed:
                entries.append((level, "fresh crossing of trigger and opening range"))
        observations.append({"bar_end": (start + pd.Timedelta(minutes=15)).isoformat(),
                             "open": float(row.open), "high": float(row.high),
                             "low": float(row.low), "close": float(row.close), "entries": entries})
    return observations


def observe_signal(store: SignalStore, sig: Signal, frame: pd.DataFrame, now: datetime) -> list[dict]:
    day = clock(now).date()
    reference = clock(sig.as_of).date()
    expected = reference if sig.setup_id == "10_connors_rsi2" else next_trading_day(reference)
    if day != expected:
        store.suspend(sig, day, "signal belongs to a different entry session")
        return []
    bars = completed_intraday(frame, now)
    check_price_scale(sig.price_basis, bars, now, symbol=sig.symbol)
    if bars.empty:
        return []
    observations = entry_observations(sig, bars)
    if sig.setup_id == "10_connors_rsi2":
        observations = observations[-1:]
    return store.observe(sig, day, observations, now)


def intraday_scan(source: BarSource, armed: Sequence[Signal], now: datetime, *, decision_clock: Callable[[], datetime] | None = None,
                  store: SignalStore | None = None) -> ScanRecord:
    rec = ScanRecord("intraday", now.astimezone(ET).isoformat(), None)
    symbols = sorted({s.symbol for s in armed})
    bars = fetch(source, symbols, "M15", 40, rec.skipped) if symbols else {}
    now = decision_clock() if decision_clock else now
    rec.at = clock(now).isoformat()
    rec.scanned = len(bars)
    for sig in armed:
        # RSI(2) enters near the close on the day it sets up (the 15:45 ticket), never the next morning.
        if sig.symbol not in bars:
            if store:
                store.suspend(sig, clock(now).date(), rec.skipped.get(sig.symbol, "missing bars"))
            continue
        if sig.setup_id == "10_connors_rsi2":
            continue
        try:
            if store:
                events = observe_signal(store, sig, bars[sig.symbol], now)
                rec.triggered.extend(_event_signal(sig, e) for e in events)
                continue
            # Stateless diagnostic only; run() always supplies the durable store.
            hit, level, why = entry_hit(sig, bars[sig.symbol], now)
        except (BarDataError, KeyError, SignalStateError) as e:
            rec.skipped[sig.symbol] = str(e)
            if store:
                store.suspend(sig, clock(now).date(), str(e))
            continue
        if hit:
            rec.triggered.append(_sig(sig, entry_level=round(level, 2), why=why))
    return rec


def episodic_pivots(source: BarSource, market: MarketSize, now: datetime, skipped: dict[str, str], *, decision_clock: Callable[[], datetime] | None = None,
                    candidates: Sequence[str] | None = None) -> list[Signal]:
    """At 10:00: this morning's movers checked for an episodic pivot, from the gap and the first 30 minutes."""
    from desk.watchlist import movers

    today = now.astimezone(ET).date()
    names = movers(source, skipped) if candidates is None else list(dict.fromkeys(canonical_symbol(s) for s in candidates))
    daily = fetch(source, names, "D", DAILY_BARS, skipped)
    intraday = fetch(source, names, "M15", 40, skipped)
    now = decision_clock() if decision_clock else now
    out = []
    for sym in names:
        if sym not in daily or sym not in intraday:
            continue
        d, m = daily[sym], intraday[sym]
        try:
            d = completed_daily(d, now)
            m = completed_intraday(m, now)
            check_price_scale(price_basis(d, now, symbol=sym).model_dump(mode="json"), m, now, symbol=sym)
            if len(m) < 2 or len(d) < minimum_history("5_qullamaggie_episodic_pivot"):
                raise BarDataError(f"EP needs {minimum_history('5_qullamaggie_episodic_pivot')} completed daily bars and two completed M15 bars")
            volume_basis(m.iloc[:2])
            ctx = Context(sym, market, None, today_open=float(m["open"].iloc[0]),
                          early_volume=float(m["volume"].iloc[:2].sum()),
                          early_volume_basis=m.attrs.get("volume_basis"))
            sig = episodic_pivot(daily_features(d), CARDS["5_qullamaggie_episodic_pivot"], ctx)
        except BarDataError as e:
            skipped[sym] = str(e)
            continue
        if sig:
            out.append(replace(sig, price_scale_id=provenance(d, "D").price_scale_id,
                               price_basis=price_basis(m, now, symbol=sym).model_dump(mode="json")))
    return out


def leader_scan_job(source: BarSource, log: "ScanLog", now: datetime, *, decision_clock: Callable[[], datetime] | None = None) -> ScanRecord:
    rec = _leader_scan_job(source, log, now, decision_clock=decision_clock)
    status = ("PARTIAL" if rec.scanned else "FAILED") if rec.error else "EMPTY" if not rec.leaders else "READY"
    log.build_status(datetime.fromisoformat(rec.at), status, rec.skipped, success=not rec.error)
    rec.discovery["watchlist_build"] = log.watchlist_status(datetime.fromisoformat(rec.at))
    return rec


def _leader_scan_job(source: BarSource, log: "ScanLog", now: datetime, *, decision_clock: Callable[[], datetime] | None = None) -> ScanRecord:
    """Fridays after the close: rank the universe and write next week's watchlist with Taz's picks."""
    from desk.watchlist import build_watchlist, leader_scan, universe

    rec = ScanRecord("leader", now.astimezone(ET).isoformat(), None)
    names = universe(source, rec.skipped)
    bars = fetch(source, sorted(set(names) | {"SPY"}), "D", DAILY_BARS, rec.skipped)
    now = decision_clock() if decision_clock else now
    rec.at = clock(now).isoformat()
    if "SPY" not in bars or bars["SPY"].index[-1].tz_convert(ET).date() != now.astimezone(ET).date():
        rec.error = "no SPY bars for today: last week's watchlist stays"
        return rec
    clean = {}
    for symbol, frame in bars.items():
        try:
            validated = completed_daily(frame, now)
            price_basis(validated, now, symbol=symbol)
            clean[symbol] = validated
        except BarDataError as exc:
            rec.skipped[symbol] = str(exc)
    if "SPY" not in clean:
        rec.error = "no valid completed SPY bars"
        return rec
    scan_ = leader_scan({s: b for s, b in clean.items() if s != "SPY"}, clean["SPY"]["close"])
    failures = {k: v for k, v in rec.skipped.items() if not v.startswith("unsupported security type")}
    failures.update({k: v for k, v in scan_.skipped.items() if v.startswith("bad data:") and "under a year" not in v})
    rec.skipped.update(scan_.skipped)
    rec.scanned = scan_.ranked
    if failures:
        rec.error = "incomplete watchlist preparation: previous list retained"
        return rec
    picks = log.picks()
    wl = build_watchlist([l.symbol for l in scan_.leaders], added=picks.get("add", []),
                         removed=picks.get("remove", []))
    log.write_watchlist(wl)
    rec.leaders = [{"symbol": l.symbol, "score": l.score, **l.returns} for l in scan_.leaders]
    return rec


def rsi2_estimate(source: BarSource, watchlist: Sequence[str], market: MarketSize, now: datetime,
                  skipped: dict[str, str] | None = None, *, decision_clock: Callable[[], datetime] | None = None,
                  store: SignalStore | None = None, triggered: list[dict] | None = None) -> list[Signal]:
    """15 minutes before close: explicitly developing daily bar from closed M15s."""
    etfs = [s for s in watchlist if s in INDEX_ETFS]
    skipped = skipped if skipped is not None else {}
    bars = fetch(source, etfs, "D", DAILY_BARS, skipped)
    intraday = fetch(source, etfs, "M15", 40, skipped)
    now = decision_clock() if decision_clock else now
    out = []
    for sym, df in bars.items():
        try:
            if sym not in intraday:
                raise BarDataError("No intraday constituents for near-close snapshot")
            snapshot = developing_daily_from_m15(df, intraday[sym], now)
            basis = price_basis(snapshot, now, symbol=sym)
            sig = connors_rsi2(daily_features(snapshot), CARDS["10_connors_rsi2"], Context(sym, market))
            if sig:
                evidence = {**sig.saw, "snapshot_as_of": snapshot.attrs["developing_as_of"],
                            "developing_daily_ohlcv": json.dumps(snapshot.iloc[-1].to_dict(), sort_keys=True),
                            "constituents_through": snapshot.attrs["constituents_through"],
                            "developing_components": json.dumps(snapshot.attrs["developing_components"], sort_keys=True)}
                sig = replace(sig, saw=evidence, price_scale_id=provenance(df, "D").price_scale_id,
                              price_basis=basis.model_dump(mode="json"))
                out.append(sig)
                if store is not None and triggered is not None:
                    for e in observe_signal(store, sig, intraday[sym], now):
                        triggered.append(_event_signal(sig, e))
        except BarDataError as exc:
            skipped[sym] = str(exc)
    return out


class ScanLog:
    """One JSON line per scan in <data dir>/scan-log.jsonl, plus the armed list for the next session."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.signals = SignalStore(root / "signals.sqlite")

    @property
    def path(self) -> Path:
        return self.root / "scan-log.jsonl"

    def write(self, rec: ScanRecord) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps(asdict(rec), default=str) + "\n")

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def picks(self) -> dict:
        """Taz's adds and removals: <data dir>/taz-picks.json, {"add": [...], "remove": [...]}."""
        p = self.root / "taz-picks.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def write_json(self, name: str, value) -> None:
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", dir=self.root, delete=False) as fh:
            path = Path(fh.name)
            try:
                json.dump(value, fh, indent=1, allow_nan=False)
                fh.flush()
                os.fsync(fh.fileno())
            except Exception:
                path.unlink(missing_ok=True)
                raise
        try:
            path.replace(self.root / name)
        finally:
            path.unlink(missing_ok=True)

    def write_watchlist(self, wl: Mapping[str, list[str]]) -> None:
        self.write_json("watchlist.json", wl)

    def build_status(self, now, status, reasons, *, success=False) -> None:
        old = self.watchlist_status(now)
        self.write_json("watchlist-status.json", {
            "attempted_at": clock(now).isoformat(), "status": status, "reasons": reasons,
            "last_success_at": clock(now).isoformat() if success else old.get("last_success_at")})

    def watchlist_status(self, now) -> dict:
        p = self.root / "watchlist-status.json"
        if not p.exists():
            return {"status": "UNKNOWN", "stale": True, "last_success_at": None}
        try:
            state = json.loads(p.read_text())
            if not isinstance(state, dict):
                raise ValueError("invalid status")
            stamp = state.get("last_success_at")
            # Weekly freshness label; data still validates independently per scan.
            state["stale"] = not stamp or not timedelta(0) <= clock(now) - clock(stamp) <= timedelta(days=7)
            return state
        except (ValueError, TypeError, BarDataError):
            return {"status": "INVALID", "stale": True, "last_success_at": None}

    def prepared_users(self, day, kind="daily") -> set[str]:
        p = self.root / f"user-prepared-{kind}-{day}.json"
        return set(json.loads(p.read_text())) if p.exists() else set()

    def mark_prepared_users(self, day, names, kind="daily"):
        self.write_json(f"user-prepared-{kind}-{day}.json", sorted(self.prepared_users(day, kind) | set(names)))

    def add_armed(self, day: date, market: MarketSize, sigs: Sequence[Signal]) -> None:
        self.load_armed(day)  # import a legacy candidate list once, if present
        self.signals.save_armed(day, market.value, [_sig(s) for s in sigs], append=True)

    def save_armed(self, for_day: date, rec: ScanRecord) -> None:
        self.signals.save_armed(for_day, rec.market, rec.armed, observed_at=rec.at or None)

    def load_armed(self, day: date) -> tuple[MarketSize | None, list[Signal]]:
        data = self.signals.load_armed(day)
        if data is None:
            p = self.root / f"armed-{day.isoformat()}.json"
            if not p.exists():
                return None, []
            legacy = json.loads(p.read_text())
            self.signals.save_armed(day, legacy["market"], legacy["signals"])
            data = self.signals.load_armed(day)
        market, payload = data
        return (MarketSize(market) if market else None), [restore_signal(s) for s in payload]


def funnel(records: Sequence[Mapping], start: date, end: date) -> dict:
    """The Friday note's funnel for [start, end]: scans run out of scheduled, armed, triggered, skips."""
    scheduled = sum(len(scheduled_slots(start + timedelta(days=i))) for i in range((end - start).days + 1))
    ran = [r for r in records if r.get("slot") and start <= datetime.fromisoformat(r["slot"]).date() <= end]
    done = {r["slot"] for r in ran if not r.get("error")}
    return {
        "scans_scheduled": scheduled,
        "scans_run": len(done),
        "scans_failed": sorted({r["slot"] for r in ran if r.get("error")} - done),
        "setups_armed": sum(len(r.get("armed", [])) for r in ran),
        "entries_triggered": len({t["event_id"] for r in ran for t in r.get("triggered", []) if t.get("event_id")})
            + sum(1 for r in ran for t in r.get("triggered", []) if not t.get("event_id")),
        "names_skipped": sorted({s for r in ran for s in r.get("skipped", {})}),
    }


def due_slot(now: datetime) -> datetime | None:
    """The latest slot at or before now, within 15 minutes; None outside the schedule."""
    now = clock(now)
    past = [s for s in scheduled_slots(now.date()) if s <= now < s + timedelta(minutes=15)]
    return past[-1] if past else None


def run(source: BarSource, watchlist: Sequence[str], log: ScanLog, now: datetime, *, decision_clock: Callable[[], datetime] | None = None) -> ScanRecord | None:
    log.signals.expire(now)  # also runs outside scan slots and when no bars arrive
    slot = due_slot(now)
    if slot is None:
        return None
    if any(r.get("slot") == slot.isoformat() and not r.get("error") for r in log.records()):
        return None                                   # this slot already ran
    from desk.watchlist import ALWAYS, bearish_candidates, movers, overlay_picks
    closed = session(slot.date())[1]
    close_slot, leader_slot = closed + timedelta(minutes=10), closed + timedelta(minutes=40)
    try:
        picks = log.picks()
        watchlist = overlay_picks(watchlist, picks)
        removed = {canonical_symbol(s) for s in picks.get("remove", [])} - set(ALWAYS)
        log.signals.suspend_symbols(removed, "removed from current watchlist")
        if slot == leader_slot:
            rec = leader_scan_job(source, log, now, decision_clock=decision_clock)
        elif slot == close_slot:
            discovery_errors = {}
            moving = movers(source, discovery_errors)
            declining = bearish_candidates(source, discovery_errors)
            sources = {s: list(tags) for s, tags in watchlist.items()}
            for label, names in (("mover", moving), ("bearish", declining)):
                for symbol in names:
                    if symbol not in removed:
                        sources.setdefault(symbol, []).append(label)
            rec, _ = close_scan(source, sources, now, decision_clock=decision_clock)
            rec.discovery.update(sources=sources, source_errors=discovery_errors,
                                 status="PARTIAL" if discovery_errors else "READY")
            log.save_armed(next_trading_day(slot.date()), rec)
        else:
            market, armed = log.load_armed(slot.date())
            armed = [s for s in armed if s.symbol not in removed]
            users = {s for s, tags in watchlist.items() if "Taz" in tags}
            pending = sorted(users - log.prepared_users(slot.date()))
            preparation = None
            user_new = []
            if pending:
                preparation, user_new = close_scan(source, pending, now, decision_clock=decision_clock, preparing=True)
                if not preparation.error:
                    market = MarketSize(preparation.market)
                    log.add_armed(slot.date(), market, user_new)
                    log.mark_prepared_users(slot.date(), preparation.discovery.get("prepared", []))
                    armed += user_new
            new: list[Signal] = []
            ep_pending = users - log.prepared_users(slot.date(), "ep")
            ep_due = (slot.time() == EP_SCAN or (ep_pending and slot.time() > EP_SCAN)) and market is not None
            if ep_due:
                skipped: dict[str, str] = {}
                candidates = sorted((set(movers(source, skipped)) if slot.time() == EP_SCAN else set()) | ep_pending)
                candidates = [s for s in candidates if s not in removed]
                new = episodic_pivots(source, market, now, skipped, decision_clock=decision_clock, candidates=candidates)
                log.add_armed(slot.date(), market, new)
                log.mark_prepared_users(slot.date(), ep_pending - set(skipped), "ep")
                armed = [*armed, *new]
            rec = intraday_scan(source, armed, now, decision_clock=decision_clock, store=log.signals)
            if ep_due:
                rec.skipped.update(skipped)
                rec.discovery["ep_candidates"] = {"symbols": candidates, "errors": skipped}
            if preparation:
                rec.discovery["user_preparation"] = {"requested": pending, "error": preparation.error,
                    "prepared": preparation.discovery.get("prepared", []), "skipped": preparation.skipped}
            rec.discovery["sources"] = watchlist
            if slot == closed - timedelta(minutes=15) and market is not None:
                new = rsi2_estimate(source, watchlist, market, now, rec.skipped,
                                    decision_clock=decision_clock, store=log.signals, triggered=rec.triggered)
            rec.armed = [_sig(s) for s in [*user_new, *new]]
    except Exception as e:                            # any failure is logged as a failed scan, never silent
        log.signals.suspend_all("scan failed; fresh validation required")
        kind = "leader" if slot == leader_slot else "close" if slot == close_slot else "intraday"
        rec = ScanRecord(kind, now.isoformat(), None, error=f"{type(e).__name__}: {e}")
        if kind == "leader":
            log.build_status(now, "FAILED", {"build": rec.error})
    rec.discovery.setdefault("watchlist_build", log.watchlist_status(now))
    rec.slot = slot.isoformat()
    log.write(rec)
    return rec


def revalidate_signal(source: BarSource, log: ScanLog, event_id: str, now: datetime, *,
                      symbol: str, price: float, quote_at: datetime,
                      decision_clock: Callable[[], datetime] | None = None) -> dict:
    """Fresh signal prerequisite for later approval/execution; never an approval.

    The future broker/review adapter supplies a trusted current underlying quote.
    Price, stop and chase checks are symmetric. This does not replace risk sizing,
    earnings/catalyst gates, quote identity validation by the broker, or approval.
    """
    import math
    from desk.risk import RiskLimits

    event = log.signals.get(event_id, now)
    from desk.watchlist import ALWAYS
    removed = {canonical_symbol(s) for s in log.picks().get("remove", [])} - set(ALWAYS)
    if event["signal"]["symbol"] in removed:
        log.signals.suspend_symbols(removed, "removed from current watchlist")
        return {"event_id": event_id, "eligible": False, "reasons": ["removed from current watchlist"],
                "checked_at": clock(now).isoformat(), "purpose": "signal prerequisite only; not approval or order"}
    sig = restore_signal(event["signal"])
    card = CARDS.get(sig.setup_id)
    if event["state"] != "triggered" or event["blocked"] or not card or card.fingerprint() != sig.setup_version:
        return {"event_id": event_id, "eligible": False,
                "reasons": ["event is terminal or its setup version changed"],
                "checked_at": clock(now).isoformat(), "card_version": event["card_version"],
                "expires_at": event["expires_at"], "purpose": "signal prerequisite only; not approval or order"}
    skipped = {}
    bars = fetch(source, [sig.symbol], "M15", 40, skipped)
    checked = decision_clock() if decision_clock else now
    reasons = []
    if sig.symbol not in bars:
        log.signals.suspend(sig, clock(checked).date(), "fresh bars unavailable")
        reasons.append("fresh bars unavailable")
    else:
        try:
            observe_signal(log.signals, sig, bars[sig.symbol], checked)
        except (BarDataError, KeyError, SignalStateError) as exc:
            log.signals.suspend(sig, clock(checked).date(), str(exc))
            reasons.append("fresh bar/action validation failed")
    event = log.signals.get(event_id, checked)
    if not event["eligible"]:
        reasons.append("event is failed, closed, expired, suspended or not current")
    limits = RiskLimits()
    if symbol != sig.symbol or not math.isfinite(price) or price <= 0:
        reasons.append("invalid underlying quote")
    age = clock(checked) - clock(quote_at)
    if not pd.Timedelta(0) <= age <= limits.max_quote_age:
        reasons.append("quote is stale or from the future")
    long = sig.direction == "long"
    if (price <= sig.stop if long else price >= sig.stop):
        reasons.append("price breached the stop")
        if symbol == sig.symbol and math.isfinite(price) and price > 0 and pd.Timedelta(0) <= age <= limits.max_quote_age:
            log.signals.invalidate(event_id, checked, "fresh underlying quote breached the stop")
    if (price < event["entry_level"] if long else price > event["entry_level"]):
        reasons.append("price is on the wrong side of entry")
    moved = (price / sig.trigger - 1) * (1 if long else -1)
    chase = min(limits.max_already_moved_pct,
                CARDS[sig.setup_id].p("max_chase") if "max_chase" in CARDS[sig.setup_id].params else float("inf"))
    if moved > chase + 1e-12:
        reasons.append("price exceeds the existing chase limit")
    return {"event_id": event_id, "eligible": not reasons, "reasons": reasons,
            "checked_at": clock(checked).isoformat(), "card_version": event["card_version"],
            "expires_at": event["expires_at"], "purpose": "signal prerequisite only; not approval or order"}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the scan that is due now (for launchd or cron every 5 minutes).")
    ap.add_argument("--watchlist", default=os.environ.get("DESK_WATCHLIST", "data/watchlist.json"))
    ap.add_argument("--data-dir", default=os.environ.get("DESK_DATA_DIR", "data"))
    args = ap.parse_args(argv)
    from desk.webull import WebullData, WebullError

    try:
        from desk.action_source import configured_source
        source: BarSource = configured_source(WebullData.from_env(), os.environ)
    except BarDataError as e:
        why = str(e)

        class NoKeys:                                 # the due scan is still logged, as failed
            def security_metadata(self, symbols):
                raise BarDataError(f"no Webull connection: {why}")

            def bars(self, *a, **k):
                raise RuntimeError(f"no Webull connection: {why}")
        source = NoKeys()
    wl_path = Path(args.watchlist)
    watchlist = json.loads(wl_path.read_text()) if wl_path.exists() else ["SPY", "QQQ", "IWM"]
    rec = run(source, watchlist, ScanLog(Path(args.data_dir)), datetime.now(timezone.utc),
              decision_clock=lambda: datetime.now(timezone.utc))
    if rec is None:
        print("no scan due")
        return 0
    print(f"{rec.kind} scan {rec.slot}: {rec.scanned} names, {len(rec.armed)} armed, "
          f"{len(rec.triggered)} triggered, {len(rec.skipped)} skipped" + (f", ERROR {rec.error}" if rec.error else ""))
    return 1 if rec.error else 0


if __name__ == "__main__":
    sys.exit(main())
