"""docs/TASTYTRADE_QUOTES.md host commands, run as pasted (G5 backlog R4).

Each Part's fenced block is extracted from the guide and pasted into a real `bash` and,
where installed, a real `zsh -f` (the iMac Terminal's shell). The stub `git` models a
linear history OLD -> R -> U in a state file; the stub `python` only logs and writes the
expected files for the chosen mode, except `-m desk.evidence_files`, which runs the real
checker. A fake `.venv/bin/python` stands in for the credential tool. Nothing real is
fetched, installed, prompted for or contacted.
"""
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs/TASTYTRADE_QUOTES.md"
OLD, R, U, ABSENT = "1" * 40, "2" * 40, "3" * 40, "4" * 40   # host HEAD, reviewed, newer unreviewed, never pushed
HISTORY = [OLD, R, U]
SHELLS = [pytest.param("bash", id="bash"),
          pytest.param("zsh", id="zsh", marks=pytest.mark.skipif(shutil.which("zsh") is None,
                                                                 reason="zsh not installed"))]


def block(part):
    text = GUIDE.read_text()
    section = text[text.index("## Read-only host commands"):text.index("## Live-run follow-up provider checks")]
    return re.search(rf"\*\*Part {part}:.*?```sh\n(.*?)```", section, re.S)[1]


GIT = r'''#!/bin/bash
# State: $HOME/git-state.json {"head", "remote", "fetched", "branch", "dirty", "fetch_ok"}
st="$HOME/git-state.json"
get() { python3 -c "import json,sys; print(json.load(open('$st'))[sys.argv[1]])" "$1"; }
put() { python3 -c "import json,sys; d=json.load(open('$st')); d[sys.argv[1]]=sys.argv[2]; json.dump(d,open('$st','w'))" "$1" "$2"; }
idx() { python3 -c "import sys; h=sys.argv[2].split(','); print(h.index(sys.argv[1]) if sys.argv[1] in h else -1)" "$1" "$HISTORY"; }
resolve() { case "$1" in HEAD) get head;; origin/codex/repair-step-01-baseline) get fetched;; *) echo "${1%^\{commit\}}";; esac; }
echo "git $* [cwd=$PWD]" >> "$HOME/log.txt"
case "$1" in
  status) [ "$(get dirty)" = "True" ] && echo " M src/desk/risk.py"; exit 0;;
  branch) get branch; exit 0;;
  fetch) [ "$(get fetch_ok)" = "True" ] || exit 1; put fetched "$(get remote)"; exit 0;;
  rev-parse) resolve "$2"; exit 0;;
  cat-file) c=$(resolve "$3"); [ "$(idx "$c")" -ge 0 ] && [ "$(idx "$c")" -le "$(idx "$(get fetched)")" ]; exit $?;;
  merge-base) a=$(idx "$(resolve "$3")"); b=$(idx "$(resolve "$4")"); [ "$a" -ge 0 ] && [ "$b" -ge 0 ] && [ "$a" -le "$b" ]; exit $?;;
  merge) t=$(resolve "$3"); [ "$(idx "$t")" -ge 0 ] && [ "$(idx "$(get head)")" -le "$(idx "$t")" ] || exit 1; put head "$t"; exit 0;;
esac
echo "unexpected git $*" >&2; exit 9
'''

PY = r'''#!/bin/bash
# Stub interpreter. `-m desk.evidence_files` runs the real checker; everything else logs and,
# for the three diagnostics, writes what $HOME/py-modes.json asks for.
if [ "$1" = "-m" ] && [ "$2" = "desk.evidence_files" ]; then
  PYTHONPATH="$DESK_SRC" exec "$DESK_REAL_PYTHON" -m desk.evidence_files "$3" "$4"
fi
echo "python $* [cwd=$PWD]" >> "$HOME/log.txt"
case "$*" in --version) echo "Python 3.x (stub)"; exit 0;; esac
name=""
case "$*" in *"quote_measure clock"*) name=clock;; *"--profile"*) name=profile;; *"desk.quote_check"*) name=quotes;; esac
[ -z "$name" ] && exit 0
out=$(python3 -c "import sys; a=sys.argv[1:]; print(a[a.index('--output')+1])" "$@")
mode=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],'ok'))" "$HOME/py-modes.json" "$name")
good='{"status":"OBSERVATIONS_ONLY"}'
[ "$name" = clock ] && good='{"status":"MEASURED","measured_finished_at":"2026-10-07T17:00:00+00:00","offset_seconds_as_printed":"+0.104","plus_minus_seconds_as_printed":"0.020"}'
case "$mode" in
  ok) echo "$good" > "$out"; exit 0;;
  ok_nofile) exit 0;;
  ok_malformed) echo '{"status": ' > "$out"; exit 0;;
  ok_unmeasured) echo '{"status":"UNAVAILABLE","code":"SNTP_TIMEOUT"}' > "$out"; exit 0;;
  fail_nofile) exit 1;;
  fail_written) echo '{"status":"UNAVAILABLE"}' > "$out"; exit 1;;
esac
exit 8
'''

