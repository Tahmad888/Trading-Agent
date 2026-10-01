# Step 08 iMac metadata and bar identity acceptance

User-supplied terminal report, received in this conversation on 2026-10-01.
This is a transcription of the reported results, not a fresh Codex provider run.
The preceding pull output matches the 08a repair file changes; the tested code is
attributed to 3fb607f from that pull sequence, not from a SHA embedded in the report.

Command: `python -m desk.metadata_check --symbols NVDA SPY BRK.B`

- Environment: user's iMac terminal and existing `.venv`.
- Host, printed by probe: `api.sandbox.webull.com`.
- Check started: **2026-10-01T18:33:18.588768+00:00**.
- Purpose: metadata and bar identity only; no decision eligibility.
- Overall: **PASS**; skipped: **{}**.

| Desk symbol | Provider symbol | Instrument ID | Type | Bar category | D | M15 |
| --- | --- | --- | --- | --- | --- | --- |
| BRK.B | BRK B | 916040668 | COMMON_STOCK | US_STOCK | PASS | PASS |
| NVDA | NVDA | 913257561 | COMMON_STOCK | US_STOCK | PASS | PASS |
| SPY | SPY | 913243251 | ETF | US_ETF | PASS | PASS |

For all six bar checks, normalized identity matched the metadata symbol and ID;
raw provider identity preserved the provider symbol and the same ID. Berkshire's
metadata timestamp was 18:33:18.807011Z, NVDA's 18:33:18.807028Z, and SPY's
18:33:18.807034Z, all on 2026-10-01.

This closes the outstanding Step 08 metadata/alias integration check for these
three symbols on this sandbox connection. Earlier failed BRK.B/BRK-B lookups led
to the explicit verified mapping; they are not hidden or reclassified as passes.

Limits: not production-host acceptance, exhaustive symbol coverage, a reviewed
Berkshire corporate-action/price profile, freshness or volume acceptance, a setup
qualification, or trading activation. No new full-suite iMac result was supplied
with this probe. The repair's 621-test results remain local Codex Python 3.12/3.14
runs as recorded in checkpoint 08a. No code change is needed to record this result.
