"""Alpaca provider identity for volume evidence (G5a checkpoint 2). Read-only.

One documented GET of the paper trading host's asset list per run
(https://docs.alpaca.markets/us/reference/get-v2-assets-1, Sourced), sharing the
run's request budget with the bar pages. No account, order or position route
exists here. The asset list describes the *current* listing only; it does not
prove that older bars belong to the same entity (Sourced: same page; asof on the
bars route selects the entity mapping). Bars are requested without ``asof``, so
Alpaca's current entity mapping for the symbol applies; that is recorded on every
mapping and corroborated only by the Webull instrument's own price history over
the volume window (Assumption: not independent proof).

Plan B: an unresolved, ambiguous, conflicting, reused or changed identity makes
that ticker's volume UNAVAILABLE with the reason kept; price checks continue.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from desk.bars import BarDataError
from desk.calendar import clock
from desk.symbols import WEBULL_IDENTITIES

ASSET_HOST = "paper-api.alpaca.markets"
ASSET_ROUTE = "/v2/assets"
ASSET_REFERENCE = "https://docs.alpaca.markets/us/reference/get-v2-assets-1"
ASSET_PARAMS = (("status", "active"), ("asset_class", "us_equity"))
MAPPING_POLICY = "alpaca-current-entity-v1"
ASOF_POLICY = "asof omitted: Alpaca maps the requested symbol to its current entity"
HISTORICAL_MAPPING = ("provider entity mapping; corroborated only by the Webull instrument's price "
                      "history over the window; not independently proven")
# Desk symbol -> Alpaca symbol for share classes. Explicit only: punctuation is never
# stripped or guessed. BRK.B: Alpaca lists class shares with a dot (Assumption until
# the asset list confirms it; an absent symbol is ASSET_NOT_FOUND, never a guess).
CLASS_SHARE_ALIASES = {"BRK.B": "BRK.B"}
PLAIN = re.compile(r"[A-Z][A-Z0-9]{0,9}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

Text = Annotated[str, Field(min_length=1)]


def _digest(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class AssetError(BarDataError):
    def __init__(self, code: str):
        self.code = code
        super().__init__("Alpaca assets: " + code)


class AlpacaAsset(BaseModel):
    """The fields kept from one asset row; tradability flags are metadata, not rules."""
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)
    id: Annotated[str, Field(pattern=UUID.pattern)]
    symbol: Text
    asset_class: Text = Field(alias="class")
    exchange: str
    name: str
    status: Text


class AssetList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: Literal["https://docs.alpaca.markets/us/reference/get-v2-assets-1"] = ASSET_REFERENCE
    params: tuple[tuple[str, str], ...] = ASSET_PARAMS
    received_at: AwareDatetime
    digest: Text           # SHA-256 of the response body
    assets: tuple[AlpacaAsset, ...]


def parse_assets(body: bytes, received_at: datetime) -> AssetList:
    """The whole list or nothing: an unattributable row makes every mapping uncertain."""
    try:
        rows = json.loads(body)
    except (ValueError, TypeError):
        raise AssetError("INVALID_JSON") from None
    if not isinstance(rows, list):
        raise AssetError("INVALID_ASSET_LIST")
    try:
        assets = tuple(AlpacaAsset.model_validate(r) for r in rows)
    except (ValidationError, TypeError):
        raise AssetError("INVALID_ASSET_LIST") from None
    return AssetList(received_at=received_at, digest=hashlib.sha256(body).hexdigest(), assets=assets)


class IdentityRecord(BaseModel):
    """One pinned desk/Webull/Alpaca mapping version. Webull and Alpaca IDs stay apart."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    desk_symbol: Text
    webull_symbol: Text
    webull_instrument_id: Text
    webull_name: str
    webull_exchange: str
    alpaca_symbol: Text
    alpaca_asset_id: Text
    asset_class: Literal["us_equity"]
    alpaca_exchange: str
    alpaca_name: str
    alpaca_status: Literal["active"]
    method: Literal["exact-symbol", "explicit-class-share-alias"]
    policy: Literal["alpaca-current-entity-v1"] = MAPPING_POLICY
    asof_policy: Literal["asof omitted: Alpaca maps the requested symbol to its current entity"] = ASOF_POLICY
    historical_mapping: Text = HISTORICAL_MAPPING
    version: int = Field(ge=1)
    # Set when this version replaced a different identity: windows must start after it.
    valid_after_session: date | None = None
    asset_list_digest: Text
    received_at: AwareDatetime
    mapping_digest: Text

    @staticmethod
    def identity_digest(fields: dict) -> str:
        # Names and exchanges are supporting evidence; their spelling never changes identity.
        keys = ("desk_symbol", "webull_instrument_id", "alpaca_symbol", "alpaca_asset_id", "asset_class",
                "method", "policy")
        return _digest({k: fields[k] for k in keys})

    @property
    def namespace(self) -> str:
        return f"{self.policy}:v{self.version}:{self.mapping_digest[:16]}"

    def dependency(self) -> dict:
        """The identity fields a volume qualification depends on (no receipt times)."""
        return {"desk_symbol": self.desk_symbol, "webull_instrument_id": self.webull_instrument_id,
                "alpaca_symbol": self.alpaca_symbol, "alpaca_asset_id": self.alpaca_asset_id,
                "asset_class": self.asset_class, "method": self.method, "policy": self.policy,
                "asof_policy": self.asof_policy, "version": self.version,
                "valid_after_session": self.valid_after_session.isoformat() if self.valid_after_session else None,
                "mapping_digest": self.mapping_digest, "namespace": self.namespace}