TOOL = '#!/bin/bash\necho "CREDENTIAL TOOL RAN from $PWD (repo marker: $(cat "$PWD/MARKER" 2>/dev/null))" >> "$HOME/log.txt"\n'

# Hidden-prompt stand-in: reports whether stdin is the terminal, then reads without echo.
PROMPT_TOOL = r'''#!/bin/bash
if [ -t 0 ]; then echo "STDIN_IS_TERMINAL"; else echo "STDIN_NOT_TERMINAL"; fi
printf 'Secret: '
IFS= read -rs secret || { echo "READ_FAILED"; exit 3; }
echo
echo "READ_LENGTH ${#secret}"
'''


def checkout(path, marker, tool=TOOL):
    (path / ".venv/bin").mkdir(parents=True)
    (path / "tools").mkdir()
    (path / "MARKER").write_text(marker)
    (path / ".venv/bin/activate").write_text("# stub\n")
    (path / ".venv/bin/python").write_text(tool)
    (path / ".venv/bin/python").chmod(0o755)
    (path / "tools/setup_tastytrade_env.py").write_text("# stub\n")


@pytest.fixture
def host(tmp_path):
    home = tmp_path / "home"
    repo = home / "Trading-Agent"
    checkout(repo, "reviewed-checkout")
    other = tmp_path / "other-checkout"                       # wherever the Terminal happens to be
    checkout(other, "OTHER-checkout")
    (home / ".config/trading-desk").mkdir(parents=True)
    (home / ".config/trading-desk/env").write_text("export FIXTURE_ONLY=1\n")
    (home / "Desktop").mkdir()
    bins = tmp_path / "bin"
    bins.mkdir()
    for name, body in (("git", GIT), ("python", PY)):
        (bins / name).write_text(body)
        (bins / name).chmod(0o755)
    (home / "py-modes.json").write_text("{}")
    env = {"HOME": str(home), "PATH": f"{bins}:/usr/bin:/bin", "TMPDIR": str(tmp_path),
           "HISTORY": ",".join(HISTORY), "DESK_SRC": str(ROOT / "src"), "DESK_REAL_PYTHON": sys.executable,
           "LANG": "C"}
    return dict(home=home, repo=repo, other=other, env=env, tmp=tmp_path)


def state(h, **kw):
    s = dict(head=OLD, remote=U, fetched=OLD, branch="codex/repair-step-01-baseline", dirty=False, fetch_ok=True)
    s.update(kw)
    (h["home"] / "git-state.json").write_text(json.dumps(s))


def head(h):
    return json.loads((h["home"] / "git-state.json").read_text())["head"]


def log(h):
    p = h["home"] / "log.txt"
    return p.read_text() if p.exists() else ""


def argv(shell, text):
    return [shell, "-f", "-c", text] if shell == "zsh" else [shell, "--noprofile", "--norc", "-c", text]


def paste(h, part, sha, shell, cwd=None):
    """Paste the guide's block into an interactive-style shell, as Taz would."""
    return subprocess.run(argv(shell, block(part).replace("REVIEWED_SHA", sha)), env=h["env"], text=True,
                          capture_output=True, timeout=60, cwd=cwd or h["other"])


def modes(h, **kw):
    (h["home"] / "py-modes.json").write_text(json.dumps(kw))


def report_dir(h):
    dirs = list((h["home"] / "Desktop").iterdir())
    assert len(dirs) == 1
    return dirs[0]


