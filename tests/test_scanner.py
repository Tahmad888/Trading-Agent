import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from desk.bars import BarDataError
from desk.calendar import exchange
from desk.bar_contract import BarProvenance
from desk import scanner as sc
from desk.playbook.triggers import Signal
from tests.charts import chart, line
from tests.basis_support import price_evidence, volume_evidence
from tests.test_triggers import QULL, RSI2_DIP

ET = sc.ET
DAY = date(2026, 9, 29)                                  # a Tuesday


def daily(cl, spread=0.005, end=DAY):
    df = chart(cl, spread=spread)
    dates = exchange().sessions[exchange().sessions <= pd.Timestamp(end)][-len(df):]
    df.index = dates.tz_localize("America/New_York").tz_convert("UTC")
    return stamp(df, "D")


def stamp(df, timeframe, symbol="LEAD"):
    df.attrs["bar_provenance"] = BarProvenance(source="synthetic fixture", evidence_ref="test generator",
        timeframe=timeframe, timestamp_semantics="session_label" if timeframe == "D" else "start",
        session="regular", delay_minutes=0, adjustment="split_adjusted", price_scale_id="fixture-scale",
        price_basis=price_evidence(str(df.index[-1].tz_convert(ET).date()), symbol)).model_dump(mode="json")
    df.attrs["volume_basis"] = volume_evidence("synthetic " + timeframe)
    return df


class Fake:
    def __init__(self, frames, fail=(), lists=None):
        self.frames, self.fail, self.calls, self.lists = frames, set(fail), [], lists or {}

    def gainers(self, period):
        return self.lists.get(period, [])

    def most_active(self, by="TURNOVER"):
        return self.lists.get(by, [])

    def bars(self, symbols, *, category, timespan, count=1000, **kw):
        self.calls.append((tuple(symbols), category, timespan))
        if self.fail & set(symbols):
            raise BarDataError("Webull HTTP 429 rate limit")
        out = {s: self.frames[(s, timespan)].copy() for s in symbols if (s, timespan) in self.frames}
        for s, frame in out.items():
            basis = frame.attrs.get("bar_provenance", {}).get("price_basis")
            if basis:
                basis.update(symbol=s, security_id=f"fixture:{s}")
        return out


UP = line((0, 300), (len(QULL) - 1, 420))


def frames(end=DAY):
    return {("SPY", "D"): daily(UP, end=end), ("QQQ", "D"): daily(UP * 1.3, end=end),
            ("IWM", "D"): daily(RSI2_DIP[-len(QULL):], end=end), ("LEAD", "D"): daily(QULL, spread=0.02, end=end)}


def test_schedule():
    slots = sc.scheduled_slots(DAY)
    assert slots[0].time().isoformat() == "09:45:00" and slots[-2].time().isoformat() == "15:45:00"
    assert slots[-1].time() == sc.CLOSE_SCAN and len(slots) == 25 + 1
    assert sc.scheduled_slots(date(2026, 10, 3)) == []  # Saturday
    assert sc.scheduled_slots(date(2026, 11, 26)) == []  # Thanksgiving
    assert sc.next_trading_day(date(2026, 10, 2)) == date(2026, 10, 5)


def test_close_scan_arms_setups_and_logs_bad_names():
    src = Fake(frames(), fail={"BAD"})
    rec, armed = sc.close_scan(src, ["LEAD", "IWM", "BAD", "GONE"], datetime(2026, 9, 29, 16, 10, tzinfo=ET))
    assert rec.market == "full" and not rec.error
    assert "1_qullamaggie_breakout" in {s.setup_id for s in armed if s.symbol == "LEAD"}
    assert rec.skipped.keys() == {"BAD", "GONE"} and rec.scanned == 4
    assert {c[1] for c in src.calls} == {"US_ETF", "US_STOCK"}


