"""Offline mutation regressions in disposable copies; never modifies the checkout or calls a provider."""
from pathlib import Path
import os,shutil,subprocess,sys,tempfile,json
import argparse
parser=argparse.ArgumentParser(description="Offline mutation checks for Tradier quote/ticket safeguards")
parser.add_argument('--output',type=Path)
args=parser.parse_args()
repo=Path(__file__).resolve().parents[1];python=Path(sys.executable)
cases=[
('attributed request start','tradier_quotes.py',
 'if opened is None or opened["key"] != key or opened["action"] != "START" or completed is not None:',
 'if False:', 'test_unattributed_result_cannot_clear_failed_check[1000000]'),
('late-failure ordering','tradier_quotes.py',
 'start = db.execute("SELECT MAX(id)', 'failure = 0\n        start = db.execute("SELECT MAX(id)',
 'test_late_failure_blocks_newer_success_until_later_started_request'),
('failure epoch','tradier_quotes.py','digest([instance, stop_id, failure])','digest([instance, stop_id, 0])',
 'test_old_approval_cannot_revive_after_stock_identity_restored'),
('current timestamp recheck','tradier_quotes.py',
 'if (datetime.fromisoformat(data["view"]["received_at"]) > aware(at)\n                or latest_policy_view(data["view"], aware(at))[component]["verdict"] != "PASS"):',
 'if False:', 'test_cached_ages_and_receipt_time_rechecked_at_use'),
('reviewed crosswalk','tradier_quotes.py',
 'def review(self, db, environment, symbol, price_basis, webull, identity):',
 'def review(self, db, environment, symbol, price_basis, webull, identity):\n        return "fake-mapping", 0',
 'test_mapping_failure_does_not_mutate_signal[wrong_id]'),
('final option reread','tickets.py','if inputs.executable_quotes is not None and event.eligible:',
 'if False:', 'test_final_reread_refuses_race_after_preliminary_check[option_spread]'),
('durable STOP','tradier_quotes.py','if stop and stop["action"] == "STOP":',
 'if False:', 'test_stop_restart_resume_requires_new_success[REST_HTTP_429]'),
]
results=[]
for name,file,old,new,test in cases:
 with tempfile.TemporaryDirectory(prefix='tradier-mut-') as temp:
  target=Path(temp)/'src';shutil.copytree(repo/'src',target)
  path=target/'desk'/file;s=path.read_text();assert old in s,(name,old);path.write_text(s.replace(old,new,1))
  env={**os.environ,'PYTHONPATH':str(target)}
  result=subprocess.run([str(python),'-m','pytest','-q','-W','error',
      'tests/test_tradier_quotes_runtime.py::'+test],cwd=repo,env=env,capture_output=True,text=True)
  # A collected assertion failure catches a mutation; collection/import errors do not.
  caught=result.returncode==1 and ('FAILED ' in result.stdout) and ('ERROR collecting' not in result.stdout)
  results.append(dict(protection=name,caught=caught,exit_code=result.returncode))
  print(name, 'CAUGHT' if caught else 'NOT CAUGHT',flush=True)
  if not caught:print(result.stdout[-3000:],result.stderr[-500:])
if args.output:
 args.output.parent.mkdir(parents=True,exist_ok=True)
 args.output.write_text(json.dumps(results,indent=2)+'\n')
sys.exit(0 if all(x['caught'] for x in results) else 1)
