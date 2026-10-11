"""Astra's 2026-10-10 re-audit regressions (test_astra_r2_r3_followup.py), copied with their
assertions unchanged (the supplied file is kept as-is in the handoff). Scratch
fixtures only, no provider calls."""
from dataclasses import replace
from copy import deepcopy
import pytest
from desk.tradier_quotes import QuoteStore,requested
from desk.tradier_risk import compose
from desk.tastytrade_quotes import QuoteUnavailable
from desk.tickets import TicketError
from tests.test_tradier_quotes_runtime import Desk,client
from tests.test_tradier_local_store import ShortTimeoutStore,blocked
from tests.ticket_support import approve

@pytest.mark.parametrize('operation',['approve','consume'])
def test_unpersisted_withdrawal_cannot_revive_old_ticket_after_restart(tmp_path,operation):
    d=Desk(tmp_path)
    tid,ver=d.prepare()
    if operation=='consume': assert approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)
    d.store.__class__=ShortTimeoutStore
    state={}
    def lock_during_reply(*args):
        state['lock']=blocked(d.store.path)
        state['lock'].__enter__()
        return {'quotes':{'quote':d.rows}}
    d.client._request=lock_during_reply
    with pytest.raises(QuoteUnavailable,match='LOCAL_QUOTE_STORE_BUSY'):
        d.store.fetch(d.client,[requested('LEAD','stock')])
    state.pop('lock').__exit__(None,None,None)
    # Simulate a process restart: abandon old process-held maps, use a new store and composer.
    restarted=QuoteStore(d.store.path)
    with restarted.held() as db:
        with pytest.raises(QuoteUnavailable,match='LATEST_CHECK_UNAVAILABLE'):
            restarted.read(db,'production','LEAD','quote',d.fixture.at)
    adapters=compose(d.fixture.inputs(clock=lambda:d.fixture.at),price_source=d.fixture.desk.src,
        log=d.fixture.desk.log,store=restarted,client=client(d.rows,d.fixture.at))
    # A healthy fresh check recovers the data; it must not rehabilitate the old ticket/approval.
    with pytest.raises(TicketError,match='changed|differ'):
        if operation=='approve': approve(d.tickets,tid,ver,adapters,now=d.fixture.at)
        else:d.tickets.consume(tid,ver,inputs=adapters,request_id='old-approval-after-restart',now=d.fixture.at)

def test_control_clean_restart_does_not_invalidate_healthy_ticket(tmp_path):
    d=Desk(tmp_path);tid,ver=d.prepare()
    restarted=QuoteStore(d.store.path)
    adapters=compose(d.fixture.inputs(clock=lambda:d.fixture.at),price_source=d.fixture.desk.src,
        log=d.fixture.desk.log,store=restarted,client=client(d.rows,d.fixture.at))
    assert approve(d.tickets,tid,ver,adapters,now=d.fixture.at)

@pytest.mark.parametrize('bad',[[],{}])
def test_saved_identity_nontext_symbol_gets_structured_refusal(bad):
    from desk.saved_greek_audit import audit
    from tests.test_afterhours_readiness import saved
    capture=deepcopy(saved());capture['identity_capture'][0]['desk_symbol']=bad
    result=audit(capture,'a'*64)
    assert result['status'] in {'CAPTURE_SHAPE_INVALID','PARTIAL_OBSERVATIONS'}
    assert result['provider_calls']==0 and result['live_state']=='NOT_RESTORED'
