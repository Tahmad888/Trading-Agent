"""Explicit fictional evidence; never used by a live provider/profile."""
import pandas as pd

from desk.data_basis import PriceBasis, VolumeBasis


def price_evidence(day="2026-09-29", symbol="LEAD", normalization="split_adjusted", actions=()):
    return PriceBasis(source="synthetic fixture", evidence_ref="test generator; NOT provider acceptance",
        symbol=symbol, security_id=f"fixture:{symbol}", currency="USD", coverage_start="2020-01-01",
        basis_session=pd.Timestamp(day).date(), verified_at=pd.Timestamp(day, tz="America/New_York"),
        coverage_complete=True, normalization=normalization, actions=actions).model_dump(mode="json")


def volume_evidence(channel="synthetic daily"):
    return VolumeBasis(source="synthetic fixture", evidence_ref="test generator; NOT provider acceptance",
        channel=channel, definition_id="fixture-regular-trades-v1", units="shares",
        share_basis_id="fixture-common-shares").model_dump(mode="json")