# ------------------------------------------------------------------ Part 1: update -------
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("remote", [R, U], ids=["origin-at-reviewed", "origin-advanced"])
def test_part1_fast_forwards_to_exactly_the_reviewed_commit(host, shell, remote):
    state(host, remote=remote)
    r = paste(host, 1, R, shell)
    assert r.returncode == 0 and f"SHA_OK {R}" in r.stdout and "UPDATE_OK" in r.stdout, r.stdout + r.stderr
    assert head(host) == R                                    # never the newer unreviewed head
    assert "python -m pip install" in log(host) and str(host["other"]) not in log(host)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("kw,sha,stop", [
    (dict(), ABSENT, "STOP: reviewed commit not fetched"),
    (dict(remote=OLD), R, "STOP: reviewed commit not fetched"),               # not pushed yet
    (dict(head=U, remote=U), R, "STOP: this checkout is not behind the reviewed commit"),  # never rewind
    (dict(fetch_ok=False), R, "STOP: fetch failed"),
    (dict(dirty=True), R, "STOP: tracked files have local changes"),
    (dict(branch="main"), R, "STOP: wrong branch"),
    (dict(), R[:12], "STOP: give the full 40-character reviewed commit SHA"),
    (dict(), "REVIEWED_SHA_NOT_REPLACED", "STOP: give the full 40-character reviewed commit SHA"),
])
def test_part1_guards_stop_before_any_merge_install_or_suite(host, shell, kw, sha, stop):
    state(host, **kw)
    before = head(host)
    r = paste(host, 1, sha, shell)
    assert r.returncode == 1 and stop in r.stdout, r.stdout + r.stderr
    assert head(host) == before and "git merge --ff-only" not in log(host)
    assert "python -m pip" not in log(host) and "python -m pytest" not in log(host)


@pytest.mark.parametrize("shell", SHELLS)
def test_part1_bad_sha_and_dirty_tree_stop_before_fetch(host, shell):
    for kw, sha in ((dict(), R[:12]), (dict(dirty=True), R), (dict(branch="x"), R)):
        state(host, **kw)
        (host["home"] / "log.txt").unlink(missing_ok=True)
        assert paste(host, 1, sha, shell).returncode == 1
        assert "git fetch" not in log(host)


@pytest.mark.parametrize("shell", SHELLS)
def test_part1_failed_cd_stops_and_never_touches_the_current_directory(host, shell):
    state(host, remote=R)
    os.rename(host["repo"], host["home"] / "Trading-Agent-renamed")
    r = paste(host, 1, R, shell)
    assert r.returncode == 1 and f"STOP: cannot enter {host['home']}/Trading-Agent" in r.stdout
    assert "git " not in log(host) and "python " not in log(host) and head(host) == OLD


