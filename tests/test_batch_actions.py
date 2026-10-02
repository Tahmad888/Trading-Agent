"""Deterministic provider-contract fixtures; not authenticated source acceptance."""
from datetime import timedelta
from decimal import Decimal
import json
from urllib import error

import pandas as pd
import pytest

from desk.action_source import configured_source
from desk.bars import BarDataError
from desk.batch_actions import BatchActions, BatchActionError, POLICY
from desk.data_basis import volume_basis, compatible_volume, price_basis
from desk.security import securities
from tests.test_vendor_basis import Native, NOW, HOST, source, daily, minutes

KEY = 'fixture-secret-never-log'


def split(symbol='LEAD', day='2026-09-01', **kw):
    return dict(id='split:'+symbol,ticker=symbol,execution_date=day,split_from=1,split_to=10,adjustment_type='forward_split',**kw)


def dividend(symbol='LEAD', amount=.375, **kw):
    return dict(id='div:'+symbol,ticker=symbol,ex_dividend_date=str(NOW.date()),cash_amount=amount,currency='USD',**kw)


class Feed:
    def __init__(self, splits=(), dividends=()):
        self.splits,self.dividends = list(splits),list(dividends)
        self.calls = []
        self.fail = None
        self.body = None

    def __call__(self, req):
        self.calls.append(req)
        if self.fail:
            raise self.fail
        if self.body is not None:
            return json.dumps(self.body).encode()
        rows = self.splits if '/splits?' in req.full_url else self.dividends
        return json.dumps({'status':'OK','results':rows}).encode()


def adapter(tmp_path,native,feed,policy=POLICY):
    return BatchActions(tmp_path/'batch.sqlite',KEY,clock_fn=lambda:native.now,transport=feed,volume_policy=policy)


def metadata(src, symbol='LEAD'):
    return securities(src,[symbol],{})[symbol]


def test_first_download_no_split_window_automatically_covers_both_native_channels(tmp_path):
    native=Native(); src=source(tmp_path,native); feed=Feed()
    src.actions=adapter(tmp_path,native,feed)
    d=daily(src); m=minutes(src)
    assert volume_basis(d).definition_id == 'webull:native:D:provider-reported'
    assert volume_basis(m).definition_id == 'webull:minute:RTH:provider-reported'
    compatible_volume(d,volume_basis(m).model_dump())
    assert len(feed.calls) == 1  # one all-market split request, not two per ticker
    assert 'ticker=' not in feed.calls[0].full_url
    assert KEY not in feed.calls[0].full_url
    assert feed.calls[0].get_header('Authorization') == 'Bearer '+KEY
    assert not src.last_volume_errors


def test_no_policy_still_returns_prices_without_volume_or_provider_calls(tmp_path):
    native=Native(); src=source(tmp_path,native); feed=Feed()
    src.actions=adapter(tmp_path,native,feed,policy=None)
    d=daily(src)
    with pytest.raises(BatchActionError,match='NOT_ACCEPTED'):
        src.actions.volume(metadata(src),'D')
    assert not feed.calls
    assert price_basis(d,NOW).method == 'webull-history-v1'
    with pytest.raises(BarDataError,match='Unverified volume'):
        volume_basis(d)


def test_split_day_bounds_and_no_guessing_old_volume(tmp_path):
    native=Native(); src=source(tmp_path,native); feed=Feed([split(day='2026-09-28')])
    src.actions=adapter(tmp_path,native,feed)
    d=daily(src)
    with pytest.raises(BarDataError,match='window crosses'):
        volume_basis(d)
    assert volume_basis(d.iloc[-1:]).valid_from.isoformat() == '2026-09-28'
    # Original provider volumes untouched.
    assert d.volume.tolist() == [10000]*3


def test_stock_dividend_classification_also_changes_share_units(tmp_path):
    native=Native(); src=source(tmp_path,native)
    row=split(day='2026-09-28'); row['adjustment_type']='stock_dividend'
    actions=adapter(tmp_path,native,Feed([row]))
    assert str(actions.volume(metadata(src),'D').valid_from) == '2026-09-28'


