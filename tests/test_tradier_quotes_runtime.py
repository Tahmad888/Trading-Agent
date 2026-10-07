"""Durable quote evidence and real synthetic scanner/ticket pipeline. Zero network."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import threading

import pytest

from desk.tradier_quotes import QuoteStore, ReviewedMapping, TradierSide, SOURCE, requested
from desk.tradier_option_check import TradierClient
from desk.tradier_risk import compose
from desk.quote_mapping import WebullSide
from desk.tastytrade_quotes import QuoteUnavailable
from desk.tickets import TicketStore, TicketError, recheck
from tests.quote_support import QuoteDesk, record
from tests.ticket_support import approve

AT = datetime(2026, 10, 7, 18, 34, tzinfo=timezone.utc)


def raw(symbol="LEAD", at=AT, price="50", **changes):
    ms = int((at - timedelta(seconds=1)).timestamp() * 1000)
    row = dict(symbol=symbol, type="stock", description="Synthetic LEAD Inc", bid=str(Decimal(price)-Decimal('.01')),
               ask=str(Decimal(price)+Decimal('.01')), last=price,
               bid_date=ms, ask_date=ms, trade_date=ms, bidsize=999999, asksize=1)
    row.update(changes)
    return row


def mapping(symbol="LEAD", environment="production"):
    w = record(symbol).webull.model_copy(update={"exchange_code":"TEST"})
    return ReviewedMapping(symbol=symbol, webull=w,
        tradier=TradierSide(environment=environment, symbol=symbol, type="stock", description="Synthetic LEAD Inc"),
        reviewed_by="fixture:reviewer", reviewed_at=AT, webull_capture_sha256="fixture", tradier_capture_sha256="fixture")


def client(rows, at=AT, error=None):
    def request(method, url, headers, data):
        assert method == "GET" and '/v1/markets/quotes?' in url and 'greeks=false' in url
        if error:
            raise QuoteUnavailable(error)
        return {"quotes": {"quote": rows}}
    return TradierClient("fixture-token", request=request, clock=lambda: at)


def fetch(store, rows=None, at=AT):
    store.fetch(client(rows or [raw(at=at)], at), [requested("LEAD", "stock")])


def read(store, component="trade", at=AT, symbol="LEAD", environment="production"):
    with store.held() as db:
        return store.read(db, environment, symbol, component, at)


def test_healthy_refresh_reopen_stable_generation(tmp_path):
    store = QuoteStore(tmp_path/'q.sqlite')
    fetch(store)
    a = read(store)[1]
    reopened = QuoteStore(store.path)
    fetch(reopened, at=AT+timedelta(seconds=3))
    assert read(reopened, at=AT+timedelta(seconds=3))[1] == a
    with reopened.held() as db:
        with pytest.raises(Exception, match='append-only'):
            db.execute('DELETE FROM events')


@pytest.mark.parametrize('field,value', [('bid_date',0),('ask_date',None),('trade_date',True),
    ('bid',True),('last',0),('ask','NaN'),('bid','100'),
    ('bid_date',int((AT+timedelta(seconds=1)).timestamp()*1000)),
    ('ask_date',int((AT-timedelta(seconds=61)).timestamp()*1000))])
def test_bad_fields_do_not_revive_previous_capture(tmp_path, field, value):
    store=QuoteStore(tmp_path/'q.sqlite'); fetch(store)
    before=read(store)[1]
    fetch(store,[raw(**{field:value})])
    component='trade' if field in {'last','trade_date'} else 'quote'
    with pytest.raises(QuoteUnavailable): read(store,component)
    fetch(store)
    assert read(store,component)[1] != before


def test_components_and_tickers_isolated(tmp_path):
    store=QuoteStore(tmp_path/'q.sqlite')
    store.fetch(client([raw(bid_date=0),raw('GOOD')]),[requested('LEAD','stock'),requested('GOOD','stock')])
    assert read(store,'trade')
    assert read(store,'quote',symbol='GOOD')
    with pytest.raises(QuoteUnavailable): read(store,'quote')


def test_unfinished_and_late_healthy_result_cannot_clear_failure(tmp_path):
    store=QuoteStore(tmp_path/'q.sqlite'); names=[requested('LEAD','stock')]; fetch(store)
    older=store.begin('production',names)
    newer=store.begin('production',names)
    with pytest.raises(QuoteUnavailable): read(store)
    store.finish('production',names,newer,[raw(last=0)],AT)
    store.finish('production',names,older,[raw()],AT)
    with pytest.raises(QuoteUnavailable): read(QuoteStore(store.path))
    fetch(store); assert read(store)


def test_late_failure_blocks_newer_success_until_later_started_request(tmp_path):
    store=QuoteStore(tmp_path/'q.sqlite'); names=[requested('LEAD','stock')]
    older=store.begin('production',names); newer=store.begin('production',names)
    store.finish('production',names,newer,[raw()],AT); assert read(store)
    store.finish('production',names,older,[raw(last=0)],AT)
    with pytest.raises(QuoteUnavailable): read(store)
    fetch(store); assert read(store)


@pytest.mark.parametrize('error',['REST_HTTP_401','REST_HTTP_403','REST_HTTP_429','REST_TRANSPORT_FAILURE','unsafe secret text'])
def test_stop_restart_resume_requires_new_success(tmp_path,error):
    store=QuoteStore(tmp_path/'q.sqlite');fetch(store);before=read(store)[1]
    failed=client([],error=error)
    with pytest.raises(QuoteUnavailable):store.fetch(failed,[requested('LEAD','stock')])
    reopened=QuoteStore(store.path)
    with pytest.raises(QuoteUnavailable):read(reopened)
    blocked=client([raw()])
    with pytest.raises(QuoteUnavailable):store.fetch(blocked,[requested('LEAD','stock')])
    assert blocked.requests==0
    reopened.resume('production')
    with pytest.raises(QuoteUnavailable):read(reopened)
    fetch(reopened);assert read(reopened)[1]!=before
    with reopened.held() as db:
        assert 'unsafe secret text' not in str([tuple(r) for r in db.execute('SELECT * FROM events')])


def test_partials_duplicates_and_environment(tmp_path):
    store=QuoteStore(tmp_path/'q.sqlite');fetch(store)
    store.fetch(client([raw(),raw()]),[requested('LEAD','stock')])
    with pytest.raises(QuoteUnavailable):read(store)
    store.fetch(client([]),[requested('LEAD','stock')])
    with pytest.raises(QuoteUnavailable):read(store)
    fetch(store)
    with pytest.raises(QuoteUnavailable):read(store,environment='sandbox')


class Desk:
    def __init__(self,tmp_path):
        self.fixture=QuoteDesk(tmp_path)
        self.store=QuoteStore(tmp_path/'tradier.sqlite')
        self.store.mapping(mapping())
        self.rows=[raw(at=self.fixture.at,price=str(self.fixture.limit))]
        self.client=client(self.rows,self.fixture.at)
        self.inputs=compose(self.fixture.inputs(clock=lambda:self.fixture.at),price_source=self.fixture.desk.src,
                            log=self.fixture.desk.log,store=self.store,client=self.client)
        self.request=self.fixture.request(quote_source=SOURCE)
        self.tickets=TicketStore(tmp_path/'tickets.sqlite')

    def prepare(self):
        return self.tickets.prepare(self.request,self.inputs,now=self.fixture.at)

    def check(self):
        return recheck('x',1,self.request,self.inputs,self.fixture.at)


def test_real_signal_prepare_approve_consume_after_store_reopen(tmp_path):
    d=Desk(tmp_path);tid,ver=d.prepare()
    view=d.tickets.get(tid,ver,now=d.fixture.at)
    assert view['state']=='pending', view
    store=QuoteStore(d.store.path)
    adapters=compose(d.fixture.inputs(clock=lambda:d.fixture.at),price_source=d.fixture.desk.src,
                     log=d.fixture.desk.log,store=store,client=client(d.rows,d.fixture.at))
    approval=approve(d.tickets,tid,ver,adapters,now=d.fixture.at)
    assert approval
    # Existing helper documents exact consumption API.
    result=d.tickets.consume(tid,ver,inputs=adapters,request_id='fixture-once',now=d.fixture.at)
    assert result
    with pytest.raises(TicketError):
        d.tickets.consume(tid,ver,inputs=adapters,request_id='fixture-twice',now=d.fixture.at)


@pytest.mark.parametrize('change',['missing','revoked','wrong_id','wrong_type','description'])
def test_mapping_failure_does_not_mutate_signal(tmp_path,change):
    d=Desk(tmp_path); before=d.fixture.event()
    if change in {'missing','revoked'}:d.store.revoke('production','LEAD')
    elif change=='wrong_id':
        m=mapping(); d.store.mapping(m.model_copy(update={'webull':m.webull.model_copy(update={'instrument_id':'other'})}))
    elif change=='wrong_type':d.rows[0]['type']='etf'
    else:d.rows[0]['description']='Different issuer'
    check=d.check();assert check.problems
    assert d.fixture.event()==before


def test_fake_provider_label_is_refused(tmp_path):
    d=Desk(tmp_path)
    check=recheck('x',1,d.request,d.fixture.inputs(),d.fixture.at)
    assert check.problems


def test_failure_and_recovery_requires_new_approval(tmp_path):
    d=Desk(tmp_path);tid,ver=d.prepare()
    approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)
    d.rows[0]['last']=0;d.check()
    d.rows[0]['last']=str(d.fixture.limit)
    with pytest.raises(TicketError):approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)


def test_writer_waits_for_held_quote_fence(tmp_path):
    store=QuoteStore(tmp_path/'q.sqlite');fetch(store)
    began=threading.Event();done=threading.Event();errors=[]
    def writer():
        began.set()
        try:store.revoke('production','LEAD')
        except Exception as exc:errors.append(exc)
        done.set()
    with store.held() as db:
        t=threading.Thread(target=writer);t.start();assert began.wait(1)
        assert not done.wait(.05)
        assert store.read(db,'production','LEAD','trade',AT)
    t.join(2);assert done.is_set() and not errors


def option_desk(tmp_path):
    from desk.instruments import ContractBook
    from desk.tickets import TicketLeg
    from tests.risk_support import contract
    from tests.ticket_support import observer
    d=Desk(tmp_path)
    c=contract('LEAD','2026-11-20','call',50)
    ms=int((d.fixture.at-timedelta(seconds=1)).timestamp()*1000)
    d.option=raw(c.symbol,d.fixture.at,'1.50',type='option',underlying='LEAD',root_symbol='LEAD',
                 expiration_date='2026-11-20',option_type='call',strike=50,contract_size=100,
                 bid='1.49',ask='1.51',bidsize=999999,asksize=1)
    d.rows.append(d.option)
    d.book=ContractBook(source='independently registered fixture',as_of=d.fixture.at,contracts=(c,))
    d.request=d.request.model_copy(update={'structure':'long_call','sizing_mode':'selected_quantity',
        'legs':(TicketLeg(symbol=c.symbol,limit_price=Decimal('1.50'),qty=3),),'est_costs_usd':Decimal('3')})
    d.inputs=replace(d.inputs,contract_book=lambda at:d.book,
        observe=observer(quote_as_of=d.fixture.at,open_interest={c.symbol:4000}))
    return d


def test_option_quantity_multiplier_and_max_loss_not_advertised_size(tmp_path):
    d=option_desk(tmp_path);check=d.check()
    assert not check.problems,check.problems
    assert check.decision.computed_max_loss_usd==450
    assert check.decision.net_premium_usd==450
    assert check.decision.final_leg_quantities==[3]
    assert check.proposal.legs[0].quantity_unit=='contract'
    assert check.proposal.option_spread_pct_mid==pytest.approx(.02/1.50)
    tid,ver=d.prepare();approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)
    assert d.tickets.consume(tid,ver,inputs=d.inputs,request_id='once',now=d.fixture.at)['order_submitted'] is False


@pytest.mark.parametrize('field,value',[('contract_size',None),('contract_size',1),('contract_size',True),
    ('underlying','OTHER'),('root_symbol','LEAD1'),('expiration_date','2026-11-21'),
    ('option_type','put'),('strike',51),('symbol','LEAD261120C00051000'),('type','stock'),('bid_date',0)])
def test_wrong_option_identity_and_time_refuses(tmp_path,field,value):
    d=option_desk(tmp_path);d.option[field]=value
    assert d.check().problems


def test_old_approval_cannot_revive_after_stock_identity_restored(tmp_path):
    d=Desk(tmp_path);tid,ver=d.prepare();approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)
    d.rows[0]['description']='wrong issuer';assert d.check().problems
    d.rows[0]['description']='Synthetic LEAD Inc';assert not d.check().problems
    with pytest.raises(TicketError,match='changed|differ'):
        d.tickets.consume(tid,ver,inputs=d.inputs,request_id='once',now=d.fixture.at)


@pytest.mark.parametrize('race',['trade_failure','option_failure','option_spread','mapping_revoke','identity_failure'])
def test_final_reread_refuses_race_after_preliminary_check(tmp_path,race):
    d=option_desk(tmp_path);tid,ver=d.prepare()
    original=d.inputs.terms_source.held_event
    @contextmanager
    def racing(event_id):
        if race=='mapping_revoke':d.store.revoke('production','LEAD')
        else:
            names=[requested('LEAD','stock'),requested(d.option['symbol'],'option')]
            starts=d.store.begin('production',names)
            rows=json.loads(json.dumps(d.rows))
            if race=='trade_failure':rows[0]['last']=0
            elif race=='option_failure':rows[1]['ask_date']=0
            elif race=='identity_failure':rows[0]['description']='other issuer'
            else:rows[1].update(bid='1.00',ask='2.00')
            d.store.finish('production',names,starts,rows,d.fixture.at)
        with original(event_id) as final:yield final
    d.inputs.terms_source.held_event=racing
    with pytest.raises(TicketError,match='Final|eligible|Quote|quote'):
        approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)
    assert d.tickets.get(tid,ver,now=d.fixture.at)['state']=='pending'


def test_refresh_not_allowed_inside_quote_fence(tmp_path):
    d=Desk(tmp_path)
    with d.store.held():
        with pytest.raises(QuoteUnavailable,match='NETWORK_UNDER'):
            d.store.fetch(d.client,[requested('LEAD','stock')])
    assert d.client.requests==0


def test_missing_ticket_quote_hooks_cannot_fall_back(tmp_path):
    d=Desk(tmp_path);d.check()
    bare=replace(d.inputs,refresh_quotes=None,executable_quotes=None)
    assert 'TRADIER_EXECUTABLE_ADAPTER_REQUIRED' in str(recheck('x',1,d.request,bare,d.fixture.at).problems)


def test_missing_account_market_status_and_book_stay_blocking(tmp_path):
    from tests.ticket_support import observer
    d=option_desk(tmp_path)
    for inputs in (replace(d.inputs,contract_book=lambda at:None),
                   replace(d.inputs,market=lambda at:None),
                   replace(d.inputs,observe=observer(quote_as_of=d.fixture.at,security_tradable=None))):
        assert recheck('x',1,d.request,inputs,d.fixture.at).problems


def test_clock_resampled_after_get_receipt(tmp_path):
    d=Desk(tmp_path);later=d.fixture.at+timedelta(seconds=2)
    d.rows[:]=[raw(at=later,price=str(d.fixture.limit))]
    d.client.clock=lambda:later
    d.inputs=replace(d.inputs,clock=lambda:later)
    check=d.check()
    assert not check.problems,check.problems
    assert check.terms.checked_at==later


def test_runtime_provider_calls_only_before_quote_and_account_fences(tmp_path):
    d=Desk(tmp_path); seen=[]; original=d.client._request
    def request(*args):
        assert getattr(d.store.local,'db',None) is None
        seen.append(True)
        return original(*args)
    d.client._request=request
    tid,ver=d.prepare();approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)
    d.tickets.consume(tid,ver,inputs=d.inputs,request_id='once',now=d.fixture.at)
    assert len(seen)==3


def test_store_replacement_changes_generation(tmp_path):
    a=QuoteStore(tmp_path/'a.sqlite');b=QuoteStore(tmp_path/'b.sqlite');fetch(a);fetch(b)
    assert read(a)[1]!=read(b)[1]


def test_cached_ages_and_receipt_time_rechecked_at_use(tmp_path):
    store=QuoteStore(tmp_path/'q.sqlite');fetch(store)
    for at in (AT+timedelta(seconds=60),AT-timedelta(milliseconds=1)):
        with pytest.raises(QuoteUnavailable):read(store,'trade',at)
        with pytest.raises(QuoteUnavailable):read(store,'quote',at)


def test_numeric_overflow_never_reaches_signal_revalidation(tmp_path):
    d=Desk(tmp_path);before=d.fixture.event();d.rows[0]['last']='1e9999'
    assert d.check().problems
    assert d.fixture.event()==before


def test_standalone_signal_refresh_uses_tradier_before_revalidation(tmp_path):
    d=Desk(tmp_path)
    terms=d.inputs.terms_source.refresh_signal(d.fixture.event_id,d.fixture.at,d.client)
    assert terms.quote_provenance.source==SOURCE
    assert d.client.requests==1


def test_webull_identity_failure_survives_fresh_tradier(tmp_path):
    d=Desk(tmp_path);d.check()
    d.fixture.desk.native.ids['LEAD']='other-id'
    # Revalidation of vendor identity is outside the quote store and fails independently.
    try:d.fixture.desk.src.security_metadata(['LEAD'])
    except Exception:pass
    assert d.check().problems


def test_book_multiplier_cannot_be_taken_from_quote(tmp_path):
    d=option_desk(tmp_path)
    d.book=d.book.model_copy(update={'contracts':(d.book.contracts[0].model_copy(update={'multiplier':1}),)})
    assert d.check().problems


def test_good_quote_change_at_final_clock_does_not_churn_approval(tmp_path):
    d=option_desk(tmp_path);tid,ver=d.prepare();original=d.inputs.terms_source.held_event
    @contextmanager
    def racing(event_id):
        names=[requested('LEAD','stock'),requested(d.option['symbol'],'option')]
        starts=d.store.begin('production',names)
        rows=json.loads(json.dumps(d.rows));rows[1].update(bid='1.50',ask='1.52')
        d.store.finish('production',names,starts,rows,d.fixture.at)
        with original(event_id) as final:yield final
    d.inputs.terms_source.held_event=racing
    assert approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)


def test_review_and_resume_cli_require_human_confirmation(tmp_path):
    from io import StringIO
    from desk.tradier_quotes import main
    record_file=tmp_path/'mapping.json';record_file.write_text(mapping().model_dump_json())
    args=['--store',str(tmp_path/'q.sqlite'),'--environment','production','review','--record',str(record_file)]
    output=StringIO()
    assert main(args,stdin=StringIO('MAP LEAD\n'),stdout=output)==1
    assert not (tmp_path/'q.sqlite').exists()
    class Terminal(StringIO):
        def isatty(self):return True
    assert main(args,stdin=Terminal('WRONG\n'),stdout=StringIO())==1
    assert main(args,stdin=Terminal('MAP LEAD\n'),stdout=StringIO())==0
    args=args[:4]+['resume']
    assert main(args,stdin=StringIO('RESUME\n'),stdout=StringIO())==1
    assert main(args,stdin=Terminal('RESUME\n'),stdout=StringIO())==0


def test_cli_shape_and_existing_report_stop_before_provider(tmp_path,monkeypatch):
    from io import StringIO
    from desk.tradier_quotes import main
    out=tmp_path/'report.json';out.write_text('preserve')
    assert main(['--store',str(tmp_path/'q.sqlite'),'--environment','production','refresh',
                 '--symbols','LEAD','--output',str(out)],stdout=StringIO())==1
    assert out.read_text()=='preserve'
    assert not (tmp_path/'q.sqlite').exists()


def test_factory_missing_inputs_does_not_construct_fake_account(monkeypatch):
    from desk.tradier_risk import factory
    monkeypatch.delenv('DESK_TRADIER_BASE_FACTORY',raising=False)
    with pytest.raises(QuoteUnavailable,match='CONFIGURATION'):factory()


def test_real_json_decoder_decimal_prices_are_usable_and_preserved(tmp_path):
    from desk.tastytrade_transport import json_read
    row=raw()
    row.update(bid=49.99,ask=50.01,last=50.0)
    wire=json_read(json.dumps({'quotes':{'quote':[row]}}))
    assert isinstance(wire['quotes']['quote'][0]['bid'],Decimal)
    store=QuoteStore(tmp_path/'q.sqlite')
    fetch(store,wire['quotes']['quote'])
    data,_=read(store,'quote')
    assert data['view']['prices']['bid']['raw']=='49.99'
    assert read(store,'trade')


def test_reflected_secret_cannot_enter_capture_or_output(tmp_path,monkeypatch):
    token='synthetic-secret-should-not-be-stored'
    monkeypatch.setenv('TRADIER_ACCESS_TOKEN',token)
    store=QuoteStore(tmp_path/'q.sqlite')
    fetch(store,[raw(description=token)])
    with pytest.raises(QuoteUnavailable):read(store)
    with store.held() as db:
        assert token not in str([tuple(r) for r in db.execute('SELECT * FROM events')])


def test_cli_refresh_with_real_numeric_decoder(tmp_path,monkeypatch):
    from io import StringIO
    import desk.tradier_client as module
    from desk.tradier_quotes import main
    at=datetime.now(timezone.utc)
    feed=client([raw(at=at)],at)
    monkeypatch.setattr(module,'TradierClient',lambda *a,**k:feed)
    output=StringIO();target=tmp_path/'capture.json'
    assert main(['--store',str(tmp_path/'q.sqlite'),'--environment','production','refresh',
                 '--symbols','LEAD','--output',str(target)],stdout=output)==0
    assert json.loads(target.read_text())['checks']['LEAD']['quote']['status']=='FRESH_FIELDS'
    assert 'fixture-token' not in output.getvalue()


def test_unusable_greeks_do_not_disable_valid_option_quotes(tmp_path):
    d=option_desk(tmp_path);d.option['greeks']={'delta':object(),'updated_at':'broken'}
    check=d.check()
    assert not check.problems,check.problems


def test_provider_quote_and_book_ids_are_bound_separately(tmp_path):
    d=option_desk(tmp_path);first=d.check()
    c=d.book.contracts[0]
    d.book=d.book.model_copy(update={'contracts':(c.model_copy(update={'broker_contract_id':'different'}),)})
    second=d.check()
    assert first.binding['executable_quote_provenance']!=second.binding['executable_quote_provenance']


def test_explicit_stop_resume_invalidates_prepared_ticket(tmp_path):
    d=Desk(tmp_path);tid,ver=d.prepare()
    with pytest.raises(QuoteUnavailable):
        d.store.fetch(client([],d.fixture.at,error='REST_HTTP_429'),[requested('LEAD','stock')])
    d.store.resume('production')
    assert not d.check().problems
    with pytest.raises(TicketError,match='changed|differ'):
        approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)


def test_revoked_mapping_reinstallation_requires_new_ticket(tmp_path):
    d=Desk(tmp_path);tid,ver=d.prepare();d.store.revoke('production','LEAD');d.store.mapping(mapping())
    with pytest.raises(TicketError,match='changed|differ'):
        approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)


def test_quote_mapping_writer_waits_until_ticket_commit(tmp_path):
    d=Desk(tmp_path);tid,ver=d.prepare();original=d.inputs.terms_source.held_event
    started=threading.Event();done=threading.Event();errors=[];threads=[]
    def writer():
        started.set()
        try:d.store.revoke('production','LEAD')
        except Exception as exc:errors.append(exc)
        done.set()
    @contextmanager
    def guarded(event_id):
        with original(event_id) as status:
            def final(at):
                t=threading.Thread(target=writer);threads.append(t);t.start()
                assert started.wait(1) and not done.wait(.05)
                return status(at)
            yield final
    d.inputs.terms_source.held_event=guarded
    assert approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)
    for t in threads:t.join(2)
    assert done.is_set() and not errors
    assert d.tickets.get(tid,ver,now=d.fixture.at)['state']=='approved'
    with pytest.raises(TicketError):
        d.tickets.consume(tid,ver,inputs=d.inputs,request_id='cannot-reuse',now=d.fixture.at)


def test_separate_process_writer_is_fenced(tmp_path):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path
    store=QuoteStore(tmp_path/'q.sqlite');fetch(store)
    ready,done=tmp_path/'ready',tmp_path/'done'
    script="""
