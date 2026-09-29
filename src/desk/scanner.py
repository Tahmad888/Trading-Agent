"""The scanner: checks the watchlist for the 10 setups on a schedule and logs every scan.

Blueprint v2.4 sections 3 and 5, build step 4; serves the watch step. Runs on
the iMac (Taz, 29 Sep 2026). The scans:

- After the close (16:10 ET): daily bars for every watchlist name, the market
  filter from SPY and QQQ, the Trend Template, and the 10 daily checks. The
  setups armed for the next session are saved.
- In session, every 15 minutes from 09:45 to 15:45 ET: 15-minute bars for the
  armed names, to see whether each entry rule fired. At 15:45 the RSI(2) check
  also runs on the day's bars so far, as an estimate of the close.
- At 10:00, this morning's movers (Webull's top gainers and unusual volume)
  are checked for an episodic pivot, which is then watched like the rest.
- Fridays at 16:40 ET, the leader scan ranks Webull's top-200 lists (1 week to
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
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from desk.bars import BarDataError, require
from desk.indicators import daily_features
from desk.playbook.cards import CARDS, INDEX_ETFS
from desk.playbook.filters import MarketSize, market_filter, trend_template
from desk.playbook.triggers import Context, Signal, connors_rsi2, episodic_pivot, scan

ET = ZoneInfo("America/New_York")
DAILY_BARS = 1000                 # rule 6: warm-up for the 200-day and the 52-week values
MIN_DAILY_BARS = 260
CLOSE_SCAN = time(16, 10)
LEADER_SCAN = time(16, 40)        # Fridays: the weekly leader scan builds next week's watchlist
EP_SCAN = time(10, 0)             # the first 30 minutes are in: check this morning's movers
FIRST_INTRADAY, LAST_INTRADAY = time(9, 45), time(15, 45)
EP_CUTOFF = time(11, 0)           # [Sourced] Kullamägi: first 1-hour high, by 11:00

# [Assumption] NYSE full-day holidays as published on nyse.com for 2026-2027,
# copied by hand; the snapshot's trading status is the live check.
HOLIDAYS = {date(2026, d[0], d[1]) for d in [(1, 1), (1, 19), (2, 16), (4, 3), (5, 25), (6, 19), (7, 3),
                                              (9, 7), (11, 26), (12, 25)]} | \
           {date(2027, d[0], d[1]) for d in [(1, 1), (1, 18), (2, 15), (3, 26), (5, 31), (6, 18), (7, 5),
                                              (9, 6), (11, 25), (12, 24)]}


class BarSource(Protocol):
    def bars(self, symbols: Sequence[str], *, category: str, timespan: str, count: int = 1000,
             **kw) -> dict[str, pd.DataFrame]: ...


def trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def scheduled_slots(d: date) -> list[datetime]:
    """Every scan due on a trading day, in Eastern time."""
    if not trading_day(d):
        return []
    slots, t = [], datetime.combine(d, FIRST_INTRADAY, ET)
    while t.time() <= LAST_INTRADAY:
        slots.append(t)
        t += timedelta(minutes=15)
    slots.append(datetime.combine(d, CLOSE_SCAN, ET))
    if d.weekday() == 4:
        slots.append(datetime.combine(d, LEADER_SCAN, ET))
    return slots


def category(symbol: str) -> str:
    return "US_ETF" if symbol in INDEX_ETFS else "US_STOCK"


def fetch(source: BarSource, symbols: Sequence[str], timespan: str, count: int,
          skipped: dict[str, str]) -> dict[str, pd.DataFrame]:
    """Bars in batches of 20 per category. A name that fails is skipped and logged."""
    out: dict[str, pd.DataFrame] = {}
    for cat in ("US_ETF", "US_STOCK"):
        names = [s for s in symbols if category(s) == cat]
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


def _sig(s: Signal, **extra) -> dict:
    d = asdict(s)
    d["as_of"] = s.as_of.isoformat()
    return {**d, **extra}


def close_scan(source: BarSource, watchlist: Sequence[str], now: datetime) -> tuple[ScanRecord, list[Signal]]:
    rec = ScanRecord("close", now.astimezone(ET).isoformat(), None)
    symbols = sorted(set(watchlist) | {"SPY", "QQQ"})
    bars = fetch(source, symbols, "D", DAILY_BARS, rec.skipped)
    for sym in symbols:
        if sym not in bars:
            rec.skipped.setdefault(sym, "no bars returned")
    feats: dict[str, pd.DataFrame] = {}
    for sym, df in bars.items():
        try:
            require(df, min_bars=MIN_DAILY_BARS)
            if df.index[-1].tz_convert(ET).date() != now.astimezone(ET).date():
                raise BarDataError(f"last daily bar is {df.index[-1].date()}, not today's")
            feats[sym] = daily_features(df)
        except BarDataError as e:
            rec.skipped[sym] = str(e)
    rec.scanned = len(feats)
    if "SPY" not in feats or "QQQ" not in feats:
        rec.error = "no SPY or QQQ bars: nothing armed"
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
            gate = None if sym in INDEX_ETFS else trend_template(feats[sym], spy)
            armed += scan(feats[sym], Context(sym, size, gate, spy))
        except BarDataError as e:
            rec.skipped[sym] = str(e)
    rec.armed = [_sig(s) for s in armed]
    return rec, armed


def entry_hit(sig: Signal, m15: pd.DataFrame, now: datetime) -> tuple[bool, float, str]:
    """Did today's 15-minute bars fire this signal's entry rule? Returns (hit, level, why)."""
    today = m15[m15.index.tz_convert(ET).date == now.astimezone(ET).date()]
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
    if setup == "5_qullamaggie_episodic_pivot":
        # Before 11:00 the first 15-minute high; after that the first hour's high; day 1 only.
        early = later[later.index.tz_convert(ET).time < EP_CUTOFF]
        first_hour = float(today["high"].iloc[:4].max())
        if (early["high"] > first["high"]).any():
            return True, float(first["high"]), "took out the first 15-minute high before 11:00"
        late = later[later.index.tz_convert(ET).time >= EP_CUTOFF]
        return bool((late["high"] > first_hour).any()), first_hour, "took out the first hour's high after 11:00"
    if setup == "9_weinstein_stage4_breakdown":
        return bool(first["close"] < sig.trigger), float(first["close"]), "first 15 minutes still under support"
    if sig.direction == "long":
        level = max(sig.trigger, float(first["high"]))
        return bool((later["high"] > level).any()), level, "took out the first 15-minute high and the trigger"
    level = min(sig.trigger, float(first["low"]))
    return bool((later["low"] < level).any()), level, "broke the first 15-minute low and the trigger"


def intraday_scan(source: BarSource, armed: Sequence[Signal], now: datetime) -> ScanRecord:
    rec = ScanRecord("intraday", now.astimezone(ET).isoformat(), None)
    symbols = sorted({s.symbol for s in armed})
    bars = fetch(source, symbols, "M15", 40, rec.skipped) if symbols else {}
    rec.scanned = len(bars)
    for sig in armed:
        # RSI(2) enters near the close on the day it sets up (the 15:45 ticket), never the next morning.
        if sig.symbol not in bars or sig.setup_id == "10_connors_rsi2":
            continue
        try:
            hit, level, why = entry_hit(sig, bars[sig.symbol], now)
        except (BarDataError, KeyError) as e:
            rec.skipped[sig.symbol] = str(e)
            continue
        if hit:
            rec.triggered.append(_sig(sig, entry_level=round(level, 2), why=why))
    return rec


def episodic_pivots(source: BarSource, market: MarketSize, now: datetime, skipped: dict[str, str]) -> list[Signal]:
    """At 10:00: this morning's movers checked for an episodic pivot, from the gap and the first 30 minutes."""
    from desk.watchlist import movers

    today = now.astimezone(ET).date()
    names = movers(source, skipped)
    daily = fetch(source, names, "D", DAILY_BARS, skipped)
    intraday = fetch(source, names, "M15", 40, skipped)
    out = []
    for sym in names:
        if sym not in daily or sym not in intraday:
            continue
        d, m = daily[sym], intraday[sym]
        d = d[d.index.tz_convert(ET).date < today]            # yesterday's close is the gap's base
        m = m[m.index.tz_convert(ET).date == today]
        if len(m) < 2 or len(d) < MIN_DAILY_BARS:
            skipped.setdefault(sym, "not enough bars for the episodic pivot")
            continue
        try:
            ctx = Context(sym, market, None, today_open=float(m["open"].iloc[0]),
                          early_volume=float(m["volume"].iloc[:2].sum()))
            sig = episodic_pivot(daily_features(d), CARDS["5_qullamaggie_episodic_pivot"], ctx)
        except BarDataError as e:
            skipped[sym] = str(e)
            continue
        if sig:
            out.append(sig)
    return out


