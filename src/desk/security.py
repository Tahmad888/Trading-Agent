"""Underlying identity for discovery. Optionability/borrow are later execution inputs."""
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from desk.bars import BarDataError
from desk.symbols import canonical_symbol


class SecurityMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,19}$")]
    provider_symbol: str | None = None
    instrument_id: Annotated[str, Field(min_length=1)]
    name: Annotated[str, Field(min_length=1)]
    category: Literal["US_STOCK"]
    sub_category: Literal["COMMON_STOCK", "ETF", "PREFERRED_STOCK", "WARRANT", "UNITS", "RIGHT"]
    exchange_code: Annotated[str, Field(min_length=1)]
    currency: Literal["USD"]
    observed_at: AwareDatetime

    @property
    def bar_category(self):
        return "US_ETF" if self.sub_category == "ETF" else "US_STOCK"


def securities(source, symbols, skipped):
    """Resolve explicit US securities in batches; reject ambiguous or missing IDs."""
    names = sorted({canonical_symbol(s) for s in symbols})
    out = {}
    for i in range(0, len(names), 100):
        batch = names[i:i + 100]
        try:
            method = getattr(source, "security_metadata", None)
            if method is None:
                raise BarDataError("security metadata source unavailable")
            rows = method(batch)
            if not isinstance(rows, list):
                raise BarDataError("malformed security metadata response")
        except BarDataError as exc:
            for name in batch:
                skipped[name] = f"metadata unavailable: {exc}"
            continue
        seen, bad = set(), set()
        for row in rows:
            symbol = row.get("symbol") if isinstance(row, Mapping) else None
            if symbol not in batch:
                # Unattributable payload makes the batch uncertain.
                for name in batch:
                    skipped[name] = "metadata contains an unrequested/malformed identity"
                bad.update(batch)
                continue
            if symbol in seen:
                bad.add(symbol)
                skipped[symbol] = "duplicate/ambiguous security identity"
                continue
            seen.add(symbol)
            try:
                metadata = SecurityMetadata.model_validate(row)
                if metadata.sub_category not in {"COMMON_STOCK", "ETF"}:
                    skipped[symbol] = "unsupported security type: " + metadata.sub_category
                    bad.add(symbol)
                    continue
                out[symbol] = metadata
            except (ValidationError, ValueError):
                bad.add(symbol)
                skipped[symbol] = "malformed or unsupported security metadata"
        for name in batch:
            if name in bad or name not in seen:
                out.pop(name, None)
                skipped.setdefault(name, "security metadata missing")
    by_id = {}
    for name, item in out.items():
        by_id.setdefault(item.instrument_id, []).append(name)
    for aliases in by_id.values():
        if len(aliases) > 1:
            for name in aliases:
                out.pop(name, None)
                skipped[name] = "instrument identity mapped to multiple requested symbols"
    return out
