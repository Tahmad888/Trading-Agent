"""Inspect cached source health, catalyst candidates and earnings gates; no network."""
import argparse
from datetime import datetime, timezone
import json

from desk.earnings import evaluate
from desk.earnings_refresh import EarningsCache, load_policy
from desk.earnings_check import aware_time


def check(policy_path, database, at):
    policy, digest = load_policy(policy_path)
    cache = EarningsCache(database)
    results = []
    for identity in policy.symbols:
        row = cache.latest(identity.symbol)
        result = dict(symbol=identity.symbol, status='UNAVAILABLE', snapshot_id=row['id'] if row else None)
        if row:
            payload = json.loads(row['payload'])
            collection = payload.get('catalyst_collection', {})
            result.update(source_status=row['status'], source_health=payload.get('source_health', {}),
                          expires_at=row['expires'], catalyst_status=collection.get('status', 'UNKNOWN'),
                          catalyst_coverage=collection.get('coverage', 'UNKNOWN'),
                          catalyst_reviews=payload.get('catalyst_reviews', []),
                          candidates=[{k:v for k,v in c.items() if k!='body'} for c in collection.get('candidates', [])])
        try:
            if at > policy.valid_until:
                raise ValueError('expired policy')
            evidence = cache.evidence(identity.symbol, at, digest)
            checks = {name: evaluate(setup, identity.symbol, identity.security_id, evidence, at)
                      for name, setup in (('cup','3_oneil_cup_with_handle'),('ep','5_qullamaggie_episodic_pivot'))}
            result.update(status='EVALUATED', checks={name:{k:v for k,v in value.items() if k!='evidence'} for name,value in checks.items()})
        except (ValueError, KeyError):
            result['reason']='latest cache snapshot unavailable, expired or policy changed'
        results.append(result)
    return dict(purpose='cached fundamentals/candidate inspection; no chart/order eligibility', provider_requests=0,
                evaluated_at=at.isoformat(), checks=results,
                status='EVALUATED' if all(r['status']=='EVALUATED' for r in results) else 'INCOMPLETE')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',required=True)
    parser.add_argument('--database',required=True)
    parser.add_argument('--at',type=aware_time)
    args=parser.parse_args(argv)
    try:
        result=check(args.policy,args.database,args.at or datetime.now(timezone.utc))
    except Exception:
        result={'status':'UNAVAILABLE','reason':'cache or configuration invalid'}
    print(json.dumps(result,indent=2))
    return 0 if result['status']=='EVALUATED' else 1


if __name__=='__main__':
    raise SystemExit(main())