def leader_scan_job(source: BarSource, log: "ScanLog", now: datetime) -> ScanRecord:
    """Fridays after the close: rank the universe and write next week's watchlist with Taz's picks."""
    from desk.watchlist import build_watchlist, leader_scan, universe

    rec = ScanRecord("leader", now.astimezone(ET).isoformat(), None)
    names = universe(source, rec.skipped)
    bars = fetch(source, sorted(set(names) | {"SPY"}), "D", DAILY_BARS, rec.skipped)
    if "SPY" not in bars or bars["SPY"].index[-1].tz_convert(ET).date() != now.astimezone(ET).date():
        rec.error = "no SPY bars for today: last week's watchlist stays"
        return rec
    scan_ = leader_scan({s: b for s, b in bars.items() if s != "SPY"}, bars["SPY"]["close"])
    rec.skipped.update(scan_.skipped)
    rec.scanned = scan_.ranked
    if not scan_.leaders:
        rec.error = "no leaders found: last week's watchlist stays"
        return rec
    picks = log.picks()
    wl = build_watchlist([l.symbol for l in scan_.leaders], added=picks.get("add", []),
                         removed=picks.get("remove", []))
    log.write_watchlist(wl)
    rec.leaders = [{"symbol": l.symbol, "score": l.score, **l.returns} for l in scan_.leaders]
    return rec


