"""Normalize reviewed action terms into the existing revision contract.

This is not a vendor parser or coverage attestation. A source adapter must resolve
event type, effective date, status and security mapping first. Conflicting source
records must not be made to agree by dropping fields. No live adapter is enabled.
"""
import hashlib
import json
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import Field, model_validator

from desk.bars import BarDataError
from desk.data_basis import CorporateAction, Evidence, Text

Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class ActionRecord(Evidence):
    """Explicit terms in the mapped Webull security identity and share units.

    Cash amount is per share on the effective date, in the specified currency.
    A split is new_shares for old_shares (including reverse splits). Records must
    have a stable source event ID; do not construct identity from mutable terms.
    """
    event_id: Text
    security_id: Text
    currency: Text
    kind: Literal["split", "cash_dividend"]
    effective_session: date
    status: Literal["confirmed", "cancelled", "unknown"]
    cash_amount: Positive | None = None
    new_shares: Positive | None = None
    old_shares: Positive | None = None

    @model_validator(mode="after")
    def validate_terms(self):
        if self.kind == "split":
            if self.cash_amount is not None or self.new_shares is None or self.old_shares is None:
                raise ValueError("A split requires only new/old share terms")
            if self.new_shares == self.old_shares:
                raise ValueError("A one-for-one record is not an established split")
        elif self.cash_amount is None or self.new_shares is not None or self.old_shares is not None:
            raise ValueError("A cash dividend requires only its per-share cash amount")
        return self

    def to_action(self, *, security_id: str, currency: str) -> CorporateAction:
        """Bind to the price producer, then hash economic terms, not fetch times.

        A cancellation requires ledger reconciliation (including removal of the
        old event), not a new active action. Existing comparisons catch removals.
        """
        if self.status != "confirmed":
            raise BarDataError("Unconfirmed/cancelled action requires source reconciliation")
        if self.security_id != security_id or self.currency != currency:
            raise BarDataError("Action security/currency does not match the price producer")
        payload = self.model_dump(mode="json", exclude={"evidence_ref", "cash_amount", "new_shares", "old_shares"})
        if self.kind == "split":
            payload["ratio"] = str(Fraction(self.new_shares) / Fraction(self.old_shares))
        else:
            # Fraction gives exact numeric equality without Decimal context rounding.
            payload["cash_amount"] = str(Fraction(self.cash_amount))
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return CorporateAction(source=self.source, evidence_ref=self.evidence_ref,
                               event_id=self.event_id, kind=self.kind,
                               effective_session=self.effective_session,
                               revision="terms-v1:" + digest)