def alpaca_symbol(desk_symbol: str) -> str:
    if desk_symbol in CLASS_SHARE_ALIASES:
        return CLASS_SHARE_ALIASES[desk_symbol]
    if PLAIN.fullmatch(desk_symbol):
        return desk_symbol
    raise AssetError("CLASS_SHARE_ALIAS_REQUIRED")


class IdentityStore:
    """Versioned pins in the volume cache file. Old versions are kept as history."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS asset_lists(digest TEXT PRIMARY KEY, received_at TEXT NOT NULL,
                    count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS mappings(desk_symbol TEXT NOT NULL, version INTEGER NOT NULL,
                    alpaca_asset_id TEXT NOT NULL, mapping_digest TEXT NOT NULL, record TEXT NOT NULL,
                    pinned_at TEXT NOT NULL, last_confirmed_at TEXT NOT NULL,
                    PRIMARY KEY(desk_symbol, version));
            ''')

    def latest(self, desk_symbol: str) -> IdentityRecord | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT record FROM mappings WHERE desk_symbol=? ORDER BY version DESC LIMIT 1",
                             (desk_symbol,)).fetchone()
        return IdentityRecord.model_validate_json(row[0]) if row else None

    def history(self, desk_symbol: str) -> list[IdentityRecord]:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT record FROM mappings WHERE desk_symbol=? ORDER BY version",
                              (desk_symbol,)).fetchall()
        return [IdentityRecord.model_validate_json(r[0]) for r in rows]

    def resolve(self, metadata: dict, assets: AssetList, session: date) -> dict[str, IdentityRecord | str]:
        """desk symbol -> pinned record, or the reason its volume is unavailable.

        ``metadata`` maps desk symbol -> Webull ``SecurityMetadata``; ``session`` is the
        decision's latest completed session (a changed identity is valid only after it).
        """
        by_symbol: dict[str, list[AlpacaAsset]] = {}
        by_id: dict[str, int] = {}
        for asset in assets.assets:
            by_symbol.setdefault(asset.symbol, []).append(asset)
            by_id[asset.id] = by_id.get(asset.id, 0) + 1
        out: dict[str, IdentityRecord | str] = {}
        chosen: dict[str, tuple[AlpacaAsset, str, object]] = {}
        for desk, meta in metadata.items():
            try:
                target = alpaca_symbol(desk)
                if meta is None:
                    raise AssetError("WEBULL_IDENTITY_MISSING")
                known = WEBULL_IDENTITIES.get(desk)
                if known and known[1] != meta.instrument_id:
                    raise AssetError("WEBULL_ALIAS_ID_MISMATCH")
                rows = by_symbol.get(target, [])
                if not rows:
                    raise AssetError("ASSET_NOT_FOUND")
                if len(rows) > 1 or by_id[rows[0].id] > 1:
                    raise AssetError("ASSET_AMBIGUOUS")
                asset = rows[0]
                if asset.status != "active" or asset.asset_class != "us_equity":
                    raise AssetError("ASSET_UNSUPPORTED")
                chosen[desk] = (asset, "explicit-class-share-alias" if desk in CLASS_SHARE_ALIASES
                                else "exact-symbol", meta)
            except AssetError as exc:
                out[desk] = exc.code
        claims: dict[str, list[str]] = {}
        for desk, (asset, _, _) in chosen.items():
            claims.setdefault(asset.id, []).append(desk)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO asset_lists VALUES (?,?,?)",
                       (assets.digest, clock(assets.received_at).isoformat(), len(assets.assets)))
            for desk, (asset, method, meta) in sorted(chosen.items()):
                if len(claims[asset.id]) > 1:
                    out[desk] = "ASSET_CONFLICT"
                    continue
                fields = dict(desk_symbol=desk, webull_symbol=meta.provider_symbol or desk,
                              webull_instrument_id=meta.instrument_id, webull_name=meta.name,
                              webull_exchange=meta.exchange_code, alpaca_symbol=asset.symbol,
                              alpaca_asset_id=asset.id, asset_class=asset.asset_class,
                              alpaca_exchange=asset.exchange, alpaca_name=asset.name, alpaca_status=asset.status,
                              method=method, policy=MAPPING_POLICY, asset_list_digest=assets.digest,
                              received_at=assets.received_at)
                digest = IdentityRecord.identity_digest(fields)
                latest = db.execute("SELECT version,mapping_digest,record FROM mappings WHERE desk_symbol=? "
                                    "ORDER BY version DESC LIMIT 1", (desk,)).fetchone()
                other = db.execute(
                    "SELECT m.desk_symbol FROM mappings m WHERE m.alpaca_asset_id=? AND m.desk_symbol<>? AND "
                    "m.version=(SELECT MAX(version) FROM mappings WHERE desk_symbol=m.desk_symbol)",
                    (asset.id, desk)).fetchone()
                if other is not None:
                    out[desk] = "ASSET_REUSED_BY_ANOTHER_SYMBOL"
                    continue
                at = clock(assets.received_at).isoformat()
                if latest is not None and latest[1] == digest:
                    db.execute("UPDATE mappings SET last_confirmed_at=? WHERE desk_symbol=? AND version=?",
                               (at, desk, latest[0]))
                    out[desk] = IdentityRecord.model_validate_json(latest[2])
                    continue
                version = 1 if latest is None else latest[0] + 1
                record = IdentityRecord(**fields, version=version, mapping_digest=digest,
                                        valid_after_session=None if latest is None else session)
                db.execute("INSERT INTO mappings VALUES (?,?,?,?,?,?,?)",
                           (desk, version, asset.id, digest, record.model_dump_json(), at, at))
                out[desk] = record
        return out
