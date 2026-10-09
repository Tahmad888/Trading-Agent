"""Tradier quote-only identity, price and epoch-ms fields. No Greek or volume analytics.

Runtime timestamps are documented bid_date/ask_date/trade_date, never Greek time,
receipt substitutes or advancement-based entitlement. Sizes stay unverified raw fields.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import os
import re

from desk.diagnostic_pair import occ_terms
from desk.quote_secrets import credential_free
from desk.risk import RiskLimits
from desk.tastytrade_quotes import QuoteUnavailable, aware, number

UTC = timezone.utc


def observe_row(row, wanted, received, unused=None):
    if not isinstance(row,dict) or row.get('symbol')!=wanted['wire']:
        raise QuoteUnavailable('IDENTITY_MISMATCH')
    kind=wanted['kind']
    if ((kind=='stock' and row.get('type') not in {'stock','etf'}) or
            (kind=='option' and row.get('type')!='option')):
        raise QuoteUnavailable('INSTRUMENT_TYPE_MISMATCH')
    view=dict(symbol=wanted['symbol'],wire_symbol=wanted['wire'],kind=kind,type=row['type'],
              received_at=aware(received).isoformat())
    if kind == 'stock':
        view['issuer_reference'] = {k: row.get(k) for k in ('symbol', 'type', 'description', 'exch')}
    if kind=='option':
        terms=occ_terms(wanted['symbol'])
        if (row.get('underlying')!=terms['underlying'] or row.get('expiration_date')!=terms['expiry']
                or row.get('option_type')!=terms['right'] or number(row.get('strike'))!=terms['strike']):
            raise QuoteUnavailable('OPTION_IDENTITY_MISMATCH')
        view['terms']={**terms,'strike':str(terms['strike']),'root_symbol':row.get('root_symbol')}
    prices={}
    for field in ('bid','ask','last'):
        try:
            value=number(row.get(field))
            if not math.isfinite(float(value)):
                raise QuoteUnavailable('PRICE_UNREPRESENTABLE')
            prices[field]=dict(raw=row.get(field),state='VALUE',value=str(value))
        except (ValueError,TypeError,ArithmeticError):
            prices[field]=dict(state='PRICE_INVALID')
    prices['book']='UNAVAILABLE'
    if prices['bid']['state']==prices['ask']['state']=='VALUE':
        bid,ask=(Decimal(prices[k]['value']) for k in ('bid','ask'))
        prices['book']='CROSSED' if bid>ask else 'LOCKED' if bid==ask else 'NORMAL'
    times={}
    for field in ('bid_date','ask_date','trade_date'):
        raw=row.get(field)
        try:
            value=int(raw) if isinstance(raw,str) and re.fullmatch(r'\d{1,16}',raw) else raw
            if type(value) is not int or value < 100_000_000_000:
                # Reject missing/zero/boolean/second-encoded times, never guess units.
                raise QuoteUnavailable('SOURCE_TIME_UNAVAILABLE')
            stamp=datetime(1970,1,1,tzinfo=UTC)+timedelta(milliseconds=value)
            times[field]=dict(raw=raw,utc=stamp.isoformat(),interpretation='EPOCH_MILLISECONDS_UTC',
                              receipt=dict(age_seconds=(aware(received)-stamp).total_seconds()))
        except (ValueError,TypeError,ArithmeticError):
            times[field]=dict(state='SOURCE_TIME_UNAVAILABLE')
    view.update(prices=prices,times=times,sizes={k:dict(raw=row.get(k),status='UNVERIFIED',arithmetic='EXCLUDED')
                                               for k in ('bidsize','asksize')})
    return view


def latest_policy_view(view, checked_at):
    limit=RiskLimits().max_quote_age.total_seconds()
    def field(price,time):
        p,t=view['prices'][price],view['times'][time]
        okay=p.get('state')=='VALUE' and Decimal(p['value'])>0 and 'utc' in t
        if okay:
            age=(aware(checked_at)-datetime.fromisoformat(t['utc'])).total_seconds()
            okay=0<=age<=limit and t['receipt']['age_seconds']>=0
        return dict(verdict='PASS' if okay else 'FAIL')
    bid,ask,trade=field('bid','bid_date'),field('ask','ask_date'),field('last','trade_date')
    return dict(quote=dict(verdict='PASS' if bid['verdict']==ask['verdict']=='PASS' and
                           view['prices']['book']=='NORMAL' else 'FAIL'),trade=trade)


def write_report(report,path):
    """No overwrite and no credential text. Diagnostic output, never eligibility."""
    text=json.dumps(report,indent=2)+'\n'
    if not credential_free(text):
        report=dict(status='REPORT_REJECTED',code='REPORT_CREDENTIAL_MATCH',purpose=report.get('purpose'))
        text=json.dumps(report,indent=2)+'\n'
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as handle:
        handle.write(text)
    return report