def test_close_scan_refuses_yesterdays_bars():
    f = frames()
    f[("LEAD", "D")] = daily(QULL, spread=0.02, end=date(2026, 9, 28))
    rec, armed = sc.close_scan(Fake(f), ["LEAD"], datetime(2026, 9, 29, 16, 10, tzinfo=ET))
    assert "Stale daily data" in rec.skipped["LEAD"] and not armed


def test_close_scan_without_spy_arms_nothing():
    f = frames()
    del f[("SPY", "D")]
    rec, armed = sc.close_scan(Fake(f), ["LEAD"], datetime(2026, 9, 29, 16, 10, tzinfo=ET))
    assert rec.error and armed == []


def m15(rows, day=DAY):
    idx = pd.date_range(pd.Timestamp(f"{day} 09:30", tz="America/New_York"), periods=len(rows), freq="15min")
    return stamp(pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx.tz_convert("UTC")).assign(volume=1e5), "M15")


def sig(setup="1_qullamaggie_breakout", d="long", trigger=100.0, stop=95.0):
    return Signal(setup, "LEAD", d, pd.Timestamp("2026-09-28", tz=ET), trigger, stop,
                  price_scale_id="fixture-scale", price_basis=price_evidence("2026-09-28"))


def test_entry_rules():
    now = datetime(2026, 9, 29, 10, 0, tzinfo=ET)
    bars = m15([(99, 101, 98.5, 100.5), (100.5, 102, 100, 101.8)])
    hit, level, _ = sc.entry_hit(sig(), bars, now)
    assert hit and level == 101                            # first 15-minute high was above the trigger
    assert not sc.entry_hit(sig(), m15([(99, 101, 98.5, 100.5), (100.5, 100.9, 100, 100.2)]), now)[0]
    luk = sig("7_luk_pullback_reclaim")
    assert sc.entry_hit(luk, m15([(100.5, 101, 99, 99.5), (99.5, 100.6, 99.4, 100.4)]), now)[0]
    assert not sc.entry_hit(luk, m15([(100.5, 101, 100.1, 100.6)]), now.replace(hour=9, minute=45))[0]
    short = sig("8_raschke_holy_grail", "short", 100, 104)
    assert sc.entry_hit(short, m15([(101, 101.5, 99.5, 100), (100, 100.2, 99, 99.2)]), now)[0]


def test_run_writes_one_line_per_slot_and_the_funnel_counts_it(tmp_path):
    log = sc.ScanLog(tmp_path)
    src = Fake({**frames(), ("LEAD", "M15"): m15([(147, 148.5, 146.5, 148), (148, 153, 148, 152.5)])})
    close = sc.run(src, ["LEAD", "IWM"], log, datetime(2026, 9, 29, 16, 12, tzinfo=ET))
    assert close.kind == "close" and close.armed
    assert sc.run(src, ["LEAD"], log, datetime(2026, 9, 29, 16, 20, tzinfo=ET)) is None   # already ran
    # Next morning, the armed setups are checked on 15-minute bars.
    f30 = {**src.frames, ("LEAD", "M15"): m15([(147, 148.5, 146.5, 148), (148, 153, 148, 152.5)], date(2026, 9, 30))}
    intraday = sc.run(Fake(f30), ["LEAD"], log, datetime(2026, 9, 30, 10, 1, tzinfo=ET))
    assert intraday.kind == "intraday" and intraday.slot.startswith("2026-09-30T10:00")
    assert any(t["setup_id"] == "1_qullamaggie_breakout" for t in intraday.triggered)
    assert sc.run(src, ["LEAD"], log, datetime(2026, 9, 30, 20, 0, tzinfo=ET)) is None   # outside the schedule
    fun = sc.funnel(log.records(), date(2026, 9, 28), date(2026, 10, 2))
    assert fun["scans_scheduled"] == 5 * 26 + 1 and fun["scans_run"] == 2
    assert fun["setups_armed"] >= 1 and fun["entries_triggered"] >= 1
    assert len(log.path.read_text().splitlines()) == 2


