"""Re-run retired setups on accepted history; never rescale saved levels.

One bounded batch per ordinary scanner invocation, no independent scheduler.
Missing evidence remains pending. Replacements wait for a subsequent crossing.
"""
from desk.calendar import clock, session, trading_day
from desk.playbook.filters import MarketSize
from desk.signal_state import candidate_id

EP = "5_qullamaggie_episodic_pivot"


def rebuild_pending(source, log, now, *, removed=(), decision_clock=None, rebuilt=None):
    from desk.scanner import close_scan, episodic_pivots

    day = clock(now).date()
    if not trading_day(day) or not session(day)[0] <= clock(now) < session(day)[1]:
        return []
    pending = log.signals.pending_rebuilds(day)
    outcomes = []
    active = []
    for request in pending:
        if request["symbol"] in removed:
            log.signals.finish_rebuild(request["candidate_id"],now,"REMOVED","removed from watchlist")
            outcomes.append({"symbol":request["symbol"],"setup_id":request["setup_id"],"status":"REMOVED"})
        else:
            active.append(request)
    if not active:
        return outcomes

    names = sorted({r["symbol"] for r in active})
    prepared, detected = close_scan(source,names,now,preparing=True,decision_clock=decision_clock)
    errors = dict(prepared.skipped)
    ep_names = sorted({r["symbol"] for r in active if r["setup_id"] == EP})
    if ep_names and not prepared.error:
        detected += episodic_pivots(source,MarketSize(prepared.market),now,errors,
                                   decision_clock=decision_clock,candidates=ep_names)
    checked = decision_clock() if decision_clock else now
    if clock(checked).date() != day or clock(checked) >= session(day)[1]:
        return outcomes + [{"symbol":r["symbol"],"setup_id":r["setup_id"],"status":"PENDING",
                            "reason":"entry session ended during rebuild"} for r in active]
    found = {(s.symbol,s.setup_id,s.direction):s for s in detected}
    for request in active:
        name, setup = request["symbol"], request["setup_id"]
        reason = prepared.error or errors.get(name) or errors.get(f"{name}/{setup}")
        if reason:
            status, replacement = "PENDING", None
        else:
            replacement = found.get((name,setup,request["direction"]))
            status = "REBUILT" if replacement else "NO_SETUP"
            reason = "fresh detection; await a new crossing" if replacement else "revised history no longer meets setup"
        if replacement is not None and candidate_id(replacement,day) == request["candidate_id"]:
            status, reason, replacement = "PENDING", "provider returned retired history; reconciliation required", None
        if log.signals.finish_rebuild(request["candidate_id"],checked,status,reason,replacement):
            if replacement is not None and rebuilt is not None:
                rebuilt.append(replacement)
            outcomes.append({"symbol":name,"setup_id":setup,"status":status,"reason":reason})
    return outcomes
