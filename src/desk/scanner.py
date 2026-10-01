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


def _sig(s: Signal, **extra) -> dict:
    d = asdict(s)
    d["as_of"] = s.as_of.isoformat()
    return {**d, **extra}


def close_scan(source: BarSource, watchlist: Sequence[str], now: datetime, *, decision_clock: Callable[[], datetime] | None = None) -> tuple[ScanRecord, list[Signal]]:
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
            if clock(now) < session(clock(now).date())[1]:
                raise BarDataError("close scan requires a completed session")
            df = completed_daily(df, now)
            price_basis(df, now, symbol=sym)
            require(df, min_bars=MIN_DAILY_BARS)
            bars[sym] = df
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
            armed += [replace(sig, price_scale_id=provenance(bars[sym], "D").price_scale_id,
                              price_basis=price_basis(bars[sym], now, symbol=sym).model_dump(mode="json"))
                      for sig in scan(feats[sym], Context(sym, size, gate, spy), skipped=rec.skipped)]
        except BarDataError as e:
            rec.skipped[sym] = str(e)
    rec.armed = [_sig(s) for s in armed]
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


def intraday_scan(source: BarSource, armed: Sequence[Signal], now: datetime, *, decision_clock: Callable[[], datetime] | None = None) -> ScanRecord:
    rec = ScanRecord("intraday", now.astimezone(ET).isoformat(), None)
    symbols = sorted({s.symbol for s in armed})
    bars = fetch(source, symbols, "M15", 40, rec.skipped) if symbols else {}
    now = decision_clock() if decision_clock else now
    rec.at = clock(now).isoformat()
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


def episodic_pivots(source: BarSource, market: MarketSize, now: datetime, skipped: dict[str, str], *, decision_clock: Callable[[], datetime] | None = None) -> list[Signal]:
    """At 10:00: this morning's movers checked for an episodic pivot, from the gap and the first 30 minutes."""
    from desk.watchlist import movers

    today = now.astimezone(ET).date()
    names = movers(source, skipped)
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
            if len(m) < 2 or len(d) < MIN_DAILY_BARS:
                raise BarDataError("not enough bars for the episodic pivot")
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


def rsi2_estimate(source: BarSource, watchlist: Sequence[str], market: MarketSize, now: datetime,
                  skipped: dict[str, str] | None = None, *, decision_clock: Callable[[], datetime] | None = None) -> list[Signal]:
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
                out.append(replace(sig, saw=evidence, price_scale_id=provenance(df, "D").price_scale_id,
                                   price_basis=basis.model_dump(mode="json")))
        except BarDataError as exc:
            skipped[sym] = str(exc)
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
    now = clock(now)
    past = [s for s in scheduled_slots(now.date()) if s <= now < s + timedelta(minutes=15)]
    return past[-1] if past else None


def run(source: BarSource, watchlist: Sequence[str], log: ScanLog, now: datetime, *, decision_clock: Callable[[], datetime] | None = None) -> ScanRecord | None:
    slot = due_slot(now)
    if slot is None:
        return None
    if any(r.get("slot") == slot.isoformat() and not r.get("error") for r in log.records()):
        return None                                   # this slot already ran
    closed = session(slot.date())[1]
    close_slot, leader_slot = closed + timedelta(minutes=10), closed + timedelta(minutes=40)
    try:
        if slot == leader_slot:
            rec = leader_scan_job(source, log, now, decision_clock=decision_clock)
        elif slot == close_slot:
            rec, _ = close_scan(source, watchlist, now, decision_clock=decision_clock)
            log.save_armed(next_trading_day(slot.date()), rec)
        else:
            market, armed = log.load_armed(slot.date())
            new: list[Signal] = []
            if slot.time() == EP_SCAN and market is not None:
                skipped: dict[str, str] = {}
                new = episodic_pivots(source, market, now, skipped, decision_clock=decision_clock)
                log.add_armed(slot.date(), market, new)
                armed = [*armed, *new]
            rec = intraday_scan(source, armed, now, decision_clock=decision_clock)
            if slot.time() == EP_SCAN and market is not None:
                rec.skipped.update(skipped)
            if slot == closed - timedelta(minutes=15) and market is not None:
                new = rsi2_estimate(source, watchlist, market, now, rec.skipped, decision_clock=decision_clock)
            rec.armed = [_sig(s) for s in new]
    except Exception as e:                            # any failure is logged as a failed scan, never silent
        kind = "leader" if slot == leader_slot else "close" if slot == close_slot else "intraday"
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
        from desk.action_source import configured_source
        source: BarSource = configured_source(WebullData.from_env(), os.environ)
    except BarDataError as e:
        why = str(e)

        class NoKeys:                                 # the due scan is still logged, as failed
            def bars(self, *a, **k):
                raise RuntimeError(f"no Webull connection: {why}")
        source = NoKeys()
    wl_path = Path(args.watchlist)
    watchlist = list(json.loads(wl_path.read_text())) if wl_path.exists() else ["SPY", "QQQ", "IWM"]
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
