"""Fictional mapping/channel reviews. Real observations are replayed separately."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pandas as pd
import pytest

from desk.action_ledger import ActionLedger, SecurityReview, snapshot_from_report
from desk.action_source import ActionBackedSource, PriceChannelReview, configured_source
from desk.alphavantage import ActionObservation, ActionSnapshot
from desk.bar_contract import completed_intraday
from desk.bars import BarDataError
from desk.data_basis import compatible_prices
from desk.webull import WebullData

NOW = datetime(2026, 10, 1, 14, 32, tzinfo=timezone.utc)


def review(**kw):
    values = dict(source="fictional reviewer", evidence_ref="test only",
        symbol="NVDA", source_symbol="NVDA", security_id="913257561", currency="USD",
        exchange_mic="XNAS", instrument_name="fictional NVDA mapping",
        identity_evidence_ref="fixture identity", coverage_evidence_ref="fixture coverage",
        coverage_start=date(2020, 1, 1), valid_through=date(2026, 12, 31),
        reviewed_at=NOW-timedelta(hours=1), cash_amount_basis="effective_date_per_share",
        exceptional_actions="none_found")
    return SecurityReview.model_validate({**values, **kw})


def pair(*, amount="0.01", split="10", at=NOW-timedelta(minutes=2), future=False, zero=False):
    splits = (ActionObservation(date(2024, 6, 10), Decimal(split)),) if split else ()
    dividends = [ActionObservation(date(2024, 6, 11), Decimal(amount))] if amount else []
    if future:
        dividends.append(ActionObservation(date(2026, 10, 2), Decimal("0.25")))
    if zero:
        dividends.append(ActionObservation(date(2024, 5, 29), Decimal(0)))
    return (ActionSnapshot("NVDA", "SPLITS", at, splits),
            ActionSnapshot("NVDA", "DIVIDENDS", at, tuple(dividends)))


def basis(store, at=NOW):
    return store.basis("NVDA", at, normalization="split_dividend_adjusted")


def test_publish_restart_and_zero_future_rows_preserved_without_effect(tmp_path):
    path = tmp_path/'actions.sqlite'
    store = ActionLedger(path)
    result = store.publish(review(), *pair(zero=True, future=True), NOW)
    assert result['status'] == 'READY' and not result['rebuild_required']
    restored = ActionLedger(path)
    b = basis(restored)
    assert len(b.actions) == 2
    with closing(sqlite3.connect(path)) as db:
        snapshots = json.loads(db.execute('SELECT snapshots FROM generations').fetchone()[0])
    divs = snapshots[1]['records']
    assert len(divs) == 3
    assert divs[0]['issues'][0] == 'ZERO_AMOUNT_IGNORED'
    assert divs[-1]['event_date'] == '2026-10-02'


def test_unchanged_receipts_aux_dates_and_numeric_format_do_not_revise(tmp_path):
    store = ActionLedger(tmp_path/'a.sqlite')
    store.publish(review(), *pair(), NOW)
    before = basis(store)
    splits, divs = pair(amount='0.01000', split='10.000', at=NOW+timedelta(seconds=1))
    divs = replace(divs, rows=(replace(divs.rows[0], declaration_date=date(2024, 5, 22)),))
    result = store.publish(review(), splits, divs, NOW+timedelta(seconds=2))
    assert not result['rebuild_required']
    assert [a.revision for a in before.actions] == [a.revision for a in basis(store, NOW+timedelta(seconds=3)).actions]


def test_correction_changes_revisions_and_stores_history(tmp_path):
    store = ActionLedger(tmp_path/'a.sqlite')
    store.publish(review(), *pair(), NOW)
    before = basis(store)
    result = store.publish(review(), *pair(amount='0.02'), NOW)
    assert len(result['corrected']) == 1 and result['rebuild_required']
    assert before.actions[1].revision != basis(store).actions[1].revision
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute('SELECT COUNT(*) FROM generations').fetchone()[0] == 2


def test_empty_split_response_accepted_with_review_and_both_sources(tmp_path):
    store = ActionLedger(tmp_path/'a.sqlite')
    assert store.publish(review(), *pair(split=None), NOW)['status'] == 'READY'
    assert len(basis(store).actions) == 1


@pytest.mark.parametrize('kind', ['removed', 'date_changed', 'positive_to_zero'])
def test_removals_block_until_reconciled_and_persist_after_restart(tmp_path, kind):
    store = ActionLedger(tmp_path/'a.sqlite')
    store.publish(review(), *pair(), NOW)
    splits, divs = pair(amount=None if kind == 'removed' else '0' if kind == 'positive_to_zero' else '0.01')
    if kind == 'date_changed':
        divs = replace(divs, rows=(replace(divs.rows[0], event_date=date(2024, 6, 12)),))
    result = store.publish(review(), splits, divs, NOW)
    assert result['status'] == 'REMOVALS_REQUIRE_REVIEW' and result['removed']
    with pytest.raises(BarDataError, match='unavailable'):
        basis(ActionLedger(store.path))
    result = store.publish(review(), splits, divs, NOW, accept_removals=True)
    assert result['status'] == 'READY' and result['rebuild_required']


def test_new_future_event_requires_fresh_day_then_activates(tmp_path):
    store = ActionLedger(tmp_path/'a.sqlite')
    store.publish(review(), *pair(future=True), NOW)
    with pytest.raises(BarDataError, match='stale'):
        basis(store, NOW+timedelta(days=1))
    result = store.publish(review(), *pair(future=True, at=NOW+timedelta(days=1)), NOW+timedelta(days=1))
    assert result['rebuild_required'] and len(result['added']) == 1
    assert len(basis(store, NOW+timedelta(days=1)).actions) == 3


@pytest.mark.parametrize('fault', ['yesterday', 'future', 'symbol', 'function', 'identity',
                                  'shorter_history', 'unsupported', 'unreviewed', 'expired'])
def test_failed_refresh_does_not_reuse_old_ready_generation(tmp_path, fault):
    store = ActionLedger(tmp_path/'a.sqlite'); rev = review()
    store.publish(rev, *pair(), NOW)
    s, d = pair()
    if fault == 'yesterday': d = replace(d, received_at=NOW-timedelta(days=1))
    if fault == 'future': d = replace(d, received_at=NOW+timedelta(minutes=1))
    if fault == 'symbol': d = replace(d, symbol='OTHER')
    if fault == 'function': d = replace(d, function='SPLITS')
    if fault == 'identity': rev = review(security_id='other')
    if fault == 'shorter_history': rev = review(coverage_start=date(2021, 1, 1))
    if fault == 'unsupported': rev = review(exceptional_actions='unresolved')
    if fault == 'unreviewed': rev = review(reviewed_at=NOW+timedelta(minutes=1))
    if fault == 'expired': rev = review(valid_through=date(2026, 9, 30))
    with pytest.raises(BarDataError): store.publish(rev, s, d, NOW)
    with pytest.raises(BarDataError, match='unavailable'): basis(store)
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute('SELECT COUNT(*) FROM generations').fetchone()[0] == 1


def test_older_same_day_source_receipt_cannot_overwrite_newer(tmp_path):
    store = ActionLedger(tmp_path/'a.sqlite')
    store.publish(review(), *pair(at=NOW), NOW)
    with pytest.raises(BarDataError, match='older observations'):
        store.publish(review(), *pair(), NOW+timedelta(minutes=1))


def test_safe_report_round_trip_and_validation():
    for snap in pair(zero=True):
        parsed = snapshot_from_report(snap.report(include_records=True))
        assert parsed.received_at == snap.received_at and len(parsed.rows) == len(snap.rows)
    raw = pair()[0].report(include_records=True)
    for change in ({'received_at': None}, {'received_at': '2026-10-01T14:00:00'},
                   {'records': []}, {'status': 'UNAVAILABLE'}, {'function': 'OTHER'}):
        with pytest.raises(BarDataError): snapshot_from_report({**raw, **change})


def channel():
    return PriceChannelReview(source='test only', evidence_ref='fictional channel review',
        host='api.sandbox.webull.com', symbol='NVDA', timeframe='M15', normalization='unadjusted')


def native_source(at, *, instrument='913257561'):
    rows = [dict(time=f'2026-10-01T{t}:00.000+0000', open='120', high='122', low='119',
                 close='121', volume='10000', trading_session='RTH')
            for t in ('13:30', '13:45', '14:00', '14:15', '14:30')]
    payload = {'result': [dict(symbol='NVDA', instrument_id=instrument, delay_minutes=0, result=rows)]}
    def transport(req, timeout):
        if "/instruments/stocks/profiles/list" in req.full_url:
            return json.dumps({"data": [{"symbol": "NVDA", "instrument_id": instrument,
                "category": "US_STOCK", "sub_category": "COMMON_STOCK", "currency": "USD",
                "name": "fictional NVDA metadata", "exchange_code": "NSQ"}]}).encode()
        return json.dumps(payload).encode()
    return WebullData('fictional-key','fictional-secret', host='api.sandbox.webull.com',
        clock=lambda: at, min_interval=0, transport=transport)


def wrapped(store, at=NOW+timedelta(seconds=1), **kw):
    return ActionBackedSource(native_source(at, **kw), store, (channel(),),
                             host='api.sandbox.webull.com', clock=lambda: at)


def fetch(source):
    return source.bars(['NVDA'], category='US_STOCK', timespan='M15')['NVDA']


def test_end_to_end_adapter_ledger_correction_invalidates_old_signal_then_rebuild(tmp_path):
    store = ActionLedger(tmp_path/'a.sqlite')
    store.publish(review(), *pair(), NOW)
    first = completed_intraday(fetch(wrapped(store)), NOW+timedelta(seconds=1))
    assert len(first) == 4 and first.index[-1].minute == 15
    old_signal_basis = first.attrs['bar_provenance']['price_basis']
    store.publish(review(), *pair(amount='0.02', at=NOW+timedelta(seconds=2)), NOW+timedelta(seconds=3))
    current = fetch(wrapped(store, NOW+timedelta(seconds=4)))
    with pytest.raises(BarDataError, match='Changed corporate-action'):
        compatible_prices(old_signal_basis, current, NOW+timedelta(seconds=4), symbol='NVDA')
    # A fresh scanner calculation uses fresh bars and the new action basis.
    rebuilt = completed_intraday(current, NOW+timedelta(seconds=4))
    compatible_prices(rebuilt.attrs['bar_provenance']['price_basis'], current,
                      NOW+timedelta(seconds=4), symbol='NVDA')
    assert rebuilt.close.tolist() == first.close.tolist()  # no double price adjustment


def test_cached_bars_cannot_be_relabelled_after_action_refresh(tmp_path):
    store = ActionLedger(tmp_path/'a.sqlite'); store.publish(review(), *pair(), NOW)
    source = wrapped(store, NOW+timedelta(seconds=1))
    cached = fetch(source)
    store.publish(review(), *pair(amount='0.02'), NOW+timedelta(seconds=2))
    source.source.bars = lambda *a, **k: {'NVDA': cached}
    source._clock = lambda: NOW+timedelta(seconds=3)
    with pytest.raises(BarDataError, match='predate'): fetch(source)


def test_wrong_instrument_unknown_mapping_and_host_rejected(tmp_path):
    store = ActionLedger(tmp_path/'a.sqlite'); store.publish(review(), *pair(), NOW)
    with pytest.raises(BarDataError, match='instrument'):
        fetch(wrapped(store, instrument='wrong'))
    with pytest.raises(BarDataError, match='No reviewed'):
        wrapped(store).bars(['SPY'], category='US_ETF', timespan='M15')
    with pytest.raises(BarDataError, match='different Webull host'):
        ActionBackedSource(native_source(NOW), store, (channel(),), host='api.webull.com')


def test_opt_in_configuration_is_required(tmp_path):
    source = native_source(NOW)
    assert configured_source(source, {}) is source
    with pytest.raises(BarDataError): configured_source(source, {'DESK_ACTION_LEDGER': 'missing'})
    store = ActionLedger(tmp_path/'a.sqlite'); store.publish(review(), *pair(), NOW)
    config = tmp_path/'channels.json'; config.write_text(json.dumps([channel().model_dump(mode='json')]))
    wrapped_source = configured_source(source, {'DESK_ACTION_LEDGER': str(store.path),
        'DESK_ACTION_CHANNELS': str(config), 'WEBULL_HOST': 'api.sandbox.webull.com'}, clock=lambda: NOW)
    assert len(completed_intraday(fetch(wrapped_source), NOW)) == 4


def test_reported_nvda_dividends_import_without_extra_api_calls(tmp_path):
    from pathlib import Path
    raw = json.loads((Path(__file__).parent/'fixtures/alphavantage_nvda_dividends_report.json').read_text())
    divs = snapshot_from_report(raw['report'])
    assert len(divs.rows) == 57
    assert divs.received_at.isoformat() == '2026-10-01T06:31:41.516285+00:00'
    assert next(r for r in divs.rows if r.event_date == date(2024, 5, 29)).value == 0
    store = ActionLedger(tmp_path/'a.sqlite')
    # Split fixture and review are fictional; actual dividend payload is not.
    store.publish(review(), pair()[0], divs, NOW)
    published = basis(store)
    assert not any(a.effective_session == date(2024, 5, 29) for a in published.actions)
    assert any(a.effective_session == date(2024, 6, 11) for a in published.actions)


def test_actual_webull_rth_payload_through_adapter_ledger_and_completion(tmp_path):
    from pathlib import Path
    raw = json.loads((Path(__file__).parent/'fixtures/webull_rth_2026-10-01.json').read_text())
    for call in raw['calls']:
        req = json.loads(call['body']); item = call['reply']['result'][0]
        symbol = item['symbol']; at = datetime.fromisoformat(call['received'])
        store = ActionLedger(tmp_path/f'{symbol}.sqlite')
        # Identity/coverage review below is test-only, not live source acceptance.
        rev = review(symbol=symbol, source_symbol=symbol, security_id=item['instrument_id'])
        snaps = tuple(replace(s, symbol=symbol) for s in pair(at=at-timedelta(seconds=2)))
        store.publish(rev, *snaps, at-timedelta(seconds=1))
        c = channel().model_copy(update={'symbol': symbol})
        source = WebullData('fixture-key','fixture-secret', host='api.sandbox.webull.com',
            min_interval=0, clock=lambda: at, transport=lambda *a: json.dumps(call['reply']).encode())
        wrapped_source = ActionBackedSource(source, store, (c,), host='api.sandbox.webull.com', clock=lambda: at)
        frame = wrapped_source.bars([symbol], category=req['category'], timespan='M15',
                                    real_time_required=False)[symbol]
        completed = completed_intraday(frame, at)
        assert len(completed) == 3
        assert completed.index[-1] == pd.Timestamp('2026-10-01T14:00:00Z')
        assert completed.close.iloc[-1] == float(item['result'][0]['close'])


def test_import_cli_and_failed_second_source_disable_previous_generation(tmp_path, monkeypatch, capsys):
    from desk import action_import
    class FixedClock:
        @staticmethod
        def now(tz): return NOW
    monkeypatch.setattr(action_import, 'datetime', FixedClock)
    review_path = tmp_path/'review.json'; review_path.write_text(review().model_dump_json())
    split_path = tmp_path/'splits.json'; div_path = tmp_path/'divs.json'
    s, d = pair()
    split_path.write_text(json.dumps(s.report(include_records=True)))
    div_path.write_text(json.dumps(d.report(include_records=True)))
    db = tmp_path/'a.sqlite'
    args = ['--review', str(review_path), '--splits', str(split_path), '--dividends', str(div_path), '--database', str(db)]
    assert action_import.main(args) == 0
    assert basis(ActionLedger(db))
    div_path.write_text(json.dumps({'status':'UNAVAILABLE','reason':'RATE_LIMITED'}))
    assert action_import.main(args) == 1
    with pytest.raises(BarDataError, match='unavailable'): basis(ActionLedger(db))
    assert 'RATE_LIMITED' not in capsys.readouterr().out  # fixed importer diagnostics


def test_scanner_reports_corrected_action_without_triggering_persisted_signal(tmp_path):
    from desk.scanner import intraday_scan
    from desk.playbook.triggers import Signal
    store = ActionLedger(tmp_path/'a.sqlite'); store.publish(review(), *pair(), NOW)
    initial = fetch(wrapped(store))
    sig = Signal(setup_id='7_luk_pullback_reclaim', symbol='NVDA', direction='long',
                 as_of=pd.Timestamp('2026-09-30T20:00:00Z'), trigger=120, stop=118,
                 price_basis=initial.attrs['bar_provenance']['price_basis'])
    at = NOW+timedelta(seconds=1)
    assert len(intraday_scan(wrapped(store, at), [sig], at).triggered) == 1
    store.publish(review(), *pair(amount='0.02', at=NOW+timedelta(seconds=2)), NOW+timedelta(seconds=3))
    at = NOW+timedelta(seconds=4)
    report = intraday_scan(wrapped(store, at), [sig], at)
    assert not report.triggered and 'Changed corporate-action' in report.skipped['NVDA']
