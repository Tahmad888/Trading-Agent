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


def prepare(webull, tradier, symbols, webull_sha, tradier_sha):
    """Require explicit issuer metadata; prices and equal ticker strings are insufficient."""
    names = [canonical(s, 'Equity') for s in symbols]
    result = dict(purpose='offline crosswalk drafts; no identity approval or activation',
                  status='DRAFTS_REQUIRE_REVIEW', candidates={}, issues={}, provider_calls=0,
                  installed_mappings=0, operational_acceptance='NOT_ATTESTED')
    for symbol in names:
        problems=[]; wrows=[]; trows=[]
        for row in webull.get('identity_capture', []):
            if not isinstance(row, dict) or row.get('symbol') != symbol: continue
            try:
                wrows.append(WebullSide.model_validate({k:row.get(k) for k in WebullSide.model_fields}).model_dump())
            except ValueError: problems.append('WEBULL_METADATA_INCOMPLETE')
        for round_ in tradier.get('rounds', []):
            row = round_.get('observations', {}).get(symbol, {})
            ref = row.get('issuer_reference', {})
            try:
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
    if args.webull_capture:
        w,t=args.webull_capture.read_bytes(),args.tradier_capture.read_bytes()
        result['crosswalk_preparation']=prepare(json.loads(w),json.loads(t),args.symbols,
            hashlib.sha256(w).hexdigest(),hashlib.sha256(t).hexdigest())
    result=write_report(result,args.output)
    print(json.dumps(result,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
