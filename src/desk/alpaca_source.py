"""Opt-in Alpaca SIP decision volume for the scanner (G5a checkpoint 2).

Configuration (all three required; presence alone verifies nothing):
``DESK_ALPACA_VOLUME_CACHE`` (SQLite path), ``DESK_ALPACA_VOLUME_BUDGET`` (HTTP requests
per run, shared by the asset list and every bar page) and the ``APCA_*`` keys. Unset
cache path: no provider, the unchanged Webull volume path applies. A set path with a
missing key or bad budget: every volume-dependent check is UNAVAILABLE for that run,
with no Webull, IEX or native fallback; prices and price-only setups are unaffected.

The provider attaches a separate ``AlpacaDecisionVolume`` to each Webull frame. It
never edits OHLCV, the Webull ``volume`` column or ``volume_basis``. Saved signals carry
their used volume evidence; ``gate`` re-checks it before trigger observation (cache
only), revalidation (fresh refetch of the same keys) and the ticket's final fence
(cache only, local SQLite).

Plan B: unavailable volume blocks only the dependent checks (VCP dry volume, cup
handle volume, EP early volume, discovery liquidity); Massive (G3a) is the other path.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pandas as pd
from pydantic import ValidationError

from desk.alpaca_assets import AssetError, IdentityRecord, IdentityStore
from desk.alpaca_volume import (NETWORK_FORBIDDEN, AlpacaVolumeClient, AlpacaVolumeError, BarRequest,
                                RequestBudget, VolumeCache, prior_sessions)
from desk.bars import BarDataError
from desk.calendar import ET, clock, latest_closed_session, previous_trading_day, session, sessions
from desk.data_basis import (ALPACA_CHANNEL_POLICY, AlpacaDecisionVolume, attached_decision)
from desk.playbook.cards import CARDS
from desk.security import SecurityMetadata

ENV_CACHE, ENV_BUDGET = "DESK_ALPACA_VOLUME_CACHE", "DESK_ALPACA_VOLUME_BUDGET"
CONFIG_UNAVAILABLE = "ALPACA_VOLUME_CONFIG_UNAVAILABLE"
NOT_CONFIGURED = "ALPACA_VOLUME_NOT_CONFIGURED"
# Asset-list outcomes that sent no request: nothing new was observed about the source
# (a provider stop is already persisted in the cache), so no health event is written.
NO_REQUEST_SENT = ("REQUEST_BUDGET_EXHAUSTED", "RUN_STOPPED_AFTER_", NETWORK_FORBIDDEN)
# Same busy timeout as the account store's final lock; a longer wait refuses the ticket.
GUARD_TIMEOUT_SECONDS = 5

# Consumer registry: every decision that reads volume, its window and its card rule.
# The daily request covers the largest window; tests hold each consumer to it.
DAILY_SESSIONS = 50
CONSUMERS = {
    "discovery:liquidity": {"sessions": 50, "channel": "native-daily"},
    "2_minervini_vcp:dry_volume": {"sessions": 50, "channel": "native-daily", "setup": "2_minervini_vcp"},
    "3_oneil_cup_with_handle:handle_volume": {"sessions": 50, "channel": "native-daily",
                                              "setup": "3_oneil_cup_with_handle"},
    "5_qullamaggie_episodic_pivot:early_volume": {"sessions": 50, "channel": "native-daily+rth-m15",
                                                  "setup": "5_qullamaggie_episodic_pivot"},
    "features:rel_volume": {"sessions": 50, "channel": "native-daily", "decision": False},
}
VOLUME_SETUPS = frozenset(c["setup"] for c in CONSUMERS.values() if "setup" in c)
# Volume conditions written on cards that no entry path implements yet (Step 10/17).
# Text on a card is not a check: these are reported, never treated as satisfied.
NOT_IMPLEMENTED = {
    "2_minervini_vcp:rising_entry_volume": "card text 'buy through the last pivot on rising volume'; no entry check",
    "3_oneil_cup_with_handle:breakout_volume": "card param 1.4x average on the breakout; no entry check",
    "4_darvas_box:breakout_volume": "card param 1.5x the 50-day on the breakout day; no entry check",
}


def complete_through(now) -> date:
    """Latest session whose native-daily SIP aggregate is final at ``now``.

    Assumption (checkpoint 1): the extended-hours daily bar is final at the ET
    midnight after its session. At 16:10 ET that is the previous session.
    """
    day = latest_closed_session(now)
    if clock(now) < pd.Timestamp(day + timedelta(days=1)).tz_localize(ET):
        day = previous_trading_day(day)
    return day


def daily_request(symbols, through: date, n: int = DAILY_SESSIONS) -> BarRequest:
    first = sessions(through - timedelta(days=n * 2 + 10), through)[-n]
    return BarRequest(symbols=tuple(symbols), timeframe="1Day", adjustment="split",
                      start=pd.Timestamp(first).tz_localize(ET).to_pydatetime(),
                      end=(pd.Timestamp(through + timedelta(days=1)).tz_localize(ET)
                           - timedelta(seconds=1)).to_pydatetime())


def rth30_request(symbols, entry: date) -> BarRequest:
    opened = session(entry)[0]
    return BarRequest(symbols=tuple(symbols), timeframe="15Min", adjustment="split", start=opened.to_pydatetime(),
                      end=(opened + timedelta(minutes=30) - timedelta(seconds=1)).to_pydatetime())


def _request_fields(req: BarRequest) -> dict:
    return {"timeframe": req.timeframe, "start": req.start.isoformat(), "end": req.end.isoformat(),
            "adjustment": req.adjustment}


def _unavailable(symbol: str, channel: str, reason: str) -> AlpacaDecisionVolume:
    return AlpacaDecisionVolume(policy=ALPACA_CHANNEL_POLICY[channel], status="UNAVAILABLE", reason=reason,
                                desk_symbol=symbol, channel=channel)


class AlpacaVolumeProvider:
    """One run's provider: one budget, one asset list, one stop state."""

    def __init__(self, client: AlpacaVolumeClient | None, store: IdentityStore | None, *, issue: str | None = None):
        if issue is None and (client is None or store is None or client.cache is None):
            raise BarDataError("Alpaca volume provider needs a client with a cache and an identity store")
        self.client, self.store, self.issue = client, store, issue
        self._assets = None
        self._attempt: int | None = None          # the registered fetch attempt behind _assets
        self._open: int | None = None             # that fetch, until its first resolution commits
        self._asset_error: str | None = None
        # An identity outcome this run could not persist (re-audit R1): the cache-only
        # gate withholds eligibility for the rest of the run. Restart relies on the
        # durable open attempt instead.
        self._unrecorded: str | None = None
        self.batches: list[dict] = []     # sanitized request reports for scan records

    @classmethod
    def unavailable(cls, issue: str) -> "AlpacaVolumeProvider":
        return cls(None, None, issue=issue)

    @property
    def requests_used(self) -> int:
        return self.client._budget.used if self.client else 0

    # ---- identity -------------------------------------------------------------
    def identities(self, frames: dict[str, pd.DataFrame], decision_session: date) -> dict:
        if self.issue:
            return {s: self.issue for s in frames}
        meta = {}
        for symbol, frame in frames.items():
            try:
                meta[symbol] = SecurityMetadata.model_validate(frame.attrs.get("security_metadata"))
            except (ValidationError, TypeError):
                meta[symbol] = None
        if self._assets is None and self._asset_error is None:
            self._fetch_assets(sorted(frames))
            self.batches.append({"kind": "assets", "receipt": getattr(self.client, "asset_receipt", None),
                                 "error": self._asset_error,
                                 "assets": len(self._assets.assets) if self._assets else 0})
        if self._asset_error:
            return {s: "IDENTITY_UNAVAILABLE:" + self._asset_error for s in frames}
        # The fetch attempt covers the list's first resolution; every later resolution
        # of the same list registers its own attempt first (re-audit R1).
        attempt, self._open = self._open, None
        try:
            if attempt is None:
                attempt = self.store.register_attempt(sorted(frames), self.client._now(),
                                                      list_attempt=self._attempt)
            return self.store.resolve(meta, self._assets, decision_session, attempt, list_attempt=self._attempt)
        except (sqlite3.Error, ValidationError, AlpacaVolumeError):
            # The outcome is not recorded: a registered attempt stays open (durable) and
            # this run withholds eligibility (in-process); the FAILED rows are best effort.
            self._unrecorded = "IDENTITY_STORE_UNAVAILABLE"
            out = {s: "IDENTITY_STORE_UNAVAILABLE" for s in frames}
            try:
                self.store.record_failures(out, self.client._now(), attempt=attempt)
            except (sqlite3.Error, AlpacaVolumeError):
                pass
            return out

    def _fetch_assets(self, symbols) -> None:
        """Register the attempt, send the request, record the outcome (re-audit R1).

        No lock is held over the request. Requests that would not be sent (final guard,
        stopped run, spent budget) register nothing. If the attempt cannot be committed,
        nothing is sent; if its outcome cannot be committed, the attempt stays open.
        """
        client = self.client
        if client.network_blocked:
            self._asset_error = NETWORK_FORBIDDEN
            return
        if client.stopped:
            self._asset_error = "RUN_STOPPED_AFTER_" + client.stopped
            return
        if client._budget.used >= client._budget.limit:
            self._asset_error = "REQUEST_BUDGET_EXHAUSTED"
            return
        try:
            self._attempt = self._open = self.store.register_attempt(symbols, client._now())
        except (sqlite3.Error, AlpacaVolumeError):
            self._asset_error = self._unrecorded = "IDENTITY_STORE_UNAVAILABLE"
            return
        try:
            self._assets = client.fetch_assets()
        except AssetError as exc:
            self._asset_error = exc.code
            try:
                if exc.code.startswith(NO_REQUEST_SENT):
                    self.store.close_attempt(self._attempt, "NOT_SENT:" + exc.code, client._now())
                else:
                    # Persisted with the attempt's closure, so a later process cannot
                    # read an old pin as current (audit F1).
                    self.store.record_source_failure(exc.code, client._now(), attempt=self._attempt)
            except (sqlite3.Error, AlpacaVolumeError):
                self._asset_error = self._unrecorded = "IDENTITY_STORE_UNAVAILABLE"

    @contextmanager
    def held(self):
        """Hold the volume/identity file's writer reservation (audit F2).

        ``BEGIN IMMEDIATE`` takes SQLite's single writer slot in rollback-journal and WAL
        modes, so no STOP, FAILED, revision, mapping or identity-health write from any
        connection or process can commit until release; reads made meanwhile see the
        last committed state. No request may be sent while it is held: the client
        refuses with ``NETWORK_FORBIDDEN_IN_FINAL_GUARD``. Callers take it after the
        signal-store lock and before the account lock, and before the final clock.
        """
        if self.issue:
            yield
            return
        paths = sorted({str(Path(self.client.cache.path).resolve()), str(Path(self.store.path).resolve())})
        held = []
        try:
            for path in paths:
                db = sqlite3.connect(path, timeout=GUARD_TIMEOUT_SECONDS, isolation_level=None)
                held.append(db)
                db.execute("BEGIN IMMEDIATE")
            self.client.network_blocked = True
            yield
        finally:
            self.client.network_blocked = False
            for db in reversed(held):
                if db.in_transaction:
                    db.execute("ROLLBACK")
                db.close()

    # ---- attach ---------------------------------------------------------------
    def _batch(self, frames, channel: str, req_for, identities, *, through=None):
        resolved = {s: r for s, r in identities.items() if isinstance(r, IdentityRecord)}
        for symbol, reason in identities.items():
            if not isinstance(reason, IdentityRecord):
                frames[symbol].attrs["decision_volume"] = _unavailable(symbol, channel, reason)
        if not resolved:
            return
        by_alpaca = {r.alpaca_symbol: (s, r) for s, r in resolved.items()}
        req = req_for(sorted(by_alpaca))
        result = self.client.fetch(req, identities={a: r for a, (_, r) in by_alpaca.items()})
        report = result.report()
        report.pop("observations", None)
        self.batches.append({"kind": channel, **report})
        for alpaca_symbol, (symbol, record) in by_alpaca.items():
            obs = result.observations.get(alpaca_symbol)
            if obs is None:
                frames[symbol].attrs["decision_volume"] = _unavailable(
                    symbol, channel, result.failures.get(alpaca_symbol) or result.error or "SYMBOL_MISSING")
                continue
            # Final at receipt, not at the request: an unfinished last session stays unusable.
            final = min(through, complete_through(obs.received_at)) if channel == "native-daily" else None
            frames[symbol].attrs["decision_volume"] = AlpacaDecisionVolume(
                policy=ALPACA_CHANNEL_POLICY[channel], status="AVAILABLE", desk_symbol=symbol, channel=channel,
                complete_through=final, identity=record, request=_request_fields(req), observation=obs)

    def attach_daily(self, frames: dict[str, pd.DataFrame], now, *, through: date) -> None:
        """Attach the ``DAILY_SESSIONS`` native-daily volumes ending at ``through``.

        ``through`` later than the latest final session: UNAVAILABLE without a request.
        """
        if not frames:
            return
        if through > complete_through(now):
            for symbol, frame in frames.items():
                frame.attrs["decision_volume"] = _unavailable(symbol, "native-daily", "DAILY_NOT_COMPLETED_AT_RECEIPT")
            return
        identities = self.identities(frames, latest_closed_session(now))
        self._batch(frames, "native-daily", lambda syms: daily_request(syms, through), identities, through=through)

    def attach_ep(self, daily: dict[str, pd.DataFrame], m15: dict[str, pd.DataFrame], entry: date, now) -> None:
        names = sorted(set(daily) & set(m15))
        if not names:
            return
        through = prior_sessions(entry)[-1]
        identities = self.identities({s: daily[s] for s in names}, latest_closed_session(now))
        self._batch({s: daily[s] for s in names}, "native-daily", lambda syms: daily_request(syms, through),
                    identities, through=through)
        self._batch({s: m15[s] for s in names}, "rth-m15", lambda syms: rth30_request(syms, entry), identities)

    @staticmethod
    def settle(frames: dict[str, pd.DataFrame], decision_now) -> None:
        """Evidence received after the decision clock was not available to that decision."""
        for symbol, frame in frames.items():
            try:
                decision = attached_decision(frame)
            except BarDataError:
                continue
            if decision is not None and decision.status == "AVAILABLE" and \
                    clock(decision.observation.received_at) > clock(decision_now):
                frame.attrs["decision_volume"] = _unavailable(symbol, decision.channel, "RECEIPT_AFTER_DECISION_CLOCK")

    # ---- persisted qualification ---------------------------------------------
    def gate(self, evidence: dict, now, *, refresh: bool, metadata: dict | None = None) -> tuple[str, str | None]:
        """("OK" | "UNAVAILABLE" | "CHANGED", code) for saved volume evidence.

        ``refresh=False`` reads only the cache (no request); ``refresh=True`` refetches the
        exact request keys and re-resolves identity (``metadata``: desk symbol -> Webull
        SecurityMetadata dump). A provider stop, failed refresh or missing cache entry is
        UNAVAILABLE; different used values, identity, source, share basis or rule is CHANGED.
        """
        if self.issue:
            return "UNAVAILABLE", self.issue
        try:
            ident = evidence["identity"]
            desk = ident["desk_symbol"]
            for item in evidence["inputs"]:
                if item["policy"] not in ALPACA_CHANNEL_POLICY.values() or (item["feed"], item["adjustment"]) != ("sip", "split"):
                    return "CHANGED", "VOLUME_SOURCE_CHANGED"
                if item["share_basis_id"] != "alpaca:sip:adjustment=split":
                    return "CHANGED", "SHARE_BASIS_CHANGED"
            setup = evidence["consumer"].split(":")[0]
            if setup in CARDS and CARDS[setup].fingerprint() != evidence["rule_version"]:
                return "CHANGED", "VOLUME_RULE_CHANGED_REQUALIFY"
            if refresh:
                if self.client.network_blocked:
                    return "UNAVAILABLE", NETWORK_FORBIDDEN
                frame = pd.DataFrame()
                frame.attrs["security_metadata"] = (metadata or {}).get(desk)
                current = self.identities({desk: frame}, latest_closed_session(now))[desk]
            elif self._unrecorded:
                # This run could not persist an identity outcome (re-audit R1).
                return "UNAVAILABLE", self._unrecorded
            else:
                # Persisted identity health and open attempts, not just the last pin
                # (audit F1, re-audit R1); no request.
                current = self.store.current(desk)
            if not isinstance(current, IdentityRecord):
                return "UNAVAILABLE", current
            if current.dependency() != ident:
                return "CHANGED", "IDENTITY_CHANGED"
            for item, audit in zip(evidence["inputs"], evidence["audit"]["inputs"]):
                req = BarRequest(symbols=(current.alpaca_symbol,), **audit["request"])
                result = self.client.fetch(req, reuse=not refresh, identities={current.alpaca_symbol: current})
                obs = result.observations.get(current.alpaca_symbol)
                if obs is None:
                    return "UNAVAILABLE", result.failures.get(current.alpaca_symbol) or result.error
                if obs.content_digest == audit["snapshot_digest"]:
                    continue
                daily = item["channel"] == "native-daily"
                values = {(pd.Timestamp(t).tz_convert(ET).date().isoformat() if daily else t): str(v)
                          for t, v in obs.bars}
                if [[k, values.get(k)] for k, _ in item["values"]] != item["values"]:
                    return "CHANGED", "VOLUME_EVIDENCE_REVISED"
            return "OK", None
        except (KeyError, TypeError, ValueError, ValidationError, sqlite3.Error):
            return "UNAVAILABLE", "VOLUME_EVIDENCE_MALFORMED"
        except (AlpacaVolumeError, BarDataError) as exc:
            return "UNAVAILABLE", getattr(exc, "code", "VOLUME_CHECK_FAILED")


