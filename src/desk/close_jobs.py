"""Recoverable unfinished close preparation (G5a CP3 live-run package 2). No orders.

One job per completed source session prepares the next trading session's candidates.
It is created by the normal close+10-minute slot with a frozen universe (watchlist +
movers + bearish candidates) and retried by later invocations of the existing runner
until every recoverable name is prepared, a provider stop blocks it, or the target
session ends. No scheduler, background worker or polling loop is added; each runner
invocation makes at most one attempt per job and no request when the job is not due.

Operational choices are engineering defaults, not trading rules: the retry spacing
(``DESK_CLOSE_RECOVERY_SPACING_MINUTES``, default 5 = the documented runner cadence),
the 10-minute claim lease, and the deadline (the target session's close, the existing
candidate expiry). Run ``python -m desk.close_jobs status`` to inspect jobs and
``python -m desk.close_jobs resume`` after a provider stop has been resolved.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Callable, Mapping
from uuid import uuid4

from desk.bar_contract import LATEST_NOT_READY
from desk.calendar import clock, latest_closed_session, next_trading_day, session

LEASE = timedelta(minutes=10)
DEFAULT_SPACING_MINUTES = 5
RECOVERABLE = frozenset({"WAITING_LATEST_SESSION", "TRANSIENT_UNAVAILABLE", "WAITING_MARKET_REFERENCE"})
OPEN = ("PENDING", "PARTIAL")


def spacing(env: Mapping[str, str] | None = None) -> timedelta:
    """Retry spacing; an invalid configuration falls back to the documented default."""
    raw = (env if env is not None else os.environ).get("DESK_CLOSE_RECOVERY_SPACING_MINUTES", "")
    try:
        minutes = int(raw) if raw else DEFAULT_SPACING_MINUTES
    except ValueError:
        minutes = DEFAULT_SPACING_MINUTES
    return timedelta(minutes=minutes if 1 <= minutes <= 60 else DEFAULT_SPACING_MINUTES)


def classify(reason) -> str:
    """Outcome class for one symbol's refusal, from structured codes only."""
    text = str(reason)
    if LATEST_NOT_READY in text:
        return "WAITING_LATEST_SESSION"
    if "PROVIDER_STOP_HTTP_" in text or re.search(r"Webull HTTP (401|403|429)\b", text):
        return "PROVIDER_STOP"
    if "IDENTITY_STORE_UNAVAILABLE" in text:
        return "TRANSIENT_UNAVAILABLE"
    if re.search(r"IDENTITY|identity", text):
        return "IDENTITY_REFUSED"
    if text.startswith(("DAILY_PROVIDER_UNAVAILABLE", "MINUTE_PROVIDER_UNAVAILABLE", "no bars returned",
                        "metadata unavailable")):
        return "TRANSIENT_UNAVAILABLE"
    if re.search(r"ROW_INVALID|MISSING_OR_STALE|Stale daily|OUTSIDE_BOUNDS|non-session|No completed daily|"
                 r"don't contain|non-positive|non-numeric|bad bar row|duplicate bar|out of order|"
                 r"need at least|no bars$|Missing or non-session", text):
        return "INVALID_HISTORY"
    return "REFUSED"


def summary(job) -> dict | None:
    """Bounded diagnostic view of one job (no candidate payloads)."""
    if job is None:
        return None
    by_status: dict[str, list[str]] = {}
    for symbol, outcome in sorted(job["outcomes"].items()):
        by_status.setdefault(outcome["status"], []).append(symbol)
    unattempted = sorted(set(job["universe"]) - set(job["outcomes"]))
    if unattempted:
        by_status["NOT_ATTEMPTED"] = unattempted
    return {"source_session": job["source_session"], "target_session": job["target_session"],
            "status": job["status"], "coverage": job["coverage"], "attempts": job["attempts"],
            "last_attempt_at": job["last_attempt_at"], "next_attempt_at": job["next_attempt_at"],
            "blocked_reason": job["blocked_reason"], "published": bool(job["published"]),
            "market": job["market"], "universe_size": len(job["universe"]),
            "discovery": job["discovery"], "names_by_status": by_status,
            "reasons": {s: o.get("reason") for s, o in sorted(job["outcomes"].items()) if o.get("reason")},
            "volume_pending": job["volume_pending"], "last_attempt": (job["history"] or [None])[-1]}


