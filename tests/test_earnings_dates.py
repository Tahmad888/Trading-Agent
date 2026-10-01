"""Publication precision and bounded issuer-review acceptance; no live requests."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from desk.earnings import Published
from desk.earnings_check import check, main
from tests.test_earnings import evidence, run, CUP
from tests.test_signal_lifecycle import now

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "config/step09-review/NVDA-2026-10-01.json"
ASOF = datetime(2026, 10, 1, 19, 0, 20, tzinfo=timezone.utc)


def test_old_date_only_quarters_qualify_without_inventing_exact_time():
    data = evidence()
    for key in ("current", "prior"):
        data[key]["published_on"] = data[key].pop("published_at")[:10]
    result = run(data, CUP)
    assert result["status"] == "QUALIFIED"
    assert result["evidence"]["current"]["published_at"] is None
    assert result["publication_bounds"]["current"] == "2026-08-02T00:00:00-12:00"


@pytest.mark.parametrize("receipt,expected", [
    ("2026-09-29T13:59:59Z", "QUALIFIED"),
    ("2026-09-29T14:00:00Z", "QUALIFIED"),
    ("2026-09-29T14:00:01Z", "PENDING_EVIDENCE"),
])
def test_date_only_catalyst_requires_evidence_available_by_trigger(receipt, expected):
    data = evidence()
    data["catalysts"][0].pop("published_at")
    data["catalysts"][0].update(published_on="2026-09-29", received_at=receipt)
    data["reviewed_at"] = "2026-09-29T14:01:00Z"
    assert run(data, at=now(minute=2))["status"] == expected


@pytest.mark.parametrize("cutoff,expected", [("2026-09-30T11:59:59Z", False),
                                            ("2026-09-30T12:00:00Z", True)])
def test_date_only_latest_possible_day_end(cutoff, expected):
    record = Published(source_ref="synthetic", published_on="2026-09-29",
                       received_at="2026-10-01T12:00:00Z")
    assert (record.known_published_by <= datetime.fromisoformat(cutoff)) is expected


@pytest.mark.parametrize("fields", [
    {}, {"published_at": "2026-09-29T12:00:00Z", "published_on": "2026-09-29"},
    {"published_on": "2026-10-02"},
])
def test_missing_ambiguous_or_impossible_publication_rejected(fields):
    with pytest.raises(ValueError):
        Published(source_ref="synthetic", received_at="2026-09-29T13:00:00Z", **fields)


def test_date_only_quarter_cannot_publish_before_period_end():
    data = evidence()
    data["current"].pop("published_at")
    data["current"]["published_on"] = data["current"]["period_end"]
    assert run(data, CUP)["status"] == "PENDING_EVIDENCE"


def test_date_only_calendar_preserves_estimate_and_receipt_cutoff():
    data = evidence()
    claim = data["calendar"][0]
    claim.pop("published_at")
    claim["published_on"] = "2026-09-29"
    assert run(data)["next_earnings"]["status"] == "ESTIMATED"
    claim["received_at"] = "2026-09-29T14:01:00Z"
    data["reviewed_at"] = claim["received_at"]
    assert run(data)["next_earnings"]["status"] == "UNKNOWN"


def test_real_issuer_review_arithmetic_is_not_ep_or_chart_qualification():
    result = check(REVIEW, "NVDA", "913257561", ASOF, replay=True)
    assert result["status"] == "EVALUATED"
    assert result["provider_requests"] == 0 and result["mode"] == "historical replay"
    cup, ep = result["checks"]["cup"], result["checks"]["ep"]
    assert cup["status"] == "QUALIFIED"
    assert Decimal(cup["growth"]["eps"]) == (Decimal("2.46") - Decimal("1.08")) / Decimal("1.08")
    assert Decimal(cup["growth"]["sales"]) == Decimal(49478) / Decimal(46743)
    assert ep["status"] == "PENDING_EVIDENCE" and "catalyst" in ep["reasons"][0]
    assert cup["next_earnings"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("symbol,security_id,at", [
    ("NVDA", "wrong", ASOF), ("SPY", "913243251", ASOF),
    ("NVDA", "913257561", ASOF.replace(day=2)),
    ("NVDA", "913257561", ASOF.replace(hour=18)),
])
def test_review_cannot_authorize_other_identity_or_outside_review_window(symbol, security_id, at):
    assert check(REVIEW, symbol, security_id, at)["status"] == "UNAVAILABLE"


def test_cli_replay_and_malformed_file(tmp_path, capsys):
    args = ["--file", str(REVIEW), "--symbol", "NVDA", "--security-id", "913257561",
            "--at", ASOF.isoformat()]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "historical replay"
    bad = tmp_path / "bad.json"
    bad.write_text("secret text: not JSON")
    args[1] = str(bad)
    assert main(args) == 1
    assert "secret" not in capsys.readouterr().out


def test_cli_rejects_naive_replay_time():
    with pytest.raises(SystemExit) as error:
        main(["--file", str(REVIEW), "--symbol", "NVDA", "--security-id", "913257561",
              "--at", "2026-10-01T19:00:20"])
    assert error.value.code == 2
