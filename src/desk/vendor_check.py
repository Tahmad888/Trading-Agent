"""Read-only G3 provider check. No scanner run, signals, orders or activation.

Writes only the chosen audit database and optional report. Uses existing Webull
credentials; never prints them. Unresolved action/volume evidence stays unknown.
"""
import argparse
from datetime import datetime, timezone
import json
import os
import sqlite3
from pathlib import Path

from desk.action_source import configured_source
from desk.bar_contract import completed_daily, completed_intraday, check_price_scale
from desk.bars import BarDataError
from desk.calendar import clock, latest_closed_session, session, trading_day
from desk.data_basis import price_basis, volume_basis, compatible_volume
from desk.scanner import fetch
from desk.security import securities
from desk.webull import WebullData


def check(source, symbols, *, require_volume=False, clock_fn=lambda:datetime.now(timezone.utc)):
    result = {"purpose":"vendor price/history check only; no decision/order activation", "checks":[],
              "action_coverage":"NOT_ATTESTED", "fallback_issue":getattr(source,"fallback_issue",None),
              "auto_action_issue":getattr(source,"auto_action_issue",None), "requires_volume":require_volume}
    skipped = {}
    metadata = securities(source,symbols,skipped)
    dailies = fetch(source,symbols,"D",1000,skipped)
    for symbol in symbols:
        item = {"symbol":symbol,"status":"UNAVAILABLE"}
        try:
            if symbol not in dailies:
                raise BarDataError(skipped.get(symbol,"Daily data unavailable"))
            now = clock_fn()
            daily = completed_daily(dailies[symbol],now)
            basis = price_basis(daily,now,symbol=symbol)
            stamp = clock(now)
            in_session = trading_day(stamp.date()) and session(stamp.date())[0] <= stamp < session(stamp.date())[1]
            day = stamp.date() if in_session else latest_closed_session(now)
            opened,closed = session(day)
            frames = source.bars([symbol],timespan="M15",category=metadata[symbol].bar_category,count=40,
                start_time=int(opened.timestamp()*1000),end_time=int(closed.timestamp()*1000)-1,sessions="RTH")
            if symbol not in frames:
                raise BarDataError(getattr(source,"last_errors",{}).get(symbol,"Minute data unavailable"))
            now = clock_fn()
            minutes = completed_intraday(frames[symbol],now,session_day=day)
            if minutes.empty:
                raise BarDataError("No completed regular-session bars yet")
            check_price_scale(basis.model_dump(mode="json"),minutes,now,symbol=symbol)
            item.update(status="PASS",checked_at=clock(now).isoformat(),daily_rows=len(daily),
                minute_rows=len(minutes),security_id=basis.security_id,
                timing_scope="current regular session" if in_session else "completed historical session",
                price_method=getattr(basis,"method","reviewed_action_ledger"))
            try:
                volume_basis(daily.iloc[-50:])
                item["volume_window"] = "VERIFIED_DAILY_ONLY"
                if require_volume:
                    early = volume_basis(minutes.iloc[:2])
                    compatible_volume(daily.iloc[-50:],early.model_dump(mode="json"))
                    item["volume_pair"] = "PASS"
                    item["volume_share_basis"] = early.share_basis_id
            except BarDataError as exc:
                if require_volume:
                    item["volume_pair"] = "UNAVAILABLE"
                    item["volume_reason"] = str(exc)
                item["volume_window"] = "UNAVAILABLE_SEPARATE_EVIDENCE_REQUIRED"
        except BarDataError as exc:
            item["reason"] = str(exc)
        result["checks"].append(item)
    result["status"] = "PASS" if result["checks"] and all(c["status"]=="PASS" and (not require_volume or c.get("volume_pair")=="PASS") for c in result["checks"]) else "INCOMPLETE"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database",required=True)
    parser.add_argument("--symbols",nargs="+",required=True)
    parser.add_argument("--output")
    parser.add_argument("--auto-actions-database")
    parser.add_argument("--accept-native-volume-policy",action="store_true",
                        help="For this probe only: accept the explicit native post-split window inference")
    parser.add_argument("--require-volume",action="store_true")
    args = parser.parse_args(argv)
    try:
        env = {**os.environ,"DESK_VENDOR_BASIS_DB":args.database}
        if args.auto_actions_database:
            env["DESK_AUTO_ACTION_DB"] = args.auto_actions_database
        if args.accept_native_volume_policy:
            from desk.batch_actions import POLICY
            env["DESK_NATIVE_VOLUME_POLICY"] = POLICY
        source = configured_source(WebullData.from_env(),env)
        result = check(source,list(dict.fromkeys(args.symbols)),require_volume=args.require_volume)
    except (BarDataError,OSError,ValueError,KeyError,sqlite3.Error):
        result = {"status":"UNAVAILABLE","reason":"CONFIGURATION_OR_PROVIDER_UNAVAILABLE"}
    text = json.dumps(result,indent=2)
    if args.output:
        Path(args.output).write_text(text+"\n")
    print(text)
    return 0 if result["status"]=="PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
