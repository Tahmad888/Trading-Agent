"""G1: price/scanner integration with synthetic data and no provider requests."""
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from desk import scanner as sc
from desk.earnings import scanner_source, qualify, UnavailableEarningsSource
from desk.webull import WebullData
from tests.test_earnings import CUP, EP, evidence
from tests.test_earnings_refresh import policy
from tests.test_scanner import Fake, frames, sig, m15, ET
from tests.test_signal_lifecycle import now, BASE

BREAKOUT = '1_qullamaggie_breakout'


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    import os
    for name in list(os.environ):
        if name.startswith(('DESK_EARNINGS_', 'DESK_ACTION_')):
            monkeypatch.delenv(name)


def fail_refresh(monkeypatch, exc):
    from desk import earnings_refresh
    def broken(*args, **kwargs):
        raise exc
    monkeypatch.setattr(earnings_refresh, 'refresh_configured', broken)


@pytest.mark.parametrize('fault', ['malformed_policy', 'missing_policy', 'corrupt_cache',
                                  'blocked_cache_path', 'conflicting_sources', 'refresh_database', 'refresh_os'])
def test_cli_earnings_failure_preserves_price_scan_and_logs_safe_issue(tmp_path, monkeypatch, fault, capsys):
    config = policy(tmp_path)
    cache = tmp_path / 'earnings.sqlite'
    monkeypatch.setenv('DESK_EARNINGS_POLICY', str(config))
    monkeypatch.setenv('DESK_EARNINGS_CACHE', str(cache))
    if fault == 'malformed_policy':
        config.write_text('SECRET malformed policy')
    elif fault == 'missing_policy':
        config.unlink()
    elif fault == 'corrupt_cache':
        cache.write_text('SECRET not a database')
    elif fault == 'blocked_cache_path':
        cache.mkdir()
    elif fault == 'conflicting_sources':
        monkeypatch.setenv('DESK_EARNINGS_EVIDENCE', 'SECRET-conflict')
    elif fault == 'refresh_database':
        fail_refresh(monkeypatch, sqlite3.DatabaseError('SECRET database failure'))
    elif fault == 'refresh_os':
        fail_refresh(monkeypatch, PermissionError('SECRET provider/path detail'))
    source = Fake(frames())
    monkeypatch.setattr(WebullData, 'from_env', staticmethod(lambda: source))
    monkeypatch.setattr(sc, 'datetime', type('Clock', (datetime,), {
        'now': staticmethod(lambda tz=None: datetime(2026, 9, 29, 20, 11, tzinfo=timezone.utc))}))
    watchlist = tmp_path / 'watchlist.json'
    watchlist.write_text(json.dumps(['LEAD']))
    root = tmp_path / 'scan'
    assert sc.main(['--watchlist', str(watchlist), '--data-dir', str(root)]) == 0
    record = sc.ScanLog(root).records()[0]
    assert record['error'] is None and record['market'] == 'full'
    assert any(s['setup_id'] == BREAKOUT for s in record['armed'])
    assert record['qualification'][f'LEAD/{BREAKOUT}']['status'] == 'NOT_REQUIRED'
    assert record['discovery']['earnings_source']['status'] == 'UNAVAILABLE'
    assert source.calls and 'SECRET' not in json.dumps(record) + capsys.readouterr().out


def test_earnings_setup_does_not_remove_action_wrapper_or_use_raw_prices(monkeypatch):
    class Prices:
        def bars(self, *args, **kwargs):
            raise ValueError('action basis rejected')
    class Raw:
        def bars(self, *args, **kwargs):
            pytest.fail('must not bypass action-backed prices')
    fail_refresh(monkeypatch, RuntimeError('SECRET'))
    source = scanner_source(Prices(), {}, refresh_source=Raw())
    with pytest.raises(ValueError, match='action basis rejected'):
        source.bars(['LEAD'], timespan='D')


def test_failure_masks_underlying_success_but_breakout_trigger_and_review_continue(tmp_path, monkeypatch):
    class Source(Fake):
        def earnings_evidence(self, symbol):
            return evidence()
    base = Source({('LEAD', 'M15'): m15(BASE)})
    log = sc.ScanLog(tmp_path)
    first = sc.intraday_scan(base, [sig(CUP), sig(BREAKOUT)], now(), store=log.signals)
    assert all(t['qualified_for_analysis'] for t in first.triggered)
    fail_refresh(monkeypatch, RuntimeError('SECRET refresh failed'))
    failed = scanner_source(base, {}, refresh_source=base)
    assert isinstance(failed, UnavailableEarningsSource)
    for event in first.triggered:
        review = sc.revalidate_signal(failed, log, event['event_id'], now(),
                                      symbol='LEAD', price=101.5, quote_at=now())
        assert review['eligible'] == (event['setup_id'] == BREAKOUT)
    fresh = sc.intraday_scan(failed, [sig(CUP), sig(BREAKOUT)], now(), store=sc.ScanLog(tmp_path/'fresh').signals)
    assert {e['setup_id']: e['qualified_for_analysis'] for e in fresh.triggered} == {CUP: False, BREAKOUT: True}
    assert qualify(failed, sig(EP), now())['status'] == 'PENDING_EVIDENCE'
    assert qualify(failed, sig(CUP), now())['status'] == 'PENDING_EVIDENCE'
    assert qualify(failed, sig(BREAKOUT), now())['status'] == 'NOT_REQUIRED'
    assert 'SECRET' not in (tmp_path/'earnings-reviews.jsonl').read_text()
    # Recovery on the next configured scan preserves the event, but reevaluates evidence.
    from desk import earnings_refresh
    monkeypatch.setattr(earnings_refresh, 'refresh_configured', lambda *args: None)
    recovered = scanner_source(base, {}, refresh_source=base)
    cup = next(e for e in first.triggered if e['setup_id'] == CUP)
    assert sc.revalidate_signal(recovered, log, cup['event_id'], now(), symbol='LEAD', price=101.5, quote_at=now())['eligible']