def test_a_crash_is_logged_as_a_failed_scan(tmp_path):
    class Broken:
        def bars(self, *a, **k):
            raise RuntimeError("boom")
    log = sc.ScanLog(tmp_path)
    rec = sc.run(Broken(), ["LEAD"], log, datetime(2026, 9, 29, 16, 10, tzinfo=ET))
    assert rec.error and "boom" in rec.error
    assert sc.funnel(log.records(), DAY, DAY)["scans_failed"] == [rec.slot]


def test_main_logs_a_failed_scan_when_keys_are_missing(tmp_path, monkeypatch):
    for k in ("WEBULL_APP_KEY", "WEBULL_APP_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sc, "datetime", type("D", (datetime,), {"now": staticmethod(lambda tz=None: datetime(2026, 9, 29, 20, 11, tzinfo=tz))}))
    assert sc.main(["--data-dir", str(tmp_path), "--watchlist", str(tmp_path / "none.json")]) == 1
    assert "no Webull connection" in sc.ScanLog(tmp_path).records()[0]["error"]


def test_friday_leader_scan_writes_the_watchlist(tmp_path):
    log = sc.ScanLog(tmp_path)
    (tmp_path / "taz-picks.json").write_text(json.dumps({"add": ["TSLA"], "remove": []}))
    lists = {"MONTH_3": [{"symbol": "LEAD", "price": "148"}, {"symbol": "PENNY", "price": "2"}],
             "TURNOVER": [{"symbol": "SPY", "price": "400"}, {"symbol": "BRK.B", "price": "480"}]}
    stale = sc.run(Fake(frames(), lists=lists), [], log, datetime(2026, 10, 2, 16, 41, tzinfo=ET))
    assert "no SPY bars for today" in stale.error        # Tuesday's bars on a Friday: nothing written
    rec = sc.run(Fake(frames(date(2026, 10, 2)), lists=lists), [], log, datetime(2026, 10, 2, 16, 42, tzinfo=ET))
    assert rec.kind == "leader" and not rec.error, rec.error
    assert [l["symbol"] for l in rec.leaders] == ["LEAD"]
    wl = json.loads((tmp_path / "watchlist.json").read_text())
    assert wl["TSLA"] == ["Taz"] and "SPY" in wl and "PENNY" not in wl


def test_morning_movers_become_episodic_pivots(tmp_path):
    from tests.test_triggers import EP
    log = sc.ScanLog(tmp_path)
    log.save_armed(DAY, sc.ScanRecord("close", "", None, market="full"))
    d = daily(np.r_[EP, EP[-1]], spread=0.02)             # the last bar is today's, and is dropped
    m = m15([(57.5, 58.5, 57, 58.2), (58.2, 58.4, 57.8, 58.1)]).assign(volume=8e5)
    f = {("GAP", "D"): d, ("GAP", "M15"): m}
    lists = {"PRE_MARKET": [{"symbol": "GAP", "price": "57.5", "change_ratio": "0.13"}]}
    rec = sc.run(Fake(f, lists=lists), [], log, datetime(2026, 9, 29, 10, 2, tzinfo=ET))
    assert [a["setup_id"] for a in rec.armed] == ["5_qullamaggie_episodic_pivot"]
    assert log.load_armed(DAY)[1][0].symbol == "GAP"


def test_episodic_pivot_uses_first_observed_opening_range_breakout():
    ep = sig("5_qullamaggie_episodic_pivot", trigger=57.5, stop=54)
    bars = m15([(57.5, 58.5, 57, 58.2), (58.2, 58.4, 57.8, 58.1), (58.1, 58.3, 57.9, 58.0), (58, 58.6, 57.9, 58.5),
                (58.5, 58.55, 58.2, 58.3), (58.3, 58.45, 58.1, 58.2), (58.2, 58.7, 58.1, 58.6)])
    hit, level, why = sc.entry_hit(ep, bars, datetime(2026, 9, 29, 11, 15, tzinfo=ET))
    assert hit and level == 58.5 and "15-minute" in why
    assert "2026-09-29T14:15:00+00:00" in why  # 10:15 ET; not the later crossing.
