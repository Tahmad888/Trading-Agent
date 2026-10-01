"""Native pair policy, bounded share units, and diagnostic acceptance."""
from dataclasses import replace
from datetime import date, timedelta
import json

import pandas as pd
import pytest

from desk.action_ledger import ActionLedger
from desk.action_source import ActionBackedSource, PriceChannelReview, native_volume_basis
from desk.bars import BarDataError
from desk.data_basis import compatible_volume, volume_basis, VolumeBasis
from desk.indicators import daily_features, Settings
from tests.test_action_ledger import NOW, review, pair, basis, native_source
from tests.test_scanner import daily, UP


def channel(tf='D', **kw):
    return PriceChannelReview(source='fixture', evidence_ref='fictional prices',
        host='api.sandbox.webull.com', symbol='NVDA', timeframe=tf,
        normalization='split_dividend_adjusted' if tf=='D' else 'unadjusted',
        volume_policy='native_no_split_window', volume_evidence_ref='fictional source-pair review',
        **kw)


def frame_and_early(tmp_path, *, split_day=None, policy=True):
    store=ActionLedger(tmp_path/'a.sqlite')
    s,d=pair()
    if split_day: s=replace(s, rows=(replace(s.rows[0],event_date=split_day),))
    store.publish(review(),s,d,NOW)
    b=basis(store)
    f=daily(UP, end=date(2026,9,30))
    # Fixture identity is replaced consistently; no claim about real channel evidence.
    f.attrs['volume_basis']=native_volume_basis(channel(ep_volume_comparison=policy),b).model_dump(mode='json')
    f.attrs['provider_identity']={'symbol':'NVDA','instrument_id':b.security_id}
    early=native_volume_basis(channel('M15',ep_volume_comparison=policy),b).model_dump(mode='json')
    return f,early


def test_distinct_channels_compare_under_explicit_directional_policy(tmp_path):
    f,early=frame_and_early(tmp_path)
    assert f.attrs['volume_basis']['definition_id'] != early['definition_id']
    compatible_volume(f.iloc[-50:],early)
    assert volume_basis(f.iloc[-50:]).channel=='webull:native:D'


@pytest.mark.parametrize('fault',['missing_policy','wrong_channel','security','share_units','dates','wrong_definition'])
def test_mixed_comparison_does_not_accept_unrelated_profiles(tmp_path,fault):
    f,early=frame_and_early(tmp_path)
    if fault=='missing_policy': early['comparison_policy']=None
    if fault=='wrong_channel': early['channel']='another vendor'
    if fault=='security': early['security_id']='another instrument'
    if fault=='share_units': early['share_basis_id']='older shares'
    if fault=='dates': early['valid_through']='2026-10-02'
    if fault=='wrong_definition': early['definition_id']='unrelated'
    with pytest.raises(BarDataError): compatible_volume(f.iloc[-50:],early)


def test_recent_split_rejects_crossing_window_but_not_postsplit_rows(tmp_path):
    f,early=frame_and_early(tmp_path,split_day=date(2026,9,15))
    with pytest.raises(BarDataError,match='split share units'): compatible_volume(f.iloc[-50:],early)
    volume_basis(f.iloc[-5:])


def test_relative_volume_recovers_only_after_full_postsplit_window(tmp_path):
    f,_=frame_and_early(tmp_path,split_day=date(2026,9,15))
    settings=Settings(volume_avg_length=5)
    features=daily_features(f,settings)
    eligible=f.index.tz_convert('America/New_York').date >= date(2026,9,15)
    after=features[eligible]
    assert after.rel_volume.iloc[:4].isna().all()
    assert after.rel_volume.iloc[4:].notna().all()
    assert features[~eligible].rel_volume.isna().all()
    assert features.sma_200.iloc[-1]==pytest.approx(daily_features(f).sma_200.iloc[-1])


def test_historical_split_does_not_disable_recent_ep_window(tmp_path):
    f,early=frame_and_early(tmp_path,split_day=date(2026,1,2))
    with pytest.raises(BarDataError): volume_basis(f)
    compatible_volume(f.iloc[-50:],early)
    assert pd.notna(daily_features(f).rel_volume.iloc[-1])


def test_wrapper_attaches_volume_without_rewriting_values(tmp_path):
    store=ActionLedger(tmp_path/'a.sqlite'); store.publish(review(),*pair(),NOW)
    at=NOW+timedelta(seconds=1)
    native=native_source(at)
    before=native.bars(['NVDA'],category='US_STOCK',timespan='M15')['NVDA']
    wrapped=ActionBackedSource(native,store,(channel('M15',ep_volume_comparison=True),),
        host='api.sandbox.webull.com',clock=lambda:at)
    after=wrapped.bars(['NVDA'],category='US_STOCK',timespan='M15')['NVDA']
    pd.testing.assert_frame_equal(before,after)
    assert volume_basis(after).valid_from==date(2024,6,10)
    assert after.attrs['volume_basis']['definition_id']=='webull:minute:RTH:provider-reported'