def rsi2_estimate(source: BarSource, watchlist: Sequence[str], market: MarketSize, now: datetime) -> list[Signal]:
    """At 15:45: RSI(2) on daily bars with today's bar so far standing in for the close [Assumption]."""
    etfs = [s for s in watchlist if s in INDEX_ETFS]
    skipped: dict[str, str] = {}
    bars = fetch(source, etfs, "D", DAILY_BARS, skipped)
    out = []
    for sym, df in bars.items():
        try:
            out += [s for s in [connors_rsi2(daily_features(df), CARDS["10_connors_rsi2"],
                                             Context(sym, market))] if s]
        except BarDataError:
            continue
    return out


class ScanLog:
    """One JSON line per scan in <data dir>/scan-log.jsonl, plus the armed list for the next session."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

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

    def write_watchlist(self, wl: Mapping[str, list[str]]) -> None:
        (self.root / "watchlist.json").write_text(json.dumps(wl, indent=1))

    def add_armed(self, day: date, market: MarketSize, sigs: Sequence[Signal]) -> None:
        old_market, old = self.load_armed(day)
        rec = ScanRecord("intraday", "", None, market=(old_market or market).value,
                         armed=[_sig(s) for s in [*old, *sigs]])
        self.save_armed(day, rec)

    def save_armed(self, for_day: date, rec: ScanRecord) -> None:
        (self.root / f"armed-{for_day.isoformat()}.json").write_text(
            json.dumps({"market": rec.market, "signals": rec.armed}, indent=1))

    def load_armed(self, day: date) -> tuple[MarketSize | None, list[Signal]]:
        p = self.root / f"armed-{day.isoformat()}.json"
        if not p.exists():
            return None, []
        data = json.loads(p.read_text())
        sigs = [Signal(**{**s, "as_of": pd.Timestamp(s["as_of"])}) for s in data["signals"]]
        return (MarketSize(data["market"]) if data["market"] else None), sigs


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not trading_day(d):
        d += timedelta(days=1)
    return d


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
        "entries_triggered": sum(len(r.get("triggered", [])) for r in ran),
        "names_skipped": sorted({s for r in ran for s in r.get("skipped", {})}),
    }


def due_slot(now: datetime) -> datetime | None:
    """The latest slot at or before now, within 15 minutes; None outside the schedule."""
    now = now.astimezone(ET)
    past = [s for s in scheduled_slots(now.date()) if s <= now < s + timedelta(minutes=15)]
    return past[-1] if past else None


def run(source: BarSource, watchlist: Sequence[str], log: ScanLog, now: datetime) -> ScanRecord | None:
    slot = due_slot(now)
    if slot is None:
        return None
    if any(r.get("slot") == slot.isoformat() and not r.get("error") for r in log.records()):
        return None                                   # this slot already ran
    try:
        if slot.time() == LEADER_SCAN:
            rec = leader_scan_job(source, log, now)
        elif slot.time() == CLOSE_SCAN:
            rec, _ = close_scan(source, watchlist, now)
            log.save_armed(next_trading_day(slot.date()), rec)
        else:
            market, armed = log.load_armed(slot.date())
            new: list[Signal] = []
            if slot.time() == EP_SCAN and market is not None:
                skipped: dict[str, str] = {}
                new = episodic_pivots(source, market, now, skipped)
                log.add_armed(slot.date(), market, new)
                armed = [*armed, *new]
            rec = intraday_scan(source, armed, now)
            if slot.time() == EP_SCAN and market is not None:
                rec.skipped.update(skipped)
            if slot.time() == LAST_INTRADAY and market is not None:
                new = rsi2_estimate(source, watchlist, market, now)
            rec.armed = [_sig(s) for s in new]
    except Exception as e:                            # any failure is logged as a failed scan, never silent
        kind = {LEADER_SCAN: "leader", CLOSE_SCAN: "close"}.get(slot.time(), "intraday")
        rec = ScanRecord(kind, now.isoformat(), None, error=f"{type(e).__name__}: {e}")
    rec.slot = slot.isoformat()
    log.write(rec)
    return rec


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the scan that is due now (for launchd or cron every 5 minutes).")
    ap.add_argument("--watchlist", default=os.environ.get("DESK_WATCHLIST", "data/watchlist.json"))
    ap.add_argument("--data-dir", default=os.environ.get("DESK_DATA_DIR", "data"))
    args = ap.parse_args(argv)
    from desk.webull import WebullData, WebullError

    try:
        source: BarSource = WebullData.from_env()
    except WebullError as e:
        why = str(e)

        class NoKeys:                                 # the due scan is still logged, as failed
            def bars(self, *a, **k):
                raise RuntimeError(f"no Webull connection: {why}")
        source = NoKeys()
    wl_path = Path(args.watchlist)
    watchlist = list(json.loads(wl_path.read_text())) if wl_path.exists() else ["SPY", "QQQ", "IWM"]
    rec = run(source, watchlist, ScanLog(Path(args.data_dir)), datetime.now(timezone.utc))
    if rec is None:
        print("no scan due")
        return 0
    print(f"{rec.kind} scan {rec.slot}: {rec.scanned} names, {len(rec.armed)} armed, "
          f"{len(rec.triggered)} triggered, {len(rec.skipped)} skipped" + (f", ERROR {rec.error}" if rec.error else ""))
    return 1 if rec.error else 0


if __name__ == "__main__":
    sys.exit(main())
