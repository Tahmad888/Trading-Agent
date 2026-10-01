"""Synthetic terms only: these tests do not attest a real provider's coverage."""
import pytest
from pydantic import ValidationError

from desk.action_evidence import ActionRecord
from desk.bars import BarDataError
from desk.data_basis import compatible_prices
from tests.basis_support import price_evidence
from tests.test_data_contracts import intraday, now
from tests.test_price_volume_basis import set_basis


def record():
    return ActionRecord.model_validate(dict(source="fixture", evidence_ref="fictional receipt 1",
        event_id="stable-id", security_id="fixture:LEAD", currency="USD", kind="cash_dividend",
        effective_session="2026-09-25", status="confirmed", cash_amount="1.50"))


def dividend(**changes):
    values = record().model_dump()
    values.update(changes)
    return ActionRecord.model_validate(values)


def action(item):
    return item.to_action(security_id="fixture:LEAD", currency="USD")


def test_receipt_and_numeric_format_changes_do_not_invalidate_existing_levels():
    old = action(dividend())
    current = action(dividend(cash_amount="1.5000", evidence_ref="fictional receipt 2"))
    assert old.revision == current.revision
    before = price_evidence("2026-09-28", normalization="split_dividend_adjusted", actions=(old,))
    bars = set_basis(intraday(2), normalization="unadjusted", actions=(current,))
    compatible_prices(before, bars, now(), symbol="LEAD")


def test_corrected_economic_terms_block_old_signal_and_rebuilt_basis_can_compare():
    old, current = action(dividend()), action(dividend(cash_amount="1.51"))
    assert old.revision != current.revision
    before = price_evidence("2026-09-28", normalization="split_dividend_adjusted", actions=(old,))
    bars = set_basis(intraday(2), normalization="unadjusted", actions=(current,))
    with pytest.raises(BarDataError, match="Changed corporate-action"):
        compatible_prices(before, bars, now(), symbol="LEAD")
    rebuilt = price_evidence(normalization="split_dividend_adjusted", actions=(current,))
    compatible_prices(rebuilt, bars, now(), symbol="LEAD")


def test_split_ratios_are_exact_and_reverse_splits_are_distinct():
    def split(new, old):
        return action(dividend(kind="split", cash_amount=None, new_shares=new, old_shares=old))
    assert split("10", "1").revision == split("20.0", "2").revision
    assert split("1", "10").revision != split("10", "1").revision


@pytest.mark.parametrize("changes", [
    {"cash_amount": "NaN"}, {"cash_amount": "Infinity"}, {"cash_amount": "0"},
    {"cash_amount": "-1"}, {"kind": "split"}, {"new_shares": "10"},
    {"kind": "split", "cash_amount": None, "new_shares": "1", "old_shares": "1"},
    {"kind": "spinoff"}, {"unresolved_date_conflict": True},
])
def test_ambiguous_unsupported_or_invalid_terms_cannot_be_silently_reclassified(changes):
    with pytest.raises(ValidationError):
        dividend(**changes)


@pytest.mark.parametrize("changes", [{"status": "cancelled"}, {"status": "unknown"},
                                     {"security_id": "another-security"}, {"currency": "CAD"}])
def test_unresolved_status_and_identity_cannot_publish_actions(changes):
    with pytest.raises(BarDataError):
        action(dividend(**changes))
