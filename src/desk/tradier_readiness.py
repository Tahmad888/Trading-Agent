"""Offline crosswalk preparation and configuration inventory. Never installs mappings.

No imports of a configured external factory, provider client construction, account
queries, credential output or store mutation. Drafts cannot be used by review CLI.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

from desk.quote_mapping import WebullSide
from desk.tradier_quotes import TradierSide, wire_symbol
from desk.tradier_fields import write_report
from desk.tastytrade_quotes import canonical


def unique(records):
    values = {json.dumps(r, sort_keys=True): r for r in records}
    return next(iter(values.values())) if len(values) == 1 else None


def capture_issues(webull, tradier):
    """Damage no single symbol owns; it withholds every draft (G5 backlog R3, D O2)."""
    issues=[]
    if not isinstance(webull,dict):issues.append(dict(path='webull',code='WEBULL_CAPTURE_NOT_OBJECT'))
    elif not isinstance(webull.get('identity_capture',[]),list):
        issues.append(dict(path='webull.identity_capture',code='WEBULL_IDENTITY_CAPTURE_NOT_LIST'))
    else:
        issues.extend(dict(path=f'webull.identity_capture[{n}]',code='WEBULL_IDENTITY_ROW_NOT_OBJECT')
                      for n,row in enumerate(webull.get('identity_capture',[])) if not isinstance(row,dict))
    if not isinstance(tradier,dict):issues.append(dict(path='tradier',code='TRADIER_CAPTURE_NOT_OBJECT'))
    elif not isinstance(tradier.get('rounds',[]),list):issues.append(dict(path='tradier.rounds',code='TRADIER_ROUNDS_NOT_LIST'))
    else:
        for n,round_ in enumerate(tradier.get('rounds',[])):
            if not isinstance(round_,dict):issues.append(dict(path=f'tradier.rounds[{n}]',code='TRADIER_ROUND_NOT_OBJECT'))
            elif not isinstance(round_.get('observations',{}),dict):
                issues.append(dict(path=f'tradier.rounds[{n}].observations',code='TRADIER_OBSERVATIONS_NOT_OBJECT'))
    return issues


def spelling(value):
    try:return canonical(value,'Equity')
    except ValueError:return None


def prepare(webull, tradier, symbols, webull_sha, tradier_sha):
    """Require explicit issuer metadata; prices and equal ticker strings are insufficient."""
    names = [canonical(s, 'Equity') for s in symbols]
    result = dict(purpose='offline crosswalk drafts; no identity approval or activation',
                  status='DRAFTS_REQUIRE_REVIEW', candidates={}, issues={}, provider_calls=0,
                  installed_mappings=0, operational_acceptance='NOT_ATTESTED',
                  input_sha256=dict(webull=webull_sha,tradier=tradier_sha),
                  capture_issues=capture_issues(webull,tradier), evidence_inventory={})
    if result['capture_issues']:
        # Unattributable damage could hide a contradiction for any symbol: no drafts.
        result['issues']={symbol:['CAPTURE_SHAPE_INVALID'] for symbol in names}
        result['status']='CAPTURE_SHAPE_INVALID'
        return result
    for symbol in names:
        problems=[]; wrows=[]; trows=[]; alternates=[]
        for row in webull.get('identity_capture', []):
            if row.get('symbol') != symbol:
                if row.get('symbol') is not None and spelling(row.get('symbol')) == symbol:
                    alternates.append(dict(source='webull',symbol=row.get('symbol'),instrument_id=row.get('instrument_id')))
                continue
            try:
                wrows.append(WebullSide.model_validate({k:row.get(k) for k in WebullSide.model_fields}).model_dump())
            except ValueError: problems.append('WEBULL_METADATA_INCOMPLETE')
        for round_ in tradier.get('rounds', []):
            observations=round_.get('observations', {})
            alternates.extend(dict(source='tradier',observation_key=key) for key in observations
                              if key != symbol and isinstance(key,str) and spelling(key) == symbol)
            row = observations.get(symbol, {})
            ref = row.get('issuer_reference', {}) if isinstance(row,dict) else None
            try:
                if not isinstance(ref,dict): raise ValueError
                side=TradierSide(environment=tradier.get('environment'), symbol=ref.get('symbol'),
                                 type=ref.get('type'), description=ref.get('description'))
                if side.symbol != wire_symbol(symbol): raise ValueError
                trows.append(side.model_dump())
            except ValueError: problems.append('TRADIER_ISSUER_METADATA_MISSING_OR_INVALID')
        w,t=unique(wrows),unique(trows)
        if w is None: problems.append('WEBULL_IDENTITY_MISSING_OR_CONTRADICTORY')
        if t is None: problems.append('TRADIER_IDENTITY_MISSING_OR_CONTRADICTORY')
        if w and t and (w['sub_category']=='ETF') != (t['type']=='etf'):
            problems.append('CROSS_PROVIDER_CLASSIFICATION_CONFLICT')
        # Alternate spellings are shown, never folded into an approval (D-O1).
        if any(a['source']=='webull' and (w is None or a['instrument_id']!=w['instrument_id']) for a in alternates):
            problems.append('ALTERNATE_SPELLING_IDENTITY_CONFLICT')
        if any(a['source']=='tradier' for a in alternates):
            problems.append('TRADIER_ALTERNATE_SPELLING_UNREVIEWED')
        result['evidence_inventory'][symbol]=dict(webull_rows=len(wrows),tradier_rows=len(trows),
                                                  alternate_spellings=alternates)
        if problems:
            result['issues'][symbol]=sorted(set(problems));continue
        result['candidates'][symbol]=dict(symbol=symbol, webull=w, tradier=t,
            webull_capture_sha256=webull_sha, tradier_capture_sha256=tradier_sha,
            method='human-reviewed-reference-v1', review_status='DRAFT_NOT_INSTALLABLE',
            required_review='Confirm issuer and exact share class; then create a ReviewedMapping record',
            shared_immutable_identifier='NOT_SUPPLIED')
    if result['issues']: result['status']='INCOMPLETE_CAPTURE_EVIDENCE'
    return result


def configuration(env):
    """Presence only. Do not invoke arbitrary external factory code under a zero-call promise."""
    keys=('DESK_TRADIER_BASE_FACTORY','DESK_TRADIER_ENVIRONMENT','DESK_TRADIER_QUOTE_STORE','TRADIER_ACCESS_TOKEN')
    labels={k:k if k!='TRADIER_ACCESS_TOKEN' else 'quote_credential' for k in keys}
    missing=[labels[k] for k in keys if not env.get(k)]
    path=env.get('DESK_TRADIER_QUOTE_STORE','')
    invalid=[]
    if env.get('DESK_TRADIER_ENVIRONMENT') not in {None,'','production','sandbox'}:
        invalid.append('DESK_TRADIER_ENVIRONMENT')
    if path and not Path(path).is_absolute():invalid.append('DESK_TRADIER_QUOTE_STORE')
    return dict(purpose='offline wiring inventory; factory not invoked',provider_calls=0,
                configured={labels[k]:bool(env.get(k)) for k in keys},missing_settings=missing,
                invalid_settings=invalid,
                quote_store_exists=bool(path and Path(path).is_file()),
                status='CONFIGURATION_MISSING' if missing else 'CONFIGURATION_INVALID' if invalid else 'CONFIGURED_INPUTS_NOT_ATTESTED',
                required_trusted_inputs=['account state','market/regime','halt/tradability',
                    'contract metadata and open interest for options','vendor price/volume source','ScanLog'],
                real_input_acceptance='NOT_ATTESTED', factory_invoked=False, installed_mappings=0)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--webull-capture',type=Path)
    parser.add_argument('--tradier-capture',type=Path)
    parser.add_argument('--symbols',nargs='+',default=['SPY','QQQ','NVDA'])
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if bool(args.webull_capture)!=bool(args.tradier_capture):parser.error('Supply both captures or neither')
    if args.output.exists():parser.error('Output already exists')
    result=configuration(os.environ)
    code=0
    if args.webull_capture:
        w,t=args.webull_capture.read_bytes(),args.tradier_capture.read_bytes()
        shas=hashlib.sha256(w).hexdigest(),hashlib.sha256(t).hexdigest()
        try:loaded=json.loads(w),json.loads(t)
        except ValueError:loaded=None
        if loaded is None:
            result['crosswalk_preparation']=dict(status='CAPTURE_JSON_INVALID',candidates={},provider_calls=0,
                installed_mappings=0,input_sha256=dict(webull=shas[0],tradier=shas[1]))
            code=1
        else:
            result['crosswalk_preparation']=prepare(*loaded,args.symbols,*shas)
            code=1 if result['crosswalk_preparation']['status']=='CAPTURE_SHAPE_INVALID' else 0
    result=write_report(result,args.output)
    print(json.dumps(result,indent=2))
    return code


if __name__=='__main__':raise SystemExit(main())