def test_bounded_volume_profile_requires_identity_and_dates():
    base=dict(source='fixture',evidence_ref='fixture',channel='daily',definition_id='fixture',units='shares',share_basis_id='fixture')
    with pytest.raises(ValueError): VolumeBasis(**base,symbol='NVDA')
    with pytest.raises(ValueError): VolumeBasis(**base,comparison_policy='webull-rth30/native-daily50-v1')


def acceptance_source(tmp_path):
    from tests.test_action_ledger import wrapped
    f,early=frame_and_early(tmp_path)
    store=ActionLedger(tmp_path/'a.sqlite')
    at=NOW+timedelta(seconds=1)
    f.attrs['bar_provenance']['price_basis']=basis(store).model_dump(mode='json')
    f.attrs['bar_provenance']['adjustment']='split_dividend_adjusted'
    f.attrs['received_at']=at.isoformat()
    m=wrapped(store,at).bars(['NVDA'],category='US_STOCK',timespan='M15')['NVDA']
    m.attrs['volume_basis']=early
    class Source:
        def bars(self,symbols,*,timespan,**kw):
            return {'NVDA':f.copy() if timespan=='D' else m.copy()}
    return Source(),at


def test_acceptance_command_exercises_real_contracts_not_setup_triggers(tmp_path):
    from desk.data_acceptance import check
    source,at=acceptance_source(tmp_path)
    report=check(source,['NVDA'],clock_fn=lambda:at)
    assert report['status']=='PASS',report
    item=report['checks'][0]
    assert item['setup_eligibility']=='NOT_EVALUATED'
    assert item['ep_threshold']==0.5 and item['ep_first30_volume']==20000
    assert item['timing_scope']=='current regular session'
    assert len(item['stages'])==5


def test_acceptance_stops_on_fetch_error_without_exposing_transport_details():
    from desk.data_acceptance import check
    from desk.webull import WebullError
    class Source:
        count=0
        def bars(self,*a,**kw):
            self.count+=1
            raise WebullError('transport URL containing FAKE_SECRET')
    src=Source(); report=check(src,['NVDA','SPY'])
    assert src.count==1 and report['status']=='FAIL'
    assert 'FAKE_SECRET' not in json.dumps(report)


def test_native_pair_preserves_exact_ep_threshold(tmp_path):
    from tests.test_price_volume_basis import features
    from tests.test_triggers import EP, ctx
    from desk.playbook.cards import CARDS
    from desk.playbook.triggers import episodic_pivot
    f=features(EP)
    bounded,early=frame_and_early(tmp_path)
    # Give the known positive setup real dates within this fictional accepted window.
    f.index=pd.date_range(end='2026-09-30',periods=len(f),freq='B',tz='America/New_York')
    f.attrs['volume_basis']=bounded.attrs['volume_basis']
    threshold=0.5*f.volume.iloc[-50:].mean()
    card=CARDS['5_qullamaggie_episodic_pivot']
    assert episodic_pivot(f,card,ctx(today_open=57.5,early_volume=threshold,early_volume_basis=early)) is not None
    assert episodic_pivot(f,card,ctx(today_open=57.5,early_volume=threshold-1,early_volume_basis=early)) is None


def test_acceptance_after_close_uses_previous_50_days_not_test_day(tmp_path):
    from desk.data_acceptance import check
    from desk.calendar import session
    f,early=frame_and_early(tmp_path)
    store=ActionLedger(tmp_path/'a.sqlite')
    at=NOW.replace(hour=21)
    today=daily(UP,end=date(2026,10,1))
    today.attrs=f.attrs.copy()
    today.attrs['bar_provenance']={**today.attrs['bar_provenance'],
        'price_basis':basis(store,at).model_dump(mode='json'),'adjustment':'split_dividend_adjusted'}
    today.loc[today.index[-1],'volume']=100_000_000
    opened,closed=session(date(2026,10,1))
    idx=pd.date_range(opened,closed-timedelta(minutes=15),freq='15min').tz_convert('UTC')
    m=pd.DataFrame(dict(open=120.,high=122.,low=119.,close=121.,volume=10000.),index=idx)
    from tests.test_action_ledger import wrapped
    metadata=wrapped(store,at).bars(['NVDA'],category='US_STOCK',timespan='M15')['NVDA'].attrs
    m.attrs={**metadata,'volume_basis':early}
    class Source:
        def bars(self,symbols,*,timespan,**kw): return {'NVDA':today if timespan=='D' else m}
    report=check(Source(),['NVDA'],clock_fn=lambda:at)
    assert report['status']=='PASS',report
    item=report['checks'][0]
    assert item['timing_scope']=='completed historical session'
    assert item['ep_prior50_daily_average']==today.volume.iloc[-51:-1].mean()
    assert item['ep_prior50_daily_average']!=today.volume.iloc[-50:].mean()