def test_longer_than_two_year_window_is_unavailable(tmp_path):
    native=Native(); src=source(tmp_path,native); actions=adapter(tmp_path,native,Feed())
    basis=actions.volume(metadata(src),'D')
    f=daily(src); f.index=pd.date_range('2023-01-01',periods=3,tz='UTC')
    f.attrs['volume_basis']=basis.model_dump(mode='json')
    with pytest.raises(BarDataError,match='window crosses'):
        volume_basis(f)


def test_daily_cache_restart_and_next_date_renewal(tmp_path):
    native=Native(); src=source(tmp_path,native); feed=Feed(); actions=adapter(tmp_path,native,feed)
    actions.volume(metadata(src),'D')
    adapter(tmp_path,native,feed).volume(metadata(src),'M15')
    assert len(feed.calls)==1
    native.now+=timedelta(days=1)
    adapter(tmp_path,native,feed).volume(metadata(src),'D')
    assert len(feed.calls)==2


def test_revision_forces_fresh_split_evidence_without_daily_manual_review(tmp_path):
    native=Native(); src=source(tmp_path,native); feed=Feed(); src.actions=adapter(tmp_path,native,feed)
    daily(src)
    native.now+=timedelta(minutes=15); native.factor['LEAD']=.99
    daily(src)
    assert len(feed.calls)==2


def test_bad_attributable_action_does_not_disable_healthy_ticker(tmp_path):
    native=Native(); src=source(tmp_path,native); row=split('BAD'); row['split_to']=0
    actions=adapter(tmp_path,native,Feed([row]))
    with pytest.raises(BatchActionError,match='UNSUPPORTED_SPLIT'):
        actions.volume(metadata(src,'BAD'),'D')
    assert actions.volume(metadata(src),'D').symbol=='LEAD'


@pytest.mark.parametrize('body',[{'status':'ERROR','results':[]},{'status':'OK','results':[{}]}, {'status':'OK','results':[],'note':KEY}])
def test_error_bodies_and_unattributable_rows_never_become_empty_coverage(tmp_path,body):
    native=Native(); src=source(tmp_path,native); feed=Feed(); feed.body=body
    with pytest.raises(BatchActionError) as exc:
        adapter(tmp_path,native,feed).volume(metadata(src),'D')
    assert KEY not in str(exc.value)
    assert KEY not in (tmp_path/'batch.sqlite').read_bytes().decode('latin1')


def test_all_pages_required_and_failed_refresh_never_reuses_old_success(tmp_path):
    native=Native(); src=source(tmp_path,native); feed=Feed()
    actions=adapter(tmp_path,native,feed); actions.volume(metadata(src),'D')
    native.now+=timedelta(minutes=1)
    feed.body={'status':'OK','results':[],'next_url':'https://attacker.invalid/stocks/v1/splits'}
    with pytest.raises(BatchActionError,match='UNSAFE_PAGINATION'):
        actions.volume(metadata(src),'D',after=native.now)
    feed.fail=OSError('secret='+KEY)
    with pytest.raises(BatchActionError,match='TRANSPORT_FAILURE'):
        actions.volume(metadata(src),'D')
    assert len(feed.calls)==3


def test_complete_pagination_and_stable_ids(tmp_path):
    native=Native(); src=source(tmp_path,native); seen=[]
    def transport(req):
        seen.append(req.full_url)
        if len(seen)==1:
            return json.dumps({'status':'OK','results':[split('OTHER')], 'next_url':'https://api.massive.com/stocks/v1/splits?cursor=second&apiKey=evil'}).encode()
        return json.dumps({'status':'OK','results':[split('LEAD')]}).encode()
    actions=BatchActions(tmp_path/'batch.sqlite',KEY,clock_fn=lambda:native.now,transport=transport,volume_policy=POLICY)
    assert str(actions.volume(metadata(src),'D').valid_from)=='2026-09-01'
    assert len(seen)==2 and all('apiKey=' not in u for u in seen)


def test_request_budget_is_shared_and_persistent_across_restart(tmp_path):
    native=Native(); feed=Feed()
    for _ in range(5):
        native.now+=timedelta(seconds=1)
        adapter(tmp_path,native,feed).snapshot('splits',after=native.now)
    native.now+=timedelta(seconds=1)
    with pytest.raises(BatchActionError,match='BUDGET'):
        adapter(tmp_path,native,feed).snapshot('splits',after=native.now)
    assert len(feed.calls)==5
    native.now+=timedelta(seconds=61)
    adapter(tmp_path,native,feed).snapshot('splits')
    assert len(feed.calls)==6


