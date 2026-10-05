"""Opt-in Webull price consistency, without a claimed complete action feed.

Native prices are never adjusted here. A mismatched ticker needs independent
source evidence; a new adjusted history never repairs an already-armed signal.
"""
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pandas as pd

from desk.bar_contract import BarProvenance
from desk.bars import BarDataError, validate
from desk.calendar import ET, clock, latest_closed_session, session, sessions
from desk.data_basis import VendorPriceBasis
from desk.history_scope import (DISCOVERY_POLICY, FULL_HISTORY, MAX_DEFECTS, ScopeUnsupported, ScopeWindow,
                                discovery_scope)
from desk.security import securities

REFERENCE = "https://developer.webull.com/apis/docs/reference/historical-bars/"


class VendorHistoryStore:
    """Vendor price evidence, kept per history scope (G5a checkpoint 3).

    ``current`` is the full-history pointer (legacy rows and NULL observation scopes
    are full history). A scoped consumer (discovery) has its own ``scoped_current``
    pointer and observations, so its success never replaces full-history evidence or
    clears a full-history failure. Revisions seen by any scope stay visible.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS identities (
                    host TEXT, symbol TEXT, identity TEXT NOT NULL,
                    PRIMARY KEY(host,symbol));
                CREATE TABLE IF NOT EXISTS snapshots (
                    digest TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS current (
                    host TEXT, symbol TEXT, digest TEXT NOT NULL, at TEXT NOT NULL,
                    PRIMARY KEY(host,symbol));
                CREATE TABLE IF NOT EXISTS observations (
                    sequence INTEGER PRIMARY KEY, host TEXT, symbol TEXT, at TEXT,
                    status TEXT, detail TEXT, digest TEXT);
                CREATE TABLE IF NOT EXISTS scoped_current (
                    host TEXT, symbol TEXT, scope TEXT, scope_digest TEXT NOT NULL,
                    digest TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(host,symbol,scope));
                CREATE TABLE IF NOT EXISTS history_defects (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, symbol TEXT NOT NULL,
                    instrument_id TEXT, scope TEXT NOT NULL, scope_digest TEXT, timeframe TEXT NOT NULL,
                    received_at TEXT, requested TEXT NOT NULL, required TEXT NOT NULL,
                    row_index INTEGER, row_time TEXT, field TEXT, reason TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('EXCLUDED_OUTSIDE_SCOPE','REJECTED_REQUIRED_DATA')),
                    evidence_digest TEXT NOT NULL, row_json TEXT, observed_at TEXT NOT NULL);
                -- Audit history only (written by 1dea7d4 from reply row counts); never read
                -- as listing-origin authority (coverage-origin repair, 2026-10-05).
                CREATE TABLE IF NOT EXISTS coverage_starts (
                    host TEXT NOT NULL, symbol TEXT NOT NULL, instrument_id TEXT NOT NULL,
                    session TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(host,symbol,instrument_id,session));
            ''')
            columns = {r[1] for r in db.execute("PRAGMA table_info(observations)")}
            for column in ("scope", "scope_digest"):
                if column not in columns:
                    # Migration: existing rows keep NULL, read as full history.
                    db.execute(f"ALTER TABLE observations ADD COLUMN {column} TEXT")

    def pin(self, host, metadata):
        identity = json.dumps([metadata.instrument_id, metadata.currency, metadata.exchange_code,
                               metadata.sub_category], sort_keys=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT identity FROM identities WHERE host=? AND symbol=?", (host,metadata.symbol)).fetchone()
            if old and old[0] != identity:
                raise BarDataError("SECURITY_IDENTITY_CHANGED")
            aliases = db.execute("SELECT symbol FROM identities WHERE host=? AND identity=? AND symbol<>?",
                                 (host,identity,metadata.symbol)).fetchall()
            if aliases:
                raise BarDataError("SECURITY_IDENTITY_ALIAS")
            db.execute("INSERT OR IGNORE INTO identities VALUES (?,?,?)", (host,metadata.symbol,identity))

    @staticmethod
    def _content_digest(basis):
        content = {k:v for k,v in basis.model_dump(mode="json").items() if k not in {"verified_at", "basis_session"}}
        return hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _changed(db, digest_sql, args, basis):
        old = db.execute(digest_sql, args).fetchone()
        if not old:
            return None, []
        before = {r[0]:r[1:] for r in json.loads(old[0])["daily_history"]}
        after = {r[0]:list(r[1:]) for r in basis.daily_history}
        return old[1], sorted(d for d in before.keys() & after.keys() if before[d] != after[d])

    def record(self, host, symbol, at, status, detail, basis=None):
        """Full-history evidence: the only writer of the ``current`` pointer."""
        digest = None
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if basis is not None:
                payload = json.dumps(basis.model_dump(mode="json"), sort_keys=True)
                digest = self._content_digest(basis)
                old_at, changed = self._changed(db, "SELECT s.payload,c.at FROM current c JOIN snapshots s "
                                                "ON s.digest=c.digest WHERE host=? AND symbol=?", (host,symbol), basis)
                if old_at and clock(old_at) > clock(at):
                    raise BarDataError("VENDOR_CLOCK_MOVED_BACKWARDS")
                if changed:
                    status, detail = "REVISED", "Daily OHLCV revised on " + ",".join(changed)
                db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?)", (digest,payload))
                db.execute("INSERT OR REPLACE INTO current VALUES (?,?,?,?)", (host,symbol,digest,clock(at).isoformat()))
            db.execute("INSERT INTO observations(host,symbol,at,status,detail,digest,scope) VALUES (?,?,?,?,?,?,?)",
                       (host,symbol,clock(at).isoformat(),status,detail,digest,FULL_HISTORY))
        return {"status":status, "detail":detail, "snapshot":digest}

    def record_scoped(self, host, symbol, scope, at, status, detail, basis=None):
        """Scoped evidence (discovery): its own pointer and observations only.

        A common session that differs from the full-history snapshot or from this
        scope's previous snapshot is recorded as ``REVISED`` (visible to
        ``latest_revision``); the full-history pointer is never written here.
        """
        digest, name = None, scope.policy
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if basis is not None:
                payload = json.dumps(basis.model_dump(mode="json"), sort_keys=True)
                digest = self._content_digest(basis)
                old_at, own = self._changed(db, "SELECT s.payload,c.at FROM scoped_current c JOIN snapshots s "
                    "ON s.digest=c.digest WHERE host=? AND symbol=? AND scope=?", (host,symbol,name), basis)
                if old_at and clock(old_at) > clock(at):
                    raise BarDataError("VENDOR_CLOCK_MOVED_BACKWARDS")
                _, full = self._changed(db, "SELECT s.payload,c.at FROM current c JOIN snapshots s "
                                        "ON s.digest=c.digest WHERE host=? AND symbol=?", (host,symbol), basis)
                changed = sorted(set(own) | set(full))
                if changed:
                    status, detail = "REVISED", "Daily OHLCV revised on " + ",".join(changed) + "; " + detail
                db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?)", (digest,payload))
                db.execute("INSERT OR REPLACE INTO scoped_current VALUES (?,?,?,?,?,?)",
                           (host,symbol,name,scope.scope_id,digest,clock(at).isoformat()))
            db.execute("INSERT INTO observations(host,symbol,at,status,detail,digest,scope,scope_digest) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (host,symbol,clock(at).isoformat(),status,detail,digest,name,scope.scope_id))
        return {"status":status, "detail":detail, "snapshot":digest}

    def record_defects(self, host, symbol, defects, *, instrument_id, scope, scope_digest, timeframe,
                       received_at, requested, required, at):
        """Bounded, sanitized defect records (no credentials, headers or request bodies)."""
        rows = []
        for d in list(defects)[:MAX_DEFECTS]:
            row_json = json.dumps(d.get("row"), sort_keys=True) if d.get("row") is not None else None
            evidence = hashlib.sha256(json.dumps([symbol, timeframe, d.get("time"), d.get("field"),
                                                  d.get("reason"), row_json], sort_keys=True).encode()).hexdigest()
            rows.append((host, symbol, instrument_id, scope, scope_digest, timeframe, received_at,
                         json.dumps(requested, sort_keys=True), json.dumps(required, sort_keys=True),
                         d.get("index"), d.get("time"), d.get("field"), d["reason"], d["status"], evidence,
                         row_json, clock(at).isoformat()))
        if rows:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.executemany("INSERT INTO history_defects(host,symbol,instrument_id,scope,scope_digest,timeframe,"
                               "received_at,requested,required,row_index,row_time,field,reason,status,"
                               "evidence_digest,row_json,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def defects(self, host, symbol, scope=None):
        query = "SELECT * FROM history_defects WHERE host=? AND symbol=?" + (" AND scope=?" if scope else "")
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(query + " ORDER BY sequence",
                                                (host, symbol, scope) if scope else (host, symbol))]

    def latest_revision(self, host, symbol):
        """Latest revision seen by ANY scope: a changed common session is a genuine change."""
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT at FROM observations WHERE host=? AND symbol=? AND status='REVISED' ORDER BY sequence DESC LIMIT 1",
                             (host,symbol)).fetchone()
        return clock(row[0]) if row else None

    def latest(self, host, symbol, scope=FULL_HISTORY):
        """Latest observation of one scope (legacy NULL rows are full history)."""
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT at,status,detail,digest,scope_digest FROM observations WHERE host=? AND symbol=? "
                             "AND COALESCE(scope,?)=? ORDER BY sequence DESC LIMIT 1",
                             (host,symbol,FULL_HISTORY,scope)).fetchone()
        return dict(zip(("at","status","detail","snapshot","scope_digest"), row)) if row else None

    def coverage(self, host, symbol, instrument_id):
        """What accepted evidence proves about this instrument's daily coverage (audit F1).

        ``earliest``: the first session of any accepted capture (full history or any
        scope; ``CONSISTENT`` or ``REVISED`` observations, append-only), so a later
        narrower reply cannot erase it across restarts, window rolls or failed refreshes.
        ``established``: where the listing's history starts, from supported origin or
        completeness evidence. The current integration has none (a reply with fewer rows
        than requested has the same shape whether the history is young or the reply was
        cut short), so it is always None. ``ignored_count_markers``: rows a ``1dea7d4``
        build wrote to ``coverage_starts`` from row counts; kept as audit history only.
        Only the same instrument ID counts.
        """
        with closing(sqlite3.connect(self.path)) as db:
            earliest = db.execute(
                "SELECT MIN(json_extract(s.payload,'$.coverage_start')) FROM observations o JOIN snapshots s "
                "ON s.digest=o.digest WHERE o.host=? AND o.symbol=? AND o.status IN ('CONSISTENT','REVISED') "
                "AND json_extract(s.payload,'$.security_id')=?", (host,symbol,instrument_id)).fetchone()[0]
            markers = db.execute("SELECT COUNT(*) FROM coverage_starts WHERE host=? AND symbol=? AND instrument_id=?",
                                 (host,symbol,instrument_id)).fetchone()[0]
        earliest = date.fromisoformat(earliest) if earliest else None
        return {"earliest": earliest, "established": None, "ignored_count_markers": markers}

    def current_snapshot(self, host, symbol, scope=FULL_HISTORY):
        with closing(sqlite3.connect(self.path)) as db:
            row = (db.execute("SELECT digest,at FROM current WHERE host=? AND symbol=?", (host,symbol)).fetchone()
                   if scope == FULL_HISTORY else
                   db.execute("SELECT digest,at FROM scoped_current WHERE host=? AND symbol=? AND scope=?",
                              (host,symbol,scope)).fetchone())
        return dict(zip(("snapshot","at"), row)) if row else None


# A frame that breaks the adapter contract (unparsable receipt time, missing
# provenance or request, not a DataFrame). BarDataError is handled first.
MALFORMED = (AttributeError, TypeError, ValueError, KeyError)


def _raw(frame, metadata, timeframe, now, host):
    if frame.attrs.get("webull_host") != host:
        raise BarDataError("WRONG_PROVIDER_HOST")
    validate(frame)
    if frame.attrs.get("provider_identity") != {"symbol":metadata.symbol, "instrument_id":metadata.instrument_id}:
        raise BarDataError("BAR_IDENTITY_MISMATCH")
    received = frame.attrs.get("received_at")
    if received is None or clock(received) > clock(now):
        raise BarDataError("BAR_RECEIPT_INVALID")
    if (clock(now)-clock(received)).total_seconds() > 60:
        raise BarDataError("BAR_RECEIPT_STALE")
    meta = frame.attrs.get("bar_provenance", {})
    if type(meta.get("delay_minutes")) is not int or meta["delay_minutes"] != 0:
        raise BarDataError("DELAYED_OR_UNKNOWN_BARS")
    request = frame.attrs.get("webull_request", {})
    if request.get("timespan") != timeframe or request.get("category") != metadata.bar_category:
        raise BarDataError("WRONG_BAR_CHANNEL")
    if timeframe == "M15" and (request.get("trading_sessions") != "RTH" or
                                frame.attrs.get("provider_sessions") != ["RTH"]):
        raise BarDataError("RAW_MINUTES_NOT_RTH")
    return clock(received)


def _daily(frame, metadata, now, host):
    received = _raw(frame, metadata, "D", now, host)
    local = frame.index.tz_convert(ET)
    if not (local == local.normalize()).all():
        raise BarDataError("DAILY_SESSION_LABEL_INVALID")
    cutoff = latest_closed_session(min(clock(now),received))
    out = frame[local.date <= cutoff].copy()
    if out.empty or len(out) > 1000:
        raise BarDataError("COMPLETED_DAILY_HISTORY_OUTSIDE_BOUNDS")
    dates = list(out.index.tz_convert(ET).date)
    if dates[-1] != latest_closed_session(now):
        raise BarDataError(f"MISSING_OR_STALE_DAILY_HISTORY: last session {dates[-1]}, "
                           f"required {latest_closed_session(now)}")
    if dates != sessions(dates[0],dates[-1]):
        # G5a checkpoint 3: name the sessions (diagnostic only; the rejection is unchanged).
        raise BarDataError("MISSING_OR_STALE_DAILY_HISTORY: missing " + _missing_text(dates))
    return out


def _missing_text(dates):
    missing = sorted(set(sessions(dates[0], dates[-1])) - set(dates))
    if not missing:
        return "non-session daily label"
    return ",".join(d.isoformat() for d in missing[:10]) + (f" (+{len(missing) - 10} more)" if len(missing) > 10 else "")


def _daily_scoped(frame, metadata, scope, now, host):
    """Required-window validation for a discovery scope (G5a checkpoint 3).

    Every unwaivable check of ``_raw`` runs first. The required sessions must be
    contiguous exchange sessions ending at the scope's cutoff. A gap is never filled
    and never called short history: a first required session missing while older rows
    exist is a gap, and a capped response that did not reach the start is truncated.
    A reply that starts late with no older row is NOT taken as a young listing by
    itself: ``VendorBasisSource._short_history`` decides it from accepted evidence.
    """
    received = _raw(frame, metadata, "D", now, host)
    local = frame.index.tz_convert(ET)
    if not (local == local.normalize()).all():
        raise BarDataError("DAILY_SESSION_LABEL_INVALID")
    report = frame.attrs.get("scope_report") or {}
    window = report.get("window") or {}
    if (window.get("policy"), window.get("required_start"), window.get("required_end")) != (
            scope.policy, scope.required_start.isoformat(), scope.required_end.isoformat()):
        raise BarDataError("SCOPE_RESPONSE_MISMATCH")
    if latest_closed_session(min(clock(now), received)) != scope.cutoff_session:
        raise BarDataError("SCOPE_CUTOFF_CHANGED")
    dates = list(local.date)
    if any(not scope.required_start <= d <= scope.required_end for d in dates):
        raise BarDataError("SCOPE_ROWS_OUTSIDE_REQUIRED_INTERVAL")
    if dates[-1] != scope.required_end:
        raise BarDataError(f"MISSING_OR_STALE_DAILY_HISTORY: last session {dates[-1]}, required {scope.required_end}")
    expected = sessions(dates[0], dates[-1])
    if dates != expected:
        raise BarDataError("MISSING_REQUIRED_SESSIONS: " + _missing_text(dates))
    if dates[0] > scope.required_start:
        if report.get("before", 0):
            gap = sessions(scope.required_start, dates[0])[:-1]
            raise BarDataError(f"MISSING_REQUIRED_SESSIONS: {gap[0].isoformat()}..{gap[-1].isoformat()} "
                               f"({len(gap)} sessions) while older rows exist")
        if report.get("rows_returned", 0) >= ScopeWindow.model_validate(window).count:
            raise BarDataError("SCOPED_HISTORY_TRUNCATED: the capped response did not reach the required start")
    return frame


def _basis(daily, raw_anchor, metadata, host, now, actions=None, *, method="webull-history-v1"):
    received = _raw(raw_anchor, metadata, "M15", now, host)
    day = daily.index[-1].tz_convert(ET).date()
    opened, closed = session(day)
    expected = pd.date_range(opened,closed-pd.Timedelta(minutes=15),freq="15min").tz_convert("UTC")
    if received < closed or not raw_anchor.index.equals(expected):
        raise BarDataError("INCOMPLETE_RAW_ANCHOR_SESSION")
    history = tuple((str(d.tz_convert(ET).date()), *(float(v) for v in row))
                    for d,row in daily[["open","high","low","close","volume"]].iterrows())
    adjustment = None
    if abs(history[-1][4]-float(raw_anchor.close.iloc[-1])) > 0.000001:
        if actions is None:
            raise BarDataError("DAILY_RAW_CLOSE_MISMATCH")
        try:
            adjustment = actions.anchor(metadata,day)
        except (BarDataError,sqlite3.Error,OSError,ValueError):
            raise BarDataError("DAILY_RAW_CLOSE_MISMATCH; ordinary action evidence unavailable") from None
        if abs(float(adjustment.expected_close(raw_anchor.close.iloc[-1]))-history[-1][4]) > 0.000001:
            raise BarDataError("DAILY_RAW_CLOSE_MISMATCH; action economics disagree")
    return VendorPriceBasis(source="Webull native history with raw close check", evidence_ref=REFERENCE,
        method=method, host=host, symbol=metadata.symbol, security_id=metadata.instrument_id,
        currency=metadata.currency, coverage_start=daily.index[0].tz_convert(ET).date(),
        basis_session=clock(now).date(), verified_at=now, normalization="split_dividend_adjusted",
        daily_history=history, anchor_session=day, daily_anchor_close=history[-1][4],
        raw_anchor_close=float(raw_anchor.close.iloc[-1]),anchor_adjustment=adjustment)


def _defect_text(code, defect):
    """``CODE: reason at session D (time, row i of n, field f)``; never raw provider text."""
    stamp = defect.get("time")
    where = f"session {pd.Timestamp(stamp).tz_convert(ET).date()} ({stamp}" if stamp else "(unreadable time"
    rows = f" of {defect['rows']}" if defect.get("rows") is not None else ""
    return f"{code}: {defect['reason']} at {where}, row {defect['index']}{rows}, field {defect['field']})"


class VendorBasisSource:
    def __init__(self, source, store, *, host, clock_fn=lambda:datetime.now(timezone.utc), fallback=None, actions=None):
        if host not in {"api.sandbox.webull.com", "api.webull.com"}:
            raise BarDataError("Unsupported vendor host")
        self.source, self.store, self.host, self._clock, self.fallback = source, store, host, clock_fn, fallback
        self.actions = actions
        self.last_volume_errors = {}
        self.last_errors = {}
        self.last_fetch_errors = {}
        self.last_scope_report = {}

    def __getattr__(self, name):
        return getattr(self.source,name)

    def security_metadata(self, symbols):
        lookup = getattr(self.source,"security_metadata_partial",self.source.security_metadata)
        return lookup(symbols)

    def _fetch(self, symbols, **kwargs):
        self.last_fetch_errors = {}
        if not symbols:
            return {}
        try:
            fetch = getattr(self.source,"bars_partial",self.source.bars)
            out = fetch(symbols, **kwargs)
        except BarDataError:
            # Do not retry a failed provider batch: quota/auth failures are shared.
            return {}
        errors = getattr(self.source, "last_partial_errors", None)
        self.last_fetch_errors = dict(errors) if isinstance(errors, dict) else {}
        return out

    def _usable(self, metadata, category):
        usable = []
        for symbol, meta in metadata.items():
            try:
                if meta.bar_category != category:
                    raise BarDataError("WRONG_SECURITY_CATEGORY")
                age = (clock(self._clock())-clock(meta.observed_at)).total_seconds()
                if not 0 <= age <= 300:
                    raise BarDataError("SECURITY_METADATA_STALE")
                self.store.pin(self.host,meta)
                usable.append(symbol)
            except BarDataError as exc:
                self.last_errors[symbol] = str(exc)
        return usable

    def _not_returned(self, symbol, errors, timeframe="DAILY"):
        """Why the provider gave no usable frame: the offending row when attributable."""
        error = errors.get(symbol)
        if not isinstance(error, dict):
            return f"{timeframe}_PROVIDER_UNAVAILABLE", None
        defect = error.get("defect")
        reason = str(error.get("reason") or "")
        if isinstance(defect, dict) and defect.get("reason"):
            code = reason.split(":")[0] if reason.startswith("SCOPED_") else f"{timeframe}_ROW_INVALID"
            return _defect_text(code, defect), defect
        return f"{timeframe}_REPLY_INVALID: {reason}" if reason else f"{timeframe}_PROVIDER_UNAVAILABLE", None

    def _anchors(self, clean, category):
        groups = {}
        for symbol, frame in clean.items():
            groups.setdefault(frame.index[-1].tz_convert(ET).date(), []).append(symbol)
        anchors = {}
        for day, names in groups.items():
            opened,closed = session(day)
            anchors.update(self._fetch(names,category=category,timespan="M15",count=40,sessions="RTH",
                                      start_time=int(opened.timestamp()*1000),end_time=int(closed.timestamp()*1000)-1))
        return anchors

    def _attach_volume(self, frame, symbol, meta, timespan, now):
        revision_at = self.store.latest_revision(self.host,symbol)
        # Do not turn native volume labels into an assertion about adjusted share units.
        frame.attrs["volume_basis"] = {"source":"Webull OpenAPI", "evidence_ref":REFERENCE,
            "channel":"native:"+timespan,"definition_id":None,"units":"shares","share_basis_id":None}
        if self.actions is not None:
            try:
                frame.attrs["volume_basis"] = self.actions.volume(meta,timespan,after=revision_at).model_dump(mode="json")
                self.last_volume_errors.pop(symbol,None)
            except (BarDataError,sqlite3.Error,OSError,ValueError) as exc:
                self.last_volume_errors[symbol] = str(exc) if isinstance(exc,BarDataError) else "Automatic action evidence store unavailable"
        if self.fallback is not None and self.actions is None:
            # Optional accepted volume evidence; it is never required for price-only setups.
            from desk.action_source import native_volume_basis
            try:
                channel = self.fallback.channels[(symbol,timespan)]
                reviewed = self.fallback.ledger.basis(symbol,now,normalization=channel.normalization)
                if (reviewed.security_id == meta.instrument_id and channel.volume_policy
                        and (revision_at is None or clock(reviewed.verified_at) > revision_at)):
                    frame.attrs["volume_basis"] = native_volume_basis(channel,reviewed).model_dump(mode="json")
            except (BarDataError,KeyError,sqlite3.Error):
                pass

    @staticmethod
    def _provenance(basis, timespan):
        normalized = "split_dividend_adjusted" if timespan == "D" else "unadjusted"
        return BarProvenance(source=basis.source,evidence_ref=REFERENCE,
            timeframe=timespan,timestamp_semantics="session_label" if timespan == "D" else "start",
            session="regular",delay_minutes=0,adjustment=normalized,price_scale_id=basis.method,
            price_basis=basis.model_copy(update={"normalization":normalized})).model_dump(mode="json")

    def bars(self, symbols, *, timespan, category, count=1000, **kwargs):
        if timespan not in {"D","M15"} or not 1 <= count <= 1000:
            raise BarDataError("Vendor basis supports D/M15, at most 1000 rows")
        if timespan == "D" and kwargs:
            raise BarDataError("Bounded daily requests need a separate historical review")
        self.last_errors = {}
        metadata = securities(self, symbols, self.last_errors)
        usable = self._usable(metadata, category)
        # Exclude today's forming daily bar at the provider, so it cannot displace
        # the oldest of the 1000 rows retained by yesterday's armed candidate.
        daily_end = session(latest_closed_session(self._clock()))[1]
        daily = self._fetch(usable, category=category, timespan="D", count=1000,
                            end_time=int(daily_end.timestamp()*1000)-1)
        daily_errors = self.last_fetch_errors
        clean = {}
        for symbol in usable:
            try:
                if symbol not in daily:
                    reason, defect = self._not_returned(symbol, daily_errors)
                    if defect is not None:
                        # Full history requires every returned row (G5a checkpoint 3 diagnostics).
                        self.store.record_defects(self.host, symbol, [{**defect, "status": "REJECTED_REQUIRED_DATA"}],
                            instrument_id=metadata[symbol].instrument_id, scope=FULL_HISTORY, scope_digest=None,
                            timeframe="D", received_at=None, requested={"count": 1000, "end_time": daily_end.isoformat()},
                            required={"rows": "every returned row"}, at=self._clock())
                    raise BarDataError(reason)
                clean[symbol] = _daily(daily[symbol],metadata[symbol],self._clock(),self.host)
            except BarDataError as exc:
                self.last_errors[symbol] = str(exc)
            except MALFORMED as exc:
                # An adapter-contract violation for one ticker stays with that ticker.
                self.last_errors[symbol] = "MALFORMED_FRAME (" + type(exc).__name__ + ")"
        anchors = self._anchors(clean, category)
        wanted = daily if timespan == "D" else self._fetch(
            [s for s in usable if s not in self.last_errors],category=category,timespan=timespan,count=count,**kwargs)
        out = {}
        for symbol in usable:
            try:
                if symbol in self.last_errors:
                    raise BarDataError(self.last_errors[symbol])
                if symbol not in anchors or symbol not in wanted:
                    raise BarDataError("MINUTE_PROVIDER_UNAVAILABLE")
                now = self._clock()
                basis = _basis(clean[symbol], anchors[symbol], metadata[symbol], self.host, now, self.actions)
                frame = (clean[symbol].tail(count) if timespan == "D" else wanted[symbol]).copy()
                _raw(frame,metadata[symbol],timespan,now,self.host)
                frame.attrs["bar_provenance"] = self._provenance(basis, timespan)
                self.store.record(self.host,symbol,now,"CONSISTENT","Vendor price consistency; action completeness unknown",basis)
                self._attach_volume(frame, symbol, metadata[symbol], timespan, now)
                out[symbol] = frame
            except BarDataError as exc:
                reason = str(exc)
                if self.fallback is not None and reason == "DAILY_RAW_CLOSE_MISMATCH":
                    # A ledger can identify relevant actions, but does not prove an
                    # unexplained price ratio. Do not bypass the failed comparison.
                    try:
                        reviewed = self.fallback.ledger.basis(symbol,self._clock(),
                            normalization="split_dividend_adjusted")
                        if reviewed.security_id == metadata[symbol].instrument_id:
                            reason += "; reviewed action evidence available for reconciliation"
                    except (BarDataError,KeyError,sqlite3.Error):
                        pass
                self.last_errors[symbol] = reason + "; source evidence/rebuild required"
            except MALFORMED as exc:
                self.last_errors[symbol] = ("MALFORMED_FRAME (" + type(exc).__name__ + ")"
                                            "; source evidence/rebuild required")
        for symbol, reason in self.last_errors.items():
            self.store.record(self.host,symbol,self._clock(),"UNAVAILABLE",reason)
        return out

    def _short_history(self, symbol, scope, first):
        """A reply starting after the required start is a source failure (audit F1 and
        the coverage-origin repair). Earlier accepted evidence for the same instrument
        proves the missing sessions exist (``SCOPED_HISTORY_INCOMPLETE``). Otherwise the
        listing's origin would need supported origin or completeness evidence, which the
        current integration does not have; a reply's row count is not such evidence, so
        coverage is unverified. No listing date is inferred."""
        try:
            known = self.store.coverage(self.host, symbol, scope.instrument_id)
        except sqlite3.Error:
            raise BarDataError("SCOPED_COVERAGE_UNVERIFIED: coverage evidence unavailable") from None
        if known["earliest"] is not None and known["earliest"] < first:
            missing = sessions(scope.required_start, first)[:-1] if known["earliest"] <= scope.required_start else \
                sessions(known["earliest"], first)[:-1]
            raise BarDataError(f"SCOPED_HISTORY_INCOMPLETE: accepted evidence for this instrument starts "
                               f"{known['earliest']}; the reply starts {first} (known sessions missing: {len(missing)})")
        if known["established"] is None or known["established"] != first:
            raise BarDataError(f"SCOPED_COVERAGE_UNVERIFIED: the reply starts {first}, after the required start "
                               f"{scope.required_start}, and no supported evidence shows the listing starts there")

    def discovery_bars(self, symbols, *, category, liquidity_through=None):
        """Daily bars for the weekly leader build only, on an explicit discovery scope.

        G5a checkpoint 3. One bounded daily request per batch; rows outside the
        required sessions are classified before parsing and recorded, never used.
        The raw M15 anchor check is unchanged. Price evidence is ``webull-discovery-v1``
        under its own store pointer: it cannot replace, clear or revalidate
        full-history evidence, arm a signal or feed setup evaluation.
        """
        scoped = getattr(self.source, "bars_scoped", None)
        if scoped is None:
            raise ScopeUnsupported("DISCOVERY_SCOPE_UNSUPPORTED: the price adapter cannot bound history")
        self.last_errors, self.last_scope_report = {}, {}
        started = self._clock()
        metadata = securities(self, symbols, self.last_errors)
        usable = self._usable(metadata, category)
        scopes = {}
        for symbol in list(usable):
            try:
                scopes[symbol] = discovery_scope(symbol, metadata[symbol].instrument_id, started,
                                                 liquidity_through=liquidity_through)
            except (BarDataError, ValueError) as exc:
                usable.remove(symbol)
                self.last_errors[symbol] = "SCOPE_INVALID: " + str(exc).splitlines()[0]
        window = ScopeWindow.for_scope(scopes[usable[0]]) if usable else None
        daily, errors = {}, {}
        if usable:
            try:
                daily = scoped(usable, category=category, window=window)
                found = getattr(self.source, "last_partial_errors", None)
                errors = dict(found) if isinstance(found, dict) else {}
            except BarDataError as exc:
                # Shared: never retried ticker by ticker (quota/auth failures are shared).
                for symbol in usable:
                    self.last_errors[symbol] = (f"DISCOVERY_BATCH_UNAVAILABLE ({type(exc).__name__}, "
                                                f"shared by {len(usable)} tickers)")
                usable = []
        clean = {}
        for symbol in usable:
            scope = scopes[symbol]
            report = (daily[symbol].attrs.get("scope_report") if symbol in daily
                      else (errors.get(symbol) or {}).get("scope_report")) or {}
            self.last_scope_report[symbol] = {k: report.get(k) for k in
                ("rows_returned", "before", "after", "excluded_defect_count", "earliest")}
            excluded = report.get("excluded_defects") or []
            received = daily[symbol].attrs.get("received_at") if symbol in daily else None
            common = dict(instrument_id=scope.instrument_id, scope=scope.policy, scope_digest=scope.scope_id,
                          timeframe="D", received_at=received, requested=window.request(),
                          required={"start": scope.required_start.isoformat(), "end": scope.required_end.isoformat(),
                                    "sessions": len(scope.required_sessions())}, at=self._clock())
            self.store.record_defects(self.host, symbol, excluded, **common)
            try:
                if symbol not in daily:
                    reason, defect = self._not_returned(symbol, errors)
                    if defect is not None:
                        self.store.record_defects(self.host, symbol, [{**defect, "status": "REJECTED_REQUIRED_DATA"}],
                                                  **common)
                    raise BarDataError(reason)
                clean[symbol] = _daily_scoped(daily[symbol], metadata[symbol], scope, self._clock(), self.host)
                first = clean[symbol].index[0].tz_convert(ET).date()
                if first > scope.required_start:
                    self._short_history(symbol, scope, first)
            except BarDataError as exc:
                self.last_errors[symbol] = str(exc)
            except MALFORMED as exc:
                self.last_errors[symbol] = "MALFORMED_FRAME (" + type(exc).__name__ + ")"
        anchors = self._anchors(clean, category)
        out = {}
        for symbol in usable:
            scope = scopes[symbol]
            try:
                if symbol in self.last_errors:
                    raise BarDataError(self.last_errors[symbol])
                if symbol not in anchors:
                    raise BarDataError("MINUTE_PROVIDER_UNAVAILABLE")
                now = self._clock()
                basis = _basis(clean[symbol], anchors[symbol], metadata[symbol], self.host, now, self.actions,
                               method="webull-discovery-v1")
                frame = clean[symbol].copy()
                frame.attrs["bar_provenance"] = self._provenance(basis, "D")
                frame.attrs["history_scope"] = {**scope.model_dump(mode="json"), "scope_id": scope.scope_id}
                frame.attrs["scope_report"] = self.last_scope_report[symbol]
                detail = json.dumps({"scope": frame.attrs["history_scope"], "requested": window.request(),
                                     "rows": len(frame), **self.last_scope_report[symbol]}, sort_keys=True)
                self.store.record_scoped(self.host, symbol, scope, now, "CONSISTENT", detail, basis)
                self._attach_volume(frame, symbol, metadata[symbol], "D", now)
                out[symbol] = frame
            except BarDataError as exc:
                self.last_errors[symbol] = str(exc)
            except MALFORMED as exc:
                self.last_errors[symbol] = "MALFORMED_FRAME (" + type(exc).__name__ + ")"
        for symbol, reason in self.last_errors.items():
            if symbol in scopes:
                self.store.record_scoped(self.host, symbol, scopes[symbol], self._clock(), "UNAVAILABLE", reason)
        return out