def open_job(source, log, slot, now, watchlist: Mapping[str, list[str]], removed: set[str]):
    """Create (or return) the job for the slot's completed session, freezing its universe.

    Discovery runs once, before any lock; its errors are kept and make coverage PARTIAL.
    """
    from desk.watchlist import bearish_candidates, movers
    source_session = slot.date()
    job = log.signals.close_job(source_session)
    if job is not None:
        return job
    errors: dict[str, str] = {}
    moving = movers(source, errors)
    declining = bearish_candidates(source, errors)
    universe = {s: list(tags) for s, tags in watchlist.items()}
    for label, names in (("mover", moving), ("bearish", declining)):
        for symbol in names:
            if symbol not in removed:
                universe.setdefault(symbol, []).append(label)
    discovery = {"status": "PARTIAL" if errors else "READY", "source_errors": errors,
                 "captured_at": clock(now).isoformat()}
    return log.signals.create_close_job(source_session, next_trading_day(source_session), universe, discovery, now)


def _expired(job, now) -> str | None:
    source_session, target = date.fromisoformat(job["source_session"]), date.fromisoformat(job["target_session"])
    if clock(now) >= session(target)[1]:
        return "TARGET_SESSION_ENDED"
    if latest_closed_session(now) != source_session:
        return "SOURCE_SESSION_NO_LONGER_LATEST"
    return None


def attempt(source, log, source_session: date, now: datetime, *, removed: set[str], kind: str,
            decision_clock: Callable[[], datetime] | None = None, env: Mapping[str, str] | None = None):
    """One bounded attempt for a job; returns its ScanRecord (written by the caller)."""
    from desk.scanner import VOLUME_USAGE, ScanRecord, close_scan
    rec = ScanRecord(kind, clock(now).isoformat(), None)
    job = log.signals.close_job(source_session)
    why = _expired(job, now)
    if why:
        job = log.signals.end_close_job(source_session, now, "EXPIRED", why)
        rec.error = f"CLOSE_JOB_EXPIRED: {why}"
        rec.discovery["close_job"] = summary(job)
        return rec
    token = uuid4().hex
    claimed, reason, job = log.signals.claim_close_job(source_session, now, token, clock(now) + LEASE)
    rec.discovery.update(sources=job["universe"], source_errors=job["discovery"].get("source_errors", {}),
                         status=job["discovery"].get("status"))
    if not claimed:
        rec.error = f"CLOSE_JOB_{reason}" + (f": next attempt {job['next_attempt_at']}"
                                              if reason == "NOT_DUE" else "")
        rec.discovery["close_job"] = summary(job)
        rec.discovery["requests"] = 0
        return rec
    universe, done = job["universe"], job["outcomes"]
    todo = sorted(s for s in universe if s not in removed
                  and done.get(s, {}).get("status") in (None, *RECOVERABLE))
    outcomes = {s: {"status": "REMOVED", "attempt": job["attempts"]}
                for s in universe if s in removed and done.get(s, {}).get("status") != "PREPARED"}
    try:
        scan, _ = close_scan(source, todo, now, decision_clock=decision_clock, preparing=True)
        market_ok = scan.error is None
        prepared = set(scan.discovery.get("prepared", []))
        for symbol in todo:
            if symbol in prepared:
                outcomes[symbol] = ({"status": "PREPARED", "attempt": job["attempts"]} if market_ok else
                                    {"status": "WAITING_MARKET_REFERENCE", "attempt": job["attempts"],
                                     "reason": scan.error})
            else:
                text = scan.skipped.get(symbol, "no bars returned")
                outcomes[symbol] = {"status": classify(text), "attempt": job["attempts"], "reason": text[:300]}
        market_reason = None if market_ok else (scan.skipped.get("SPY") or scan.skipped.get("QQQ") or scan.error)
        market_class = None if market_ok else classify(market_reason)
        merged = {**done, **outcomes}
        stops = [s for s, o in outcomes.items() if o["status"] == "PROVIDER_STOP"]
        if market_class == "PROVIDER_STOP":
            stops.append("SPY/QQQ")
        recoverable = [s for s in universe if s not in removed
                       and merged.get(s, {}).get("status") in (None, *RECOVERABLE)]
        published = bool(job["published"]) or market_ok
        if stops:
            status = "BLOCKED"
        elif not market_ok and market_class not in RECOVERABLE and not job["published"]:
            status = "FAILED"  # the market reference itself is refused: nothing can be prepared
        elif recoverable or not published:
            status = "PARTIAL" if published else "PENDING"
        else:
            status = "FINISHED"
        full = (status == "FINISHED" and published and job["discovery"].get("status") == "READY"
                and all(merged.get(s, {}).get("status") in ("PREPARED", "REMOVED") for s in universe))
        coverage = "FULL" if full else "PARTIAL"
        target = date.fromisoformat(job["target_session"])
        next_at = clock(now) + spacing(env) if status in OPEN else None
        floor = now if clock(now) >= session(target)[0] else None  # no replay of earlier crossings
        record = {"at": clock(now).isoformat(), "attempt": job["attempts"], "kind": kind, "requested": todo,
                  "symbols_requested": len(todo) + 2, "market": "EVALUATED" if market_ok else market_class,
                  "market_reason": market_reason and str(market_reason)[:300],
                  "prepared": sorted(s for s in todo if outcomes[s]["status"] == "PREPARED"),
                  "alpaca_requests": scan.discovery.get(VOLUME_USAGE, {}).get("requests", 0), "status": status}
        committed = log.signals.commit_close_job(
            source_session, token, now, outcomes=outcomes, market=scan.market if market_ok else None,
            payload=scan.armed if market_ok else [], volume_pending=scan.discovery.get("volume_pending", [])
            if market_ok else [], status=status, coverage=coverage, next_attempt_at=next_at,
            blocked_reason=", ".join(f"{s}: {merged[s].get('reason') or market_reason}" if s in merged
                                     else f"{s}: {market_reason}" for s in stops)[:500] or None,
            floor_after=floor, record=record)
    except Exception as exc:
        log.signals.release_close_job(source_session, token, now, clock(now) + spacing(env),
                                      {"at": clock(now).isoformat(), "attempt": job["attempts"],
                                       "error": type(exc).__name__})
        raise
    rec.scanned, rec.skipped, rec.at = scan.scanned, scan.skipped, scan.at
    rec.market, rec.market_why = scan.market, scan.market_why
    if not committed:
        rec.error = "CLOSE_JOB_CLAIM_LOST: this attempt's results were discarded"
    elif not market_ok:
        rec.error = f"close preparation pending: {scan.error}"
    else:
        rec.armed, rec.qualification = scan.armed, scan.qualification
    rec.discovery["close_job"] = summary(log.signals.close_job(source_session))
    rec.discovery["requests"] = record["symbols_requested"]
    for key in ("prepared", "volume_pending", VOLUME_USAGE):
        if key in scan.discovery:
            rec.discovery[key] = scan.discovery[key]
    return rec