@pytest.mark.parametrize('fault', ['malformed', 'expired'])
def test_per_symbol_bad_evidence_does_not_contaminate_other_symbols(tmp_path, fault):
    good = evidence()
    bad = evidence()
    bad['symbol'], bad['security_id'] = 'BAD', 'fixture:BAD'
    if fault == 'malformed':
        bad['current']['eps']['value'] = 'not-a-number'
    else:
        bad['valid_until'] = '2026-09-29T13:46:00Z'
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps({'schema_version': 1, 'securities': [good, bad]}))
    source = scanner_source(Fake({}), {'DESK_EARNINGS_EVIDENCE': str(path)}, refresh_source=None)
    from dataclasses import replace
    bad_signal = replace(sig(CUP), symbol='BAD', price_basis={'security_id': 'fixture:BAD'})
    assert qualify(source, bad_signal, now())['status'] == 'PENDING_EVIDENCE'
    assert qualify(source, sig(CUP), now())['status'] == 'QUALIFIED'
    assert qualify(source, replace(bad_signal, setup_id=BREAKOUT), now())['status'] == 'NOT_REQUIRED'


@pytest.mark.parametrize('benchmark', ['SPY', 'QQQ'])
def test_missing_benchmark_remains_visible_with_earnings_outage(benchmark):
    data = frames()
    del data[(benchmark, 'D')]
    source = UnavailableEarningsSource(Fake(data))
    rec, armed = sc.close_scan(source, ['LEAD'], datetime(2026, 9, 29, 16, 10, tzinfo=ET))
    assert rec.error and benchmark in rec.skipped and not armed
    assert rec.scanned == 2  # healthy LEAD and the other benchmark still processed


def test_genuine_ticker_price_failure_is_not_converted_to_earnings_warning():
    source = UnavailableEarningsSource(Fake(frames(), fail={'LEAD'}))
    rec, armed = sc.close_scan(source, ['LEAD'], datetime(2026, 9, 29, 16, 10, tzinfo=ET))
    assert 'LEAD' in rec.skipped and not armed


@pytest.mark.parametrize('status', ['POLICY_EXPIRED', 'UNAVAILABLE', 'REFRESH_IN_PROGRESS', 'INCOMPLETE'])
def test_returned_refresh_outcome_is_reported_without_stopping_prices(tmp_path, monkeypatch, status, capsys):
    """Outside Claude #12: a returned (not raised) refresh outcome reaches the scan record."""
    from desk import earnings_refresh
    monkeypatch.setattr(earnings_refresh, 'refresh_configured', lambda *args: {'status': status})
    config = policy(tmp_path)
    monkeypatch.setenv('DESK_EARNINGS_POLICY', str(config))
    monkeypatch.setenv('DESK_EARNINGS_CACHE', str(tmp_path / 'earnings.sqlite'))
    source = Fake(frames())
    monkeypatch.setattr(WebullData, 'from_env', staticmethod(lambda: source))
    monkeypatch.setattr(sc, 'datetime', type('Clock', (datetime,), {
        'now': staticmethod(lambda tz=None: datetime(2026, 9, 29, 20, 11, tzinfo=timezone.utc))}))
    watchlist = tmp_path / 'watchlist.json'
    watchlist.write_text(json.dumps(['LEAD']))
    root = tmp_path / 'scan'
    assert sc.main(['--watchlist', str(watchlist), '--data-dir', str(root)]) == 0
    record = sc.ScanLog(root).records()[0]
    assert record['error'] is None and any(s['setup_id'] == BREAKOUT for s in record['armed'])
    assert record['discovery']['earnings_source']['status'] == status
    assert qualify(scanner_source(Fake({}), {'DESK_EARNINGS_POLICY': str(config),
                                            'DESK_EARNINGS_CACHE': str(tmp_path / 'earnings.sqlite')},
                                  refresh_source=None), sig(EP), now())['status'] == 'PENDING_EVIDENCE'


def test_ready_or_cached_refresh_adds_no_issue(monkeypatch):
    from desk import earnings_refresh
    for status in ('READY', 'CACHED'):
        monkeypatch.setattr(earnings_refresh, 'refresh_configured', lambda *args, s=status: {'status': s})
        source = scanner_source(Fake({}), {}, refresh_source=None)
        assert getattr(source, 'earnings_refresh_status', None) is None