# ------------------------------------------------------------- Part 2: credentials -------
@pytest.mark.parametrize("shell", SHELLS)
def test_part2_runs_the_tool_from_the_verified_checkout_whatever_the_terminal_directory(host, shell):
    state(host, head=R)
    r = paste(host, 2, R, shell, cwd=host["other"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"CREDENTIAL TOOL RAN from {host['repo']} (repo marker: reviewed-checkout)" in log(host)
    assert "OTHER-checkout" not in log(host)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("kw,sha,setup,stop", [
    (dict(head=R), R, "rename", "STOP: cannot enter"),
    (dict(head=U), R, None, "STOP: HEAD is not the reviewed commit"),
    (dict(head=OLD), R, None, "STOP: HEAD is not the reviewed commit"),
    (dict(head=R), ABSENT, None, "STOP: HEAD is not the reviewed commit"),
    (dict(head=R, dirty=True), R, None, "STOP: tracked files have local changes"),
    (dict(head=R, branch="other"), R, None, "STOP: wrong branch"),
    (dict(head=R), R[:7], None, "STOP: give the full 40-character reviewed commit SHA"),
    (dict(head=R), R, "no-tool", "STOP: credential tool missing"),
    (dict(head=R), R, "no-python", "STOP: .venv/bin/python missing"),
])
def test_part2_guards_stop_before_the_credential_tool(host, shell, kw, sha, setup, stop):
    state(host, **kw)
    if setup == "rename":
        os.rename(host["repo"], host["home"] / "Trading-Agent-renamed")
    elif setup == "no-tool":
        (host["repo"] / "tools/setup_tastytrade_env.py").unlink()
    elif setup == "no-python":
        (host["repo"] / ".venv/bin/python").unlink()
    r = paste(host, 2, sha, shell, cwd=host["other"])
    assert r.returncode == 1 and stop in r.stdout, r.stdout + r.stderr
    assert "CREDENTIAL TOOL RAN" not in log(host)


def run_in_terminal(h, shell, text, answer, cwd):
    """Run `text` in `shell` attached to a pseudo-terminal; type `answer` at the prompt."""
    pid, fd = pty.fork()
    if pid == 0:                                                   # child: the Terminal's shell
        os.chdir(cwd)
        os.execve(shutil.which(shell), argv(shell, text), h["env"])
    out, sent, stalled = b"", False, False
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 30)
            if not ready:
                stalled = True
                break
            try:
                chunk = os.read(fd, 1024)
            except OSError:                                        # EIO once the child has exited
                break
            if not chunk:
                break
            out += chunk
            if not sent and b"Secret: " in out:
                os.write(fd, answer)
                sent = True
    finally:
        if stalled or not sent:
            os.kill(pid, signal.SIGKILL)                           # never leave a prompt waiting
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert not stalled, "terminal stalled: %r" % out
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


@pytest.mark.parametrize("shell", SHELLS)
def test_part2_keeps_the_terminal_as_stdin_so_input_stays_hidden(host, shell):
    state(host, head=R)
    (host["repo"] / ".venv/bin/python").write_text(PROMPT_TOOL)
    code, out = run_in_terminal(host, shell, block(2).replace("REVIEWED_SHA", R), b"hunter2-fixture\n",
                                host["other"])
    assert code == 0, out
    assert "STDIN_IS_TERMINAL" in out and "READ_LENGTH 15" in out
    assert "hunter2-fixture" not in out                            # typed text was not echoed


# ------------------------------------------------------------- Part 3: diagnostics -------
@pytest.mark.parametrize("shell", SHELLS)
def test_part3_success_passes_the_valid_clock_and_exits_zero(host, shell):
    state(host, head=R)
    r = paste(host, 3, R, shell)
    assert r.returncode == 0, r.stdout + r.stderr
    for name in ("clock", "quotes", "profile"):
        assert f"{name}: exit 0, report VALID" in r.stdout
    assert "ALL_COMMANDS_EXIT_0_REPORTS_VALID (observations only; not a live-data PASS)" in r.stdout
    assert "SUMMARY" not in r.stdout
    quotes = [line for line in log(host).splitlines() if "--measure" in line]
    assert len(quotes) == 1 and f"--host-clock {report_dir(host)}/clock.json" in quotes[0]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("given,lines,failed,clock_passed", [
    (dict(clock="fail_nofile"), ["clock: exit 1, report MISSING"], "clock", False),
    (dict(clock="ok_nofile"), ["clock: exit 0, report MISSING"], "clock", False),
    (dict(clock="ok_malformed"), ["clock: exit 0, report MALFORMED"], "clock", False),
    (dict(clock="ok_unmeasured"), ["clock: exit 0, report UNAVAILABLE"], "clock", False),
    (dict(quotes="ok_nofile"), ["quotes: exit 0, report MISSING"], "quotes", True),
    (dict(quotes="fail_written"), ["quotes: exit 1, report VALID"], "quotes", True),
    (dict(profile="ok_malformed"), ["profile: exit 0, report MALFORMED"], "profile", True),
    (dict(clock="fail_nofile", quotes="fail_nofile", profile="fail_nofile"),
     ["clock: exit 1, report MISSING", "quotes: exit 1, report MISSING", "profile: exit 1, report MISSING"],
     "clock quotes profile", False),
])
def test_part3_reports_each_exit_and_file_and_ends_nonzero(host, shell, given, lines, failed, clock_passed):
    state(host, head=R)
    modes(host, **given)
    r = paste(host, 3, R, shell)
    assert r.returncode == 1, r.stdout + r.stderr
    for line in lines:
        assert line in r.stdout, r.stdout
    note = "" if clock_passed else "; host clock not attached"
    assert f"SUMMARY: failed or missing evidence: {failed}{note}\n" in r.stdout, r.stdout
    assert "EVIDENCE_COLLECTED_WITH_FAILURES (observations only; not a live-data PASS)" in r.stdout
    assert "ALL_COMMANDS_EXIT_0" not in r.stdout and "written;" not in r.stdout
    calls = log(host)
    assert calls.count("desk.quote_check") == 2                    # peers still collected after a failure
    assert ("--host-clock" in calls) is clock_passed
    assert ("captures run without --host-clock" in r.stdout) is not clock_passed


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("kw,sha,setup,stop", [
    (dict(head=U), R, None, "STOP: HEAD is not the reviewed commit"),
    (dict(head=R), ABSENT, None, "STOP: HEAD is not the reviewed commit"),
    (dict(head=R, dirty=True), R, None, "STOP: tracked files have local changes"),
    (dict(head=R, branch="x"), R, None, "STOP: wrong branch"),
    (dict(head=R), R[:12], None, "STOP: give the full 40-character reviewed commit SHA"),
    (dict(head=R), R, "rename", "STOP: cannot enter"),
    (dict(head=R), R, "no-env", "STOP: credential file missing"),
])
def test_part3_guards_stop_before_credentials_or_any_diagnostic(host, shell, kw, sha, setup, stop):
    state(host, **kw)
    if setup == "rename":
        os.rename(host["repo"], host["home"] / "Trading-Agent-renamed")
    elif setup == "no-env":
        (host["home"] / ".config/trading-desk/env").unlink()
    r = paste(host, 3, sha, shell)
    assert r.returncode == 1 and stop in r.stdout, r.stdout + r.stderr
    assert "python " not in log(host) and not list((host["home"] / "Desktop").iterdir())