def test_http_rate_limit_latches_without_retries_or_secret_output(tmp_path):
    native=Native(); feed=Feed(); feed.fail=error.HTTPError('https://x?key='+KEY,429,KEY,{},None)
    for _ in range(2):
        with pytest.raises(BatchActionError,match='RATE_LIMITED') as exc:
            adapter(tmp_path,native,feed).snapshot('splits')
        assert KEY not in str(exc.value)
    assert len(feed.calls)==1


def test_malformed_optional_config_keeps_vendor_price_source_alive(tmp_path):
    native=Native(); src=configured_source(native,{'WEBULL_HOST':HOST,'DESK_VENDOR_BASIS_DB':str(tmp_path/'vendor.sqlite'),
        'DESK_AUTO_ACTION_DB':str(tmp_path/'auto.sqlite')},clock=lambda:native.now)
    assert src.auto_action_issue
    assert daily(src).close.iloc[-1]==1000


def test_ordinary_cash_dividend_explains_anchor_without_adjusting_bars(tmp_path):
    native=Native(); src=source(tmp_path,native)
    # Prior raw close 1000; refreshed adjusted daily close 999.625.
    native.factor['LEAD']=.999625
    original=native.bars
    def bars(*args,**kwargs):
        frames=original(*args,**kwargs)
        if kwargs['timespan']=='M15' and 'start_time' in kwargs:
            for frame in frames.values():
                frame.loc[:,['open','high','low','close']]/=.999625
        return frames
    native.bars=bars
    src.actions=adapter(tmp_path,native,Feed(dividends=[dividend()]))
    d=daily(src); basis=price_basis(d,NOW)
    assert basis.anchor_adjustment.value==Decimal('.375')
    assert basis.daily_anchor_close==999.625 and basis.raw_anchor_close==1000
    assert d.volume.iloc[-1]==10000
    assert len(src.actions.snapshot('dividends')['rows'])==1


@pytest.mark.parametrize('variant',['missing','wrong_amount','wrong_date','wrong_currency','combined'])
def test_unexplained_anchor_stays_unavailable_per_ticker(tmp_path,variant):
    native=Native(); src=source(tmp_path,native); native.bad['LEAD']='mismatch'
    row=dividend(amount=10)
    if variant=='wrong_amount': row['cash_amount']=.375
    if variant=='wrong_date': row['ex_dividend_date']='2026-09-28'
    if variant=='wrong_currency': row['currency']='CAD'
    dividends=[] if variant=='missing' else [row]
    splits=[split(day=str(NOW.date()))] if variant=='combined' else []
    src.actions=adapter(tmp_path,native,Feed(splits,dividends))
    frames=src.bars(['LEAD','OTHER'],timespan='D',category='US_STOCK')
    assert set(frames)=={'OTHER'} and 'DAILY_RAW_CLOSE_MISMATCH' in src.last_errors['LEAD']


def test_split_only_anchor_with_post_split_volume_interval(tmp_path):
    native=Native(); src=source(tmp_path,native); native.factor['LEAD']=.1
    original=native.bars
    def bars(*args,**kwargs):
        frames=original(*args,**kwargs)
        if kwargs['timespan']=='M15' and 'start_time' in kwargs:
            for frame in frames.values(): frame.loc[:,['open','high','low','close']]*=10
        return frames
    native.bars=bars
    src.actions=adapter(tmp_path,native,Feed([split(day=str(NOW.date()))]))
    d=daily(src); basis=price_basis(d,NOW)
    assert basis.anchor_adjustment.kind=='split' and basis.raw_anchor_close==1000 and basis.daily_anchor_close==100
    with pytest.raises(BarDataError,match='window crosses'):
        volume_basis(d)
    assert volume_basis(minutes(src)).valid_from==NOW.date()