def recover(source, log, now: datetime, *, removed: set[str], decision_clock=None, env=None):
    """At most one attempt on the open job, if any; expired jobs are closed. Returns a record or None.

    ``None`` means nothing to do. A record with ``write`` False reports a job that is
    pending but not yet due (no request made); the caller prints it without logging.
    """
    for job in log.signals.close_jobs():
        if job["status"] not in (*OPEN, "BLOCKED"):
            continue
        source_session = date.fromisoformat(job["source_session"])
        if job["status"] == "BLOCKED":
            why = _expired(job, now)
            if why:
                ended = log.signals.end_close_job(source_session, now, "EXPIRED", why)
                return _report(ended, now, f"CLOSE_JOB_EXPIRED: {why}"), True
            continue  # blocked until an operator resumes it; no request
        rec = attempt(source, log, source_session, now, removed=removed, kind="close_recovery",
                      decision_clock=decision_clock, env=env)
        return rec, not (rec.error or "").startswith(("CLOSE_JOB_NOT_DUE", "CLOSE_JOB_CLAIMED"))
    return None, False


def _report(job, now, error):
    from desk.scanner import ScanRecord
    rec = ScanRecord("close_recovery", clock(now).isoformat(), None, error=error)
    rec.discovery["close_job"] = summary(job)
    return rec


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or resume close-preparation jobs (no requests).")
    parser.add_argument("command", choices=("status", "resume"))
    parser.add_argument("--data-dir", default=os.environ.get("DESK_DATA_DIR", "data"))
    parser.add_argument("--source-session", help="YYYY-MM-DD (resume: required)")
    parser.add_argument("--actor", default="operator")
    args = parser.parse_args(argv)
    from desk.signal_state import SignalStore
    path = Path(args.data_dir) / "signals.sqlite"
    if not path.exists():  # inspecting never creates a store
        print(json.dumps({"status": "NO_SIGNAL_STORE", "data_dir": str(args.data_dir)}))
        return 1
    store = SignalStore(path)
    now = datetime.now(timezone.utc)
    if args.command == "status":
        jobs = [summary(j) for j in store.close_jobs()
                if not args.source_session or j["source_session"] == args.source_session]
        print(json.dumps(jobs, indent=2, default=str))
        return 0
    if not args.source_session:
        parser.error("resume needs --source-session")
    job = store.resume_close_job(date.fromisoformat(args.source_session), now, args.actor)
    print(json.dumps(summary(job), indent=2, default=str))
    return 0 if job is not None and job["status"] in OPEN else 1


if __name__ == "__main__":
    raise SystemExit(main())
