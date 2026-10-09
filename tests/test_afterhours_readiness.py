"""Offline drafts and retained observations cannot grant identity/live acceptance."""
from copy import deepcopy
from datetime import timedelta
import pytest
from desk.tradier_readiness import prepare, configuration
from desk.tradier_quotes import ReviewedMapping
from desk.saved_greek_audit import audit
from tests.test_tradier_quotes_runtime import mapping
from tests.test_greek_dropped_start import recorder, send, row, CALL, AT


def captures():
    m=mapping()
    return {'identity_capture':[m.webull.model_dump()]}, {'environment':'production','rounds':[
        {'observations':{'LEAD':{'issuer_reference':m.tradier.model_dump()}}}]}


def test_crosswalk_is_complete_draft_but_never_an_approved_mapping():
    w,t=captures();r=prepare(w,t,['LEAD'],'a'*64,'b'*64)
    assert not r['issues'] and r['installed_mappings']==0
    draft=r['candidates']['LEAD'];assert draft['review_status']=='DRAFT_NOT_INSTALLABLE'
    with pytest.raises(ValueError):ReviewedMapping.model_validate(draft)


@pytest.mark.parametrize('damage',['description','classification','duplicate','missing_webull'])
def test_incomplete_or_conflicting_captures_never_yield_candidate(damage):
    w,t=captures()
    if damage=='description':del t['rounds'][0]['observations']['LEAD']['issuer_reference']['description']
    elif damage=='classification':t['rounds'][0]['observations']['LEAD']['issuer_reference']['type']='etf'
    elif damage=='duplicate':
        x=deepcopy(t['rounds'][0]);x['observations']['LEAD']['issuer_reference']['description']='different issuer';t['rounds'].append(x)
    else:w['identity_capture']=[]
    r=prepare(w,t,['LEAD'],'a'*64,'b'*64);assert r['issues']['LEAD'] and not r['candidates']


def test_repeated_identical_issuer_and_slash_share_class():
    w,t=captures();t['rounds'].append(deepcopy(t['rounds'][0]))
    assert prepare(w,t,['LEAD'],'a','b')['candidates']
    w['identity_capture'][0]['symbol']='BRK.B'
    t['rounds']=[{'observations':{'BRK.B':{'issuer_reference':{'symbol':'BRK/B','type':'stock','description':'Berkshire class B'}}}}]
    assert prepare(w,t,['BRK.B'],'a','b')['candidates']['BRK.B']['tradier']['symbol']=='BRK/B'


def test_configuration_reports_presence_not_secrets_or_factory_acceptance(tmp_path):
    r=configuration({'TRADIER_ACCESS_TOKEN':'never-output'})
    assert 'never-output' not in str(r) and r['status']=='CONFIGURATION_MISSING'
    full={'TRADIER_ACCESS_TOKEN':'never-output','DESK_TRADIER_BASE_FACTORY':'arbitrary:factory',
          'DESK_TRADIER_ENVIRONMENT':'production','DESK_TRADIER_QUOTE_STORE':str(tmp_path/'missing')}
    r=configuration(full)
    assert not r['factory_invoked'] and r['real_input_acceptance']=='NOT_ATTESTED'
    assert not (tmp_path/'missing').exists()


def test_readiness_report_can_be_saved_without_disabling_credential_guard(tmp_path):
    from desk.tradier_fields import write_report
    r=configuration({'TRADIER_ACCESS_TOKEN':'never-output'})
    out=write_report(r,tmp_path/'readiness.json')
    assert out['status']=='CONFIGURATION_MISSING'
    assert out['configured']['quote_credential']
    assert 'never-output' not in (tmp_path/'readiness.json').read_text()


def saved():
    s,rec=recorder();send(rec,row())
    return {'identity_capture':[i.capture() for i in s.identities.values()],'measurements':rec.report()}


def test_saved_greeks_retain_index_age_units_but_never_current_eligibility():
    r=audit(saved(),'a'*64);assert r['retained_greek_count']==1
    x=r['observations'][0];assert x['status']=='INDEXED_OBSERVATION_VALID'
    assert x['index']==row()['index']>2**53
    assert x['fields']['delta']['status']=='DOCUMENTED'
    assert x['snapshot_acceptance']=='NOT_ATTESTED' and r['live_state']=='NOT_RESTORED'


@pytest.mark.parametrize('bad',['index','identity','source_time'])
def test_saved_invalid_rows_are_never_repaired_by_receipt_or_ticker(bad):
    c=saved();r=c['measurements']['records'][0]
    if bad=='index':r['index']+=2**32
    elif bad=='source_time':r['source_time']['raw']=0
    else:c['identity_capture']=[]
    out=audit(c,'a'*64);assert out['observations'][0]['status']=='UNAVAILABLE'
    assert out['live_state']=='NOT_RESTORED'


def test_dropped_records_visible_and_raw_transaction_flags_preserved():
    c=saved();c['measurements']['records_dropped']={'GREEKS':3}
    c['measurements']['records'][0]['event_flags']=1
    out=audit(c,'a'*64)
    assert out['record_drops']=={'GREEKS':3} and out['observations'][0]['event_flags']==1
    assert out['observations'][0]['snapshot_acceptance']=='NOT_ATTESTED'


@pytest.mark.parametrize('conflict',[False,True])
def test_saved_identity_repeats_cannot_hide_contradiction(conflict):
    c=saved();other=deepcopy(next(i for i in c['identity_capture'] if i['desk_symbol']==CALL))
    if conflict:other['streamer_symbol']='.OTHER'
    c['identity_capture'].append(other)
    r=audit(c,'a'*64)
    assert r['observations'][0]['status']==('UNAVAILABLE' if conflict else 'INDEXED_OBSERVATION_VALID')
