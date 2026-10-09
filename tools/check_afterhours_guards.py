"""Offline safeguard mutations in disposable source copies; no provider calls."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists():p.error('Output already exists')
    repo=Path(__file__).resolve().parents[1]
    cases=[
        ('operational composition','tradier_risk.py',
         'client.use_operational_policy(store.reserve_request)','pass',
         'test_afterhours_budget.py::test_real_synthetic_ticket_checks_continue_beyond_twelve'),
        ('durable rate bound','tradier_quotes.py',
         'if sum(t > at - timedelta(minutes=1) for t in stamps) >= limit:', 'if False:',
         'test_afterhours_budget.py::test_rate_reservations_survive_reopen_and_exact_minute_boundary'),
        ('local refusal isolation','tradier_quotes.py',
         'if isinstance(exc, RequestBudgetExceeded):', 'if False:',
         'test_afterhours_budget.py::test_budget_failure_is_ticker_failure_not_provider_stop_and_changes_generation'),
        ('honest dispatch labels','tradier_client.py',
         '"dispatch_attempted_at": at.isoformat()', '"sent_at": at.isoformat()',
         'test_afterhours_budget.py::test_external_local_budget_failure_is_not_sent_or_provider_stop'),
        ('raw Greek summary','quote_measure.py',
         '"label": "RAW_OBSERVATION_SUMMARY"', '"label": "CURRENT"',
         'test_afterhours_budget.py::test_raw_greek_summary_is_explicitly_linked_to_reducer'),
        ('crosswalk contradictions','tradier_readiness.py',
         'len(values) == 1', 'len(values) >= 1',
         'test_afterhours_readiness.py::test_incomplete_or_conflicting_captures_never_yield_candidate[duplicate]'),
        ('saved identity contradictions','saved_greek_audit.py',
         'if len(references)!=1:', 'if not references:',
         'test_afterhours_readiness.py::test_saved_identity_repeats_cannot_hide_contradiction[True]'),
    ]
    results=[]
    for name,file,old,new,test in cases:
        with tempfile.TemporaryDirectory(prefix='g5-afterhours-mut-') as temp:
            target=Path(temp)/'src';shutil.copytree(repo/'src',target)
            path=target/'desk'/file;text=path.read_text()
            assert old in text,(name,old)
            path.write_text(text.replace(old,new,1))
            env={**os.environ,'PYTHONPATH':str(target)+os.pathsep+str(repo)}
            r=subprocess.run([sys.executable,'-m','pytest','-q','-W','error','tests/'+test],
                cwd=repo,env=env,capture_output=True,text=True)
            caught=r.returncode==1 and 'FAILED ' in r.stdout and 'ERROR collecting' not in r.stdout
            results.append(dict(protection=name,caught=caught,exit_code=r.returncode))
            print(name,'CAUGHT' if caught else 'NOT CAUGHT',flush=True)
            if not caught:print(r.stdout[-2000:],r.stderr[-500:])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(results,indent=2)+'\n')
    return 0 if all(x['caught'] for x in results) else 1


if __name__=='__main__':raise SystemExit(main())
