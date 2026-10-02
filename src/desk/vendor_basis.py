"""Opt-in Webull price consistency, without a claimed complete action feed.

Native prices are never adjusted here. A mismatched ticker needs independent
source evidence; a new adjusted history never repairs an already-armed signal.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pandas as pd

from desk.bar_contract import BarProvenance
from desk.bars import BarDataError, validate
from desk.calendar import ET, clock, latest_closed_session, session, sessions
from desk.data_basis import VendorPriceBasis
from desk.security import securities

REFERENCE = "https://developer.webull.com/apis/docs/reference/historical-bars/"


class VendorHistoryStore:
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
            ''')

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

    def record(self, host, symbol, at, status, detail, basis=None):
        digest = None
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if basis is not None:
                payload = json.dumps(basis.model_dump(mode="json"), sort_keys=True)
                content = {k:v for k,v in basis.model_dump(mode="json").items()
                           if k not in {"verified_at", "basis_session"}}
                digest = hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()
                old = db.execute("SELECT s.payload,c.at FROM current c JOIN snapshots s ON s.digest=c.digest WHERE host=? AND symbol=?",
                                 (host,symbol)).fetchone()
                if old:
                    if clock(old[1]) > clock(at):
                        raise BarDataError("VENDOR_CLOCK_MOVED_BACKWARDS")
                    before = {r[0]:r[1:] for r in json.loads(old[0])["daily_history"]}
                    after = {r[0]:list(r[1:]) for r in basis.daily_history}
                    changed = sorted(d for d in before.keys() & after.keys() if before[d] != after[d])
                    if changed:
                        status, detail = "REVISED", "Daily OHLCV revised on " + ",".join(changed)
                db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?)", (digest,payload))
                db.execute("INSERT OR REPLACE INTO current VALUES (?,?,?,?)", (host,symbol,digest,clock(at).isoformat()))
            db.execute("INSERT INTO observations(host,symbol,at,status,detail,digest) VALUES (?,?,?,?,?,?)",
                       (host,symbol,clock(at).isoformat(),status,detail,digest))
        return {"status":status, "detail":detail, "snapshot":digest}

    def latest_revision(self, host, symbol):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT at FROM observations WHERE host=? AND symbol=? AND status='REVISED' ORDER BY sequence DESC LIMIT 1",
                             (host,symbol)).fetchone()
        return clock(row[0]) if row else None

    def latest(self, host, symbol):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT at,status,detail,digest FROM observations WHERE host=? AND symbol=? ORDER BY sequence DESC LIMIT 1",
                             (host,symbol)).fetchone()
        return dict(zip(("at","status","detail","snapshot"), row)) if row else None


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
    if dates[-1] != latest_closed_session(now) or dates != sessions(dates[0],dates[-1]):
        raise BarDataError("MISSING_OR_STALE_DAILY_HISTORY")
    return out


def _basis(daily, raw_anchor, metadata, host, now, actions=None):
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
        method="webull-history-v1", host=host, symbol=metadata.symbol, security_id=metadata.instrument_id,
        currency=metadata.currency, coverage_start=daily.index[0].tz_convert(ET).date(),
        basis_session=clock(now).date(), verified_at=now, normalization="split_dividend_adjusted",
        daily_history=history, anchor_session=day, daily_anchor_close=history[-1][4],
        raw_anchor_close=float(raw_anchor.close.iloc[-1]),anchor_adjustment=adjustment)


class VendorBasisSource:
    def __init__(self, source, store, *, host, clock_fn=lambda:datetime.now(timezone.utc), fallback=None, actions=None):
        if host not in {"api.sandbox.webull.com", "api.webull.com"}:
            raise BarDataError("Unsupported vendor host")
        self.source, self.store, self.host, self._clock, self.fallback = source, store, host, clock_fn, fallback
        self.actions = actions
        self.last_volume_errors = {}
        self.last_errors = {}

    def __getattr__(self, name):
        return getattr(self.source,name)

    def security_metadata(self, symbols):
        lookup = getattr(self.source,"security_metadata_partial",self.source.security_metadata)
        return lookup(symbols)

    def _fetch(self, symbols, **kwargs):
        if not symbols:
            return {}
        try:
            fetch = getattr(self.source,"bars_partial",self.source.bars)
            return fetch(symbols, **kwargs)
        except BarDataError:
            # Do not retry a failed provider batch: quota/auth failures are shared.
            return {}

    def bars(self, symbols, *, timespan, category, count=1000, **kwargs):
        if timespan not in {"D","M15"} or not 1 <= count <= 1000:
            raise BarDataError("Vendor basis supports D/M15, at most 1000 rows")
        if timespan == "D" and kwargs:
            raise BarDataError("Bounded daily requests need a separate historical review")
        self.last_errors = {}
        metadata = securities(self, symbols, self.last_errors)
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
        # Exclude today's forming daily bar at the provider, so it cannot displace
        # the oldest of the 1000 rows retained by yesterday's armed candidate.
        daily_end = session(latest_closed_session(self._clock()))[1]
        daily = self._fetch(usable, category=category, timespan="D", count=1000,
                            end_time=int(daily_end.timestamp()*1000)-1)
        clean, groups = {}, {}
        for symbol in usable:
            try:
                if symbol not in daily:
                    raise BarDataError("DAILY_PROVIDER_UNAVAILABLE")
                clean[symbol] = _daily(daily[symbol],metadata[symbol],self._clock(),self.host)
                day = clean[symbol].index[-1].tz_convert(ET).date()
                groups.setdefault(day,[]).append(symbol)
            except BarDataError as exc:
                self.last_errors[symbol] = str(exc)
        anchors = {}
        for day, names in groups.items():
            opened,closed = session(day)
            anchors.update(self._fetch(names,category=category,timespan="M15",count=40,sessions="RTH",
                                      start_time=int(opened.timestamp()*1000),end_time=int(closed.timestamp()*1000)-1))
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
                normalized = "split_dividend_adjusted" if timespan == "D" else "unadjusted"
                frame.attrs["bar_provenance"] = BarProvenance(source=basis.source,evidence_ref=REFERENCE,
                    timeframe=timespan,timestamp_semantics="session_label" if timespan == "D" else "start",
                    session="regular",delay_minutes=0,adjustment=normalized,price_scale_id="webull-history-v1",
                    price_basis=basis.model_copy(update={"normalization":normalized})).model_dump(mode="json")
                self.store.record(self.host,symbol,now,"CONSISTENT","Vendor price consistency; action completeness unknown",basis)
                revision_at = self.store.latest_revision(self.host,symbol)
                # Do not turn native volume labels into an assertion about adjusted share units.
                frame.attrs["volume_basis"] = {"source":"Webull OpenAPI", "evidence_ref":REFERENCE,
                    "channel":"native:"+timespan,"definition_id":None,"units":"shares","share_basis_id":None}
                if self.actions is not None:
                    try:
                        frame.attrs["volume_basis"] = self.actions.volume(metadata[symbol],timespan,after=revision_at).model_dump(mode="json")
                        self.last_volume_errors.pop(symbol,None)
                    except (BarDataError,sqlite3.Error,OSError,ValueError) as exc:
                        self.last_volume_errors[symbol] = str(exc) if isinstance(exc,BarDataError) else "Automatic action evidence store unavailable"
                if self.fallback is not None and self.actions is None:
                    # Optional accepted volume evidence; it is never required for price-only setups.
                    from desk.action_source import native_volume_basis
                    try:
                        channel = self.fallback.channels[(symbol,timespan)]
                        reviewed = self.fallback.ledger.basis(symbol,now,normalization=channel.normalization)
                        if (reviewed.security_id == metadata[symbol].instrument_id and channel.volume_policy
                                and (revision_at is None or clock(reviewed.verified_at) > revision_at)):
                            frame.attrs["volume_basis"] = native_volume_basis(channel,reviewed).model_dump(mode="json")
                    except (BarDataError,KeyError,sqlite3.Error):
                        pass
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
        for symbol, reason in self.last_errors.items():
            self.store.record(self.host,symbol,self._clock(),"UNAVAILABLE",reason)
        return out
