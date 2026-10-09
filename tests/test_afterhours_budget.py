"""Independent workload lifecycle and durable local limits; no provider calls."""
from datetime import timedelta
from dataclasses import replace
import pytest
from desk.tradier_client import TradierClient, RequestBudgetExceeded, LocalRateLimitExceeded
from desk.tradier_quotes import QuoteStore, requested
from desk.tastytrade_quotes import QuoteUnavailable
from tests.test_tradier_quotes_runtime import AT, raw, read, fetch, mapping


def operational(store, clock=lambda: AT, calls=None):
    calls = [] if calls is None else calls
    def reply(*args):
        calls.append(args)
        return {"quotes": {"quote": [raw(at=clock()), raw('GOOD', at=clock())]}}
    return TradierClient('fixture', operational=True, reserve_request=store.reserve_request,
                         request=reply, clock=clock)


def test_healthy_operational_refreshes_do_not_stop_at_twelve(tmp_path):
    store = QuoteStore(tmp_path/'q.sqlite'); c = operational(store)
    for _ in range(25):
        store.fetch(c, [requested('LEAD', 'stock')])
        assert read(store)
    assert c.requests == 25
    with store.held() as db:
        assert store.global_event(db, 'production') is None


@pytest.mark.parametrize('environment,limit', [('production',120),('sandbox',60)])
def test_rate_reservations_survive_reopen_and_exact_minute_boundary(tmp_path, environment, limit):
    store = QuoteStore(tmp_path/'q.sqlite')
    for _ in range(limit): store.reserve_request(environment, AT)
    reopened = QuoteStore(store.path)
    with pytest.raises(LocalRateLimitExceeded): reopened.reserve_request(environment, AT)
    reopened.reserve_request(environment, AT+timedelta(minutes=1))
    reopened.reserve_request('sandbox' if environment=='production' else 'production', AT)


def test_budget_failure_is_ticker_failure_not_provider_stop_and_changes_generation(tmp_path):
    store = QuoteStore(tmp_path/'q.sqlite'); c = operational(store)
    names = [requested('LEAD','stock'), requested('GOOD','stock')]
    store.fetch(c,names); lead_generation = read(store)[1]; good_generation = read(store,symbol='GOOD')[1]
    # Exhaust local reservations without pretending to send HTTP requests.
    for _ in range(119): store.reserve_request('production',AT)
    with pytest.raises(LocalRateLimitExceeded): store.fetch(c,[names[0]])
    assert c.requests == 1
    with pytest.raises(QuoteUnavailable): read(QuoteStore(store.path))
    assert read(store,symbol='GOOD')[1] == good_generation
    with store.held() as db: assert store.global_event(db,'production') is None
    later = AT+timedelta(minutes=1)
    store.fetch(operational(store,clock=lambda:later),[names[0]])
    assert read(store,at=later)[1] != lead_generation


def test_external_local_budget_failure_is_not_sent_or_provider_stop(tmp_path):
    store=QuoteStore(tmp_path/'q.sqlite');fetch(store)
    def no_send(*args):raise RequestBudgetExceeded()
    c=TradierClient('fixture',request=no_send,clock=lambda:AT)
    with pytest.raises(RequestBudgetExceeded):store.fetch(c,[requested('LEAD','stock')])
    assert c.accounting()['reserved_attempts']==1
    assert c.accounting()['confirmed_network_sends'] is None
    assert c.log[0]['network_send_status']=='NOT_ATTESTED' and 'sent_at' not in c.log[0]
    with pytest.raises(QuoteUnavailable):read(store)
    with store.held() as db:assert store.global_event(db,'production') is None
    fetch(store);assert read(store)


def test_operational_get_requires_workload_and_one_quote_only(tmp_path):
    c=operational(QuoteStore(tmp_path/'q.sqlite'))
    with pytest.raises(QuoteUnavailable,match='WORKLOAD'):c.get('quotes',{})
    with c.quote_workload():
        with pytest.raises(QuoteUnavailable,match='WORKLOAD'):c.get('chains',{})
        c.get('quotes',{})
        with pytest.raises(RequestBudgetExceeded):c.get('quotes',{})
    with c.quote_workload():c.get('quotes',{})
    assert c.requests==2


