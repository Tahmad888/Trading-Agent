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


def shape_issues(capture):
    """Capture-level damage that no single record or symbol owns (G5 backlog R3, D O2)."""
    if not isinstance(capture,dict):return [dict(path='$',code='CAPTURE_ROOT_NOT_OBJECT')]
    issues=[];m=capture.get('measurements',{})
    if not isinstance(m,dict):issues.append(dict(path='measurements',code='MEASUREMENTS_NOT_OBJECT'))
    else:
        if not isinstance(m.get('records',[]),list):issues.append(dict(path='measurements.records',code='RECORDS_NOT_LIST'))
        if not isinstance(m.get('records_dropped',{}),dict):
            issues.append(dict(path='measurements.records_dropped',code='RECORDS_DROPPED_NOT_OBJECT'))
    ids=capture.get('identity_capture',[])
    if not isinstance(ids,list):issues.append(dict(path='identity_capture',code='IDENTITY_CAPTURE_NOT_LIST'))
    else:
        # An unreadable identity row could contradict any symbol, so it is not isolated.
        for n,i in enumerate(ids):
            if not isinstance(i,dict):
                issues.append(dict(path=f'identity_capture[{n}]',code='IDENTITY_ROW_NOT_OBJECT'))
            elif (code:=symbol_issue(i.get('desk_symbol'))):
                # Rows are grouped by this symbol; one that names no symbol is unattributable (R3b).
                issues.append(dict(path=f'identity_capture[{n}].desk_symbol',code=code))
    return issues


def symbol_issue(value):
    """Why a saved identity's desk_symbol cannot attribute it to one symbol, or None."""
    if value is None:return 'IDENTITY_SYMBOL_MISSING'
    if not isinstance(value,str):return 'IDENTITY_SYMBOL_NOT_TEXT'
    if not value or not value.isprintable() or any(c.isspace() for c in value):return 'IDENTITY_SYMBOL_INVALID_TEXT'
    return None


def audit(capture, sha):
    result=dict(purpose='saved Greek observation audit; zero provider calls',input_sha256=sha,
                provider_calls=0,decision_eligibility='NOT_ATTESTED',live_state='NOT_RESTORED',
                scope='retained observations only; control/health history is not complete',
                record_drops={},observations=[],issues=[],
                quote_size_units='UNRESOLVED_NO_CONVERSION',tradier_conventions='PROVISIONAL_NOT_CHANGED')
    damaged=shape_issues(capture)
    if damaged:
        result.update(status='CAPTURE_SHAPE_INVALID',issues=damaged,retained_greek_count=0)
        return result
    m=capture.get('measurements', {})
    identities={}
    for i in capture.get('identity_capture',[]):
        identities.setdefault(i.get('desk_symbol'),{})[json.dumps(i,sort_keys=True,default=str)]=i
    result['record_drops']=m.get('records_dropped',{})
    records=m.get('records',[])
    for n,r in enumerate(records):
        if not isinstance(r,dict):
            result['issues'].append(dict(path=f'measurements.records[{n}]',code='RECORD_NOT_OBJECT'));continue
        if r.get('type')!='GREEKS':continue
        out=dict(record=n,symbol=r.get('symbol'),generation=r.get('generation'),
                 label='HISTORICAL_OBSERVATION',snapshot_acceptance='NOT_ATTESTED')
        try:
            if not isinstance(r.get('source_time',{}),dict):
                raise QuoteUnavailable('SAVED_ROW_SHAPE_INVALID')
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
        except QuoteUnavailable as exc:
            out.update(status='UNAVAILABLE',reason=str(exc) if str(exc)=='SAVED_ROW_SHAPE_INVALID'
                       else 'SAVED_ROW_OR_IDENTITY_INVALID')
        except (ValueError,TypeError,KeyError,ArithmeticError):
            out.update(status='UNAVAILABLE',reason='SAVED_ROW_OR_IDENTITY_INVALID')
        result['observations'].append(out)
    result['retained_greek_count']=len(result['observations'])
    result['status']='OBSERVATIONS_AUDITED' if result['observations'] else 'NO_GREEK_RECORDS'
    if result['issues'] or any(r['status']=='UNAVAILABLE' for r in result['observations']):
        result['status']='PARTIAL_OBSERVATIONS'
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--capture',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args(argv)
    if args.output.exists():p.error('Output already exists')
    raw=args.capture.read_bytes();sha=hashlib.sha256(raw).hexdigest()
    try:capture=json.loads(raw,parse_float=Decimal)
    except ValueError:capture=None
    if capture is None:
        result=audit([],sha);result.update(status='CAPTURE_JSON_INVALID',issues=[dict(path='$',code='CAPTURE_JSON_INVALID')])
    else:result=audit(capture,sha)
    result=write_report(result,args.output)
    print(json.dumps({k:result.get(k) for k in ('status','retained_greek_count','live_state','provider_calls')},indent=2))
    return 1 if result['status'] in {'CAPTURE_SHAPE_INVALID','CAPTURE_JSON_INVALID','REPORT_REJECTED'} else 0


if __name__=='__main__':raise SystemExit(main())