import sys
from pathlib import Path
from desk.tradier_quotes import QuoteStore
Path(sys.argv[2]).write_text('ready')
QuoteStore(sys.argv[1]).revoke('production','LEAD')
Path(sys.argv[3]).write_text('done')
"""
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    process=None
    try:
        with store.held():
            process=subprocess.Popen([sys.executable,'-c',script,str(store.path),str(ready),str(done)],
                                     env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            deadline=time.monotonic()+5
            while not ready.exists() and time.monotonic()<deadline:time.sleep(.005)
            assert ready.exists()
            assert not done.exists()
        _,stderr=process.communicate(timeout=10)
        assert process.returncode==0,stderr.decode()
        assert done.exists()
    finally:
        if process and process.poll() is None:
            process.kill();process.communicate()


def test_invalid_option_date_is_rejected_before_provider_request(tmp_path):
    d=option_desk(tmp_path)
    d.request=d.request.model_copy(update={'legs':(d.request.legs[0].model_copy(
        update={'symbol':'LEAD261332C00050000'}),)})
    assert d.check().problems
    assert d.client.requests==0


def test_unsupported_request_kind_is_rejected():
    with pytest.raises(QuoteUnavailable):requested('LEAD','anything')


@pytest.mark.parametrize('check',[None,True,1_000_000])
def test_unattributed_result_cannot_clear_failed_check(tmp_path,check):
    store=QuoteStore(tmp_path/'q.sqlite');fetch(store);fetch(store,[raw(last=0)])
    with pytest.raises(QuoteUnavailable,match='CHECK_ID'):
        store.finish('production',[requested('LEAD','stock')],{'LEAD':check},[raw()],AT)
    with pytest.raises(QuoteUnavailable):read(store)


def test_completed_or_other_symbol_check_id_cannot_supply_new_result(tmp_path):
    store=QuoteStore(tmp_path/'q.sqlite');names=[requested('LEAD','stock')]
    starts=store.begin('production',names);store.finish('production',names,starts,[raw()],AT)
    with pytest.raises(QuoteUnavailable,match='CHECK_ID'):
        store.finish('production',names,starts,[raw(price='100')],AT)
    other=store.begin('production',[requested('OTHER','stock')])
    with pytest.raises(QuoteUnavailable,match='CHECK_ID'):
        store.finish('production',names,{'LEAD':other['OTHER']},[raw(price='100')],AT)
    assert read(store)[0]['view']['prices']['last']['value']=='50'
