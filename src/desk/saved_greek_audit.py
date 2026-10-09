"""Offline audit of saved measurement rows, not replay certification or current state.

The bounded measurement log omits some control/health messages. Do not manufacture
a complete snapshot from it. Verify each retained indexed observation and its own
age; use the current reducer only on a complete subsequent live capture.
"""
import argparse
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from desk.tastytrade_greeks import indexed_time, VALUES
from desk.option_conventions import normalize_greeks
from desk.tradier_fields import write_report
from desk.tastytrade_quotes import QuoteUnavailable, aware


def audit(capture, sha):
    m=capture.get('measurements', {})
    identities={}
    for i in capture.get('identity_capture',[]):
        if isinstance(i,dict):
            identities.setdefault(i.get('desk_symbol'),{})[json.dumps(i,sort_keys=True,default=str)]=i
    result=dict(purpose='saved Greek observation audit; zero provider calls',input_sha256=sha,
                provider_calls=0,decision_eligibility='NOT_ATTESTED',live_state='NOT_RESTORED',
                scope='retained observations only; control/health history is not complete',
                record_drops=m.get('records_dropped',{}),observations=[],issues=[])
    records=m.get('records',[])
    for n,r in enumerate(records):
        if not isinstance(r,dict) or r.get('type')!='GREEKS':continue
        out=dict(record=n,symbol=r.get('symbol'),generation=r.get('generation'),
                 label='HISTORICAL_OBSERVATION',snapshot_acceptance='NOT_ATTESTED')
        try:
            references=identities.get(r.get('symbol'),{})
            if len(references)!=1:
                raise QuoteUnavailable('SAVED_IDENTITY_MISSING_OR_CONFLICTING')
            identity=next(iter(references.values()))
            if (identity.get('kind')!='Equity Option' or
                    identity.get('streamer_symbol')!=r.get('wire_symbol')):
                raise QuoteUnavailable('SAVED_IDENTITY_MISSING_OR_CONFLICTING')
            at=aware(datetime.fromisoformat(r['received_at']))
            row=dict(index=r.get('index'),sequence=r.get('sequence'),time=r.get('source_time',{}).get('raw'))
            index,source=indexed_time(row,at)
            raw={k:r[k]['value'] for k in VALUES if isinstance(r.get(k),dict) and r[k].get('state')=='VALUE'}
            out.update(index=index,sequence=row['sequence'],event_flags=r.get('event_flags'),
                       source_at=source.isoformat(),received_at=at.isoformat(),
                       receipt_age_seconds=str(Decimal((at-source)//timedelta(microseconds=1))/1000000),
                       fields=normalize_greeks('tastytrade','dxlink',raw)['fields'],
                       status='INDEXED_OBSERVATION_VALID')
        except (ValueError,TypeError,KeyError,ArithmeticError):
            out.update(status='UNAVAILABLE',reason='SAVED_ROW_OR_IDENTITY_INVALID')
        result['observations'].append(out)
    result['retained_greek_count']=len(result['observations'])
    result['status']='OBSERVATIONS_AUDITED' if result['observations'] else 'NO_GREEK_RECORDS'
    if any(r['status']=='UNAVAILABLE' for r in result['observations']):result['status']='PARTIAL_OBSERVATIONS'
    result['quote_size_units']='UNRESOLVED_NO_CONVERSION'
    result['tradier_conventions']='PROVISIONAL_NOT_CHANGED'
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--capture',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args(argv)
    if args.output.exists():p.error('Output already exists')
    raw=args.capture.read_bytes();result=audit(json.loads(raw,parse_float=Decimal),hashlib.sha256(raw).hexdigest())
    result=write_report(result,args.output)
    print(json.dumps({k:result[k] for k in ('status','retained_greek_count','live_state','provider_calls')},indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