def test_clock_backwards_refusal_preserves_reservations(tmp_path):
    store=QuoteStore(tmp_path/'q.sqlite');store.reserve_request('production',AT)
    with pytest.raises(LocalRateLimitExceeded,match='CLOCK'):store.reserve_request('production',AT-timedelta(milliseconds=1))
    store.reserve_request('production',AT)


def test_factory_uses_explicit_operational_policy(tmp_path,monkeypatch):
    from types import ModuleType
    import sys
    from desk.tradier_risk import factory, Dependencies
    from tests.quote_support import QuoteDesk
    d=QuoteDesk(tmp_path/'desk');mod=ModuleType('fixture_tradier_base');base=d.inputs()
    mod.factory=lambda:Dependencies(base,d.desk.src,d.desk.log)
    monkeypatch.setitem(sys.modules,mod.__name__,mod)
    for key,value in {'DESK_TRADIER_BASE_FACTORY':mod.__name__+':factory',
        'DESK_TRADIER_ENVIRONMENT':'production','DESK_TRADIER_QUOTE_STORE':str(tmp_path/'q.sqlite'),
        'TRADIER_ACCESS_TOKEN':'fixture'}.items():monkeypatch.setenv(key,value)
    wrapped=factory()
    c=next(cell.cell_contents for cell in wrapped.refresh_quotes.__closure__ if isinstance(cell.cell_contents,TradierClient))
    assert c.operational and callable(c.reserve_request)
    assert wrapped.market is base.market and wrapped.contract_book is base.contract_book


def test_diagnostic_cap_remains_finite():
    c=TradierClient('fixture',request=lambda *args:{})
    for _ in range(12):c.get('quotes',{})
    with pytest.raises(RequestBudgetExceeded):c.get('quotes',{})
    assert c.requests==12


def test_real_synthetic_ticket_checks_continue_beyond_twelve(tmp_path):
    from tests.test_tradier_quotes_runtime import Desk
    d=Desk(tmp_path)
    for _ in range(20):
        result=d.check()
        assert not result.problems,result.problems
    assert d.client.operational and d.client.requests==20


def test_used_diagnostic_client_cannot_reset_budget_through_composition(tmp_path):
    c=TradierClient('fixture',request=lambda *args:{})
    c.get('quotes',{})
    with pytest.raises(QuoteUnavailable,match='CANNOT_CHANGE_POLICY'):
        c.use_operational_policy(QuoteStore(tmp_path/'q.sqlite').reserve_request)
    assert c.requests==1 and not c.operational


def test_operational_client_cannot_reset_rate_scope_after_dispatch(tmp_path):
    store=QuoteStore(tmp_path/'q.sqlite');c=operational(store)
    store.fetch(c,[requested('LEAD','stock')])
    c.use_operational_policy(store.reserve_request)
    with pytest.raises(QuoteUnavailable,match='RATE_SCOPE_CHANGED'):
        c.use_operational_policy(QuoteStore(tmp_path/'other.sqlite').reserve_request)


def test_local_failed_final_refresh_and_recovery_cannot_revive_approval(tmp_path):
    from tests.test_tradier_quotes_runtime import Desk
    from tests.ticket_support import approve
    from desk.tickets import TicketError
    d=Desk(tmp_path);tid,ver=d.prepare();approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)
    original=d.client._request
    def refused(*args):raise RequestBudgetExceeded()
    d.client._request=refused
    assert d.check().problems
    d.client._request=original
    assert not d.check().problems
    with pytest.raises(TicketError):approve(d.tickets,tid,ver,d.inputs,now=d.fixture.at)
    with d.store.held() as db:assert d.store.global_event(db,'production') is None


def test_parallel_clients_share_atomic_reservations(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path=tmp_path/'q.sqlite';store=QuoteStore(path)
    for _ in range(119):store.reserve_request('production',AT)
    def reserve(_):
        try:QuoteStore(path).reserve_request('production',AT);return True
        except LocalRateLimitExceeded:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sum(pool.map(reserve,range(2)))==1


def test_raw_greek_summary_is_explicitly_linked_to_reducer():
    from tests.test_quote_measure import recorded
    _,rec,_=recorded();report=rec.report()
    assert report['greeks']['label']=='RAW_OBSERVATION_SUMMARY'
    assert report['greeks']['reduced_state_path']=='measurements.greek_state'
    assert report['greeks']['arithmetic_eligibility']=='NONE'
    assert 'current_state' not in report['greeks']
    assert 'greek_state' in report