def test_late_provider_action_arrival_refreshes_shared_anchor_feed(tmp_path):
    native=Native(); src=source(tmp_path,native); feed=Feed(); actions=adapter(tmp_path,native,feed)
    with pytest.raises(BatchActionError,match='MISSING_OR_AMBIGUOUS'):
        actions.anchor(metadata(src),NOW.date()-timedelta(days=1))
    assert len(feed.calls)==2
    feed.dividends=[dividend()]
    native.now+=timedelta(minutes=16)
    assert actions.anchor(metadata(src),NOW.date()-timedelta(days=1)).value==Decimal('.375')
    assert len(feed.calls)==4
    actions.anchor(metadata(src),NOW.date()-timedelta(days=1))
    assert len(feed.calls)==4


def test_volume_probe_requires_pair_and_cannot_confuse_price_pass_with_volume_pass(tmp_path):
    from desk.vendor_check import check
    from tests.test_revision_rebuild import RevisedCharts
    native=RevisedCharts(); src=source(tmp_path,native)
    result=check(src,['LEAD'],clock_fn=lambda:native.now,require_volume=True)
    assert result['checks'][0]['status']=='PASS'
    assert result['checks'][0]['volume_pair']=='UNAVAILABLE'
    assert result['status']=='INCOMPLETE'
    src.actions=adapter(tmp_path,native,Feed())
    result=check(src,['LEAD'],clock_fn=lambda:native.now,require_volume=True)
    assert result['checks'][0]['volume_pair']=='PASS' and result['status']=='PASS'


def test_pagination_loop_does_not_claim_complete_absence(tmp_path):
    native=Native(); feed=Feed(); feed.body={'status':'OK','results':[], 'next_url':'https://api.massive.com/stocks/v1/splits?cursor=one'}
    with pytest.raises(BatchActionError,match='PAGINATION_LOOP'):
        adapter(tmp_path,native,feed).snapshot('splits')
    assert len(feed.calls)==2


def test_final_page_transport_failure_discards_the_incomplete_batch(tmp_path):
    native=Native(); calls=[]
    def transport(req):
        calls.append(req.full_url)
        if len(calls)>1: raise OSError('secret '+KEY)
        return json.dumps({'status':'OK','results':[split()], 'next_url':'https://api.massive.com/stocks/v1/splits?cursor=two'}).encode()
    actions=BatchActions(tmp_path/'batch.sqlite',KEY,clock_fn=lambda:native.now,transport=transport,volume_policy=POLICY)
    with pytest.raises(BatchActionError,match='TRANSPORT_FAILURE'):
        actions.snapshot('splits')
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(tmp_path/'batch.sqlite')) as db:
        assert db.execute('SELECT status,payload FROM feeds').fetchone()==('UNAVAILABLE',None)


def test_fifty_tickers_share_one_split_request_without_manual_files(tmp_path):
    native=Native(); src=source(tmp_path,native); feed=Feed()
    src.actions=adapter(tmp_path,native,feed)
    symbols=['NAME'+str(i) for i in range(50)]
    frames=src.bars(symbols,timespan='D',category='US_STOCK')
    assert set(frames)==set(symbols)
    assert len(feed.calls)==1
    assert all(volume_basis(f).symbol==s for s,f in frames.items())


def test_broken_optional_cache_does_not_disable_price_only_frames(tmp_path):
    import sqlite3
    from contextlib import closing
    native=Native(); src=source(tmp_path,native)
    src.actions=adapter(tmp_path,native,Feed())
    with closing(sqlite3.connect(tmp_path/'batch.sqlite')) as db, db:
        db.execute('DROP TABLE feeds')
    assert daily(src).close.iloc[-1]==1000
    assert src.last_volume_errors['LEAD']=='Automatic action evidence store unavailable'
    native.bad['LEAD']='mismatch'
    frames=src.bars(['LEAD','OTHER'],timespan='D',category='US_STOCK')
    assert set(frames)=={'OTHER'} and 'DAILY_RAW_CLOSE_MISMATCH' in src.last_errors['LEAD']


def test_malformed_pagination_url_is_safe_adapter_error(tmp_path):
    native=Native(); feed=Feed(); feed.body={'status':'OK','results':[],'next_url':'https://[bad'}
    with pytest.raises(BatchActionError,match='UNSAFE_PAGINATION'):
        adapter(tmp_path,native,feed).snapshot('splits')