class DecisionVolumeSource:
    """Bar source plus the run's volume provider; everything else passes through."""

    def __init__(self, source, provider: AlpacaVolumeProvider):
        self.source, self.decision_volume = source, provider

    def __getattr__(self, name):
        return getattr(self.source, name)


def configured_volume(source, env, *, clock_fn=lambda: datetime.now(timezone.utc), transport=None):
    """Scanner opt-in. A configuration fault isolates volume only, never prices."""
    path = env.get(ENV_CACHE)
    if not path:
        return source
    try:
        budget = int(env.get(ENV_BUDGET, ""))
        if budget < 1:
            raise ValueError("budget")
        cache = VolumeCache(path)
        kwargs = {"transport": transport} if transport is not None else {}
        client = AlpacaVolumeClient.from_env(env, budget=RequestBudget(budget), cache=cache, clock_fn=clock_fn,
                                             **kwargs)
        provider = AlpacaVolumeProvider(client, IdentityStore(path))
    except (ValueError, TypeError, AlpacaVolumeError, BarDataError, OSError, sqlite3.Error):
        provider = AlpacaVolumeProvider.unavailable(CONFIG_UNAVAILABLE)
    return DecisionVolumeSource(source, provider)


def provider_of(source) -> AlpacaVolumeProvider | None:
    return getattr(source, "decision_volume", None)


def needs_volume_guard(persisted: dict) -> bool:
    """Whether a stored event depends on Alpaca evidence (re-audit R2).

    ``persisted`` is the signal store's own row (candidate setup column, candidate
    payload, event terms), read under the signal lock; nothing from a ticket request.
    Only a price-only setup whose candidate and terms both carry no volume evidence
    may skip the volume/identity reservation. Anything unreadable or inconsistent keeps
    it (and ``volume_status`` then decides).
    """
    import json
    try:
        setup = persisted["setup_id"]
        if setup in VOLUME_SETUPS or setup not in CARDS:
            return True
        for text in (persisted["candidate_signal"], persisted["signal_terms"]):
            if text is None:
                continue
            payload = json.loads(text)
            if not isinstance(payload, dict) or payload.get("setup_id") != setup \
                    or payload.get("volume_evidence") is not None:
                return True
        return persisted["candidate_signal"] is None
    except (KeyError, TypeError, ValueError):
        return True
