# Checkpoint 01 — baseline and branch reconciliation

Date: 2026-09-30. Status: complete locally. Next: Step 02, not started.
Trader-day functions: watch, plan, journal. Implementer: Codex.
Review: local self-review; no independent Claude review claimed.

## Requirement and scope

Taz requested a complete numbered repair plan and authorized starting work, with
stopping points between steps. Step 01 establishes reproducible evidence and the
integration path. It does not repair runtime trading behavior or deploy anything.

Local checkout: `work/Trading-Agent` in the Codex task workspace.
Local branch: `codex/repair-step-01-baseline`.

| Reference | Exact commit |
| --- | --- |
| Working branch `claude/project-thread-837gow` | `4a93fcef5230025e3abdd78377ef3ae4c8357b89` |
| PR #1 head `claude/project-thread-vkjlmn` | `3ba9855bce70196b880439f87e4d1f4c4a7cd15e` |
| Common ancestor | `7a40c491aa1f70acd9ac2fe8b76eb33c753659ff` |

The GitHub PR payload's base SHA was the common ancestor, not the current working
branch tip. Local Git history was used to verify the actual current branches.

## Reconciliation decision

The working branch has newer Webull/scanner/watchlist/setup work. PR #1 has two risk
commits and changes seven files: `CLAUDE.md`, `src/desk/contracts.py`, `src/desk/risk.py`,
`tests/conftest.py`, `tests/test_risk.py`, `tests/test_risk_v23.py`, `tests/test_risk_v24.py`.

Step 02 should incorporate PR #1 into the latest working-branch lineage and immediately
update its now-superseded budget rules. Retain dollar loss halts, quote/tradability checks
and removal of count caps. Remove the hard $100 ceiling and grade-dollar assignment;
use the user's selected risk budget. Do not replace the whole working branch with PR #1.

A temporary merge applied without conflicts and passed the combined test suite. It
was aborted afterward, restoring the unchanged working-branch runtime. The permanent
Step 02 integration must account for the documentation additions in this checkpoint;
CLAUDE.md may require a small manual reconciliation. Retest then against actual new changes.

## Checks actually run

An isolated `.venv` was created with Python 3.12.14. The project's existing dependencies
were installed using `uv pip install --python .venv/bin/python -e '.[dev]'` with a
temporary cache. No global package installation or runtime service activation occurred.

Each suite used `.venv/bin/python -m pytest -q`:

| Checkout | Result | Meaning |
| --- | --- | --- |
| Working branch at `4a93fce` | 114 passed in 10.71s | Existing working-branch tests reproduced. |
| PR #1 at `3ba9855` | 60 passed in 0.29s | Existing tests on its older branch reproduced. |
| Temporary merge of both exact commits | 119 passed in 0.83s | Current suites are mechanically compatible when combined. |

Counts are not additive: PR #1 modifies/replaces existing tests and starts from an
older history. Passing tests are not evidence that audit defects are fixed, that
live provider semantics are correct, or that trading is profitable.

Observed environment (recorded, not a newly imposed dependency pin):

```
Python 3.12.14 (arm64 macOS)
pytest 9.1.1
pydantic 2.13.5
pydantic-core 2.46.5
numpy 2.5.3
pandas 3.0.6
TA-Lib 0.8.1
```

## Changes delivered by this step

- `AGENTS.md`: bounded-step workflow and honest completion/review rules.
- `docs/REPAIR_PLAN.md`: all 22 steps, dependencies, ownership, acceptance examples,
  user decisions, unresolved choices, fallback behavior and checkpoint template.
- `CLAUDE.md`: links the repair protocol and flags the latest policy decisions.
- `README.md`: points to the current tracker and distinguishes repairs from deployed behavior.
- This checkpoint and user-facing copies in the task's `outputs` directory.

## Remaining limitations and rollback

Runtime source and tests are unchanged at this checkpoint. The $100 rule remains in
unmerged PR #1; the working branch still has legacy percentage sizing. The 11:00 EP
logic remains in runtime until Step 03. No live-data or broker check, scheduled runner,
paper order, live order, remote push or GitHub merge was performed.

The older blueprint source/research files outside this repository were not copied or
rewritten. Source-specific questions remain in the tracker. No Claude work was dispatched.

Rollback: this step changes documentation only. Revert its documentation checkpoint
commit or return to the pinned working-branch base. There is no data/schema migration
or running service to roll back. Preserve later contributor work if rollback happens later.

## Exit checklist

- [x] Working and PR heads pinned; history divergence and changed files inspected.
- [x] Both suites reproduced and temporary integration tested.
- [x] Temporary integration removed; source and tests match the working-branch base.
- [x] New user decisions and still-open questions distinguished from implemented code.
- [x] Full numbered plan, acceptance criteria, roles and checkpoints written.
- [x] Step 02 integration approach selected; no unresolved issue blocks starting it.

Stop at Step 01. The next instruction is **Start Step 02: user-selected risk budget**.
