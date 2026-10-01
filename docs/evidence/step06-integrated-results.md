# Step 06 — reported integrated acceptance results

Code under test: f395c7e6662c2fd1a75b6cfebc851d56b9d6f66e.
Evidence: user-supplied command outputs; no authenticated requests repeated by Codex.

| Check | NVDA | SPY |
|---|---|---|
| Host running the check | Claude cloud, Python 3.11.15 | User's iMac |
| Webull environment | Sandbox | Sandbox configuration |
| Completed daily bars / price basis | PASS | PASS |
| Native 50-day volume | PASS | PASS |
| Price indicators | PASS | PASS |
| Completed minutes / price compatibility | PASS | PASS |
| EP volume source pair | PASS | PASS |
| Daily rows | 1,000 | 1,000 |
| Latest daily session | 2026-09-30 | 2026-09-30 |
| Current regular minute session | 2026-10-01 | 2026-10-01 |
| Check receipt UTC | 2026-10-01T16:00:30.120626+00:00 | 2026-10-01T16:16:05.331344+00:00 |
| First-30-minute volume | 13,988,359 | 4,554,863 |
| Prior-50-day average volume | 123,427,871.52 | 44,037,843.68 |
| Ratio | 0.11333225492536632 | 0.10343065462282416 |

Both import generations were READY. NVDA: 19 cash dividends plus 2024 split in
reviewed coverage. SPY: 19 cash dividends; empty split response retained. Both
checks reported setup eligibility NOT_EVALUATED. Ratios below 0.5 do not fail
data acceptance; they mean the EP volume threshold was not met.

SPY dated source receipts: SPLITS 2026-10-01T16:13:21.497475+00:00 (0 rows),
DIVIDENDS 2026-10-01T16:14:07.789713+00:00 (112 rows). The iMac initially lacked
usable TLS issuer trust. User installed certifi 2026.7.22 in its virtualenv,
configured SSL_CERT_FILE in the private local environment file (mode 0600),
verified HTTPS, then completed the API calls above. No SSL verification bypass.
No keys recorded here. The certificate setting references that virtualenv's CA
bundle and must remain available when launching the desk.

Current status: Step 06 closed for the scoped NVDA/SPY sandbox integration.
Latest local suites passed 508 tests on Python 3.12/3.14; Claude's Python 3.11
suite passed 508 in 4.77s. Taz supplied the iMac Python 3.14.7 strict-suite
result on 2026-10-01: **508 passed in 5.10s**. This completes the outstanding
host verification. No additional API requests were needed.

These checks do not activate a runner, evaluate full setups, attest additional
symbols/production access or place orders. The reviewed config remains dated to
2026-10-01. Step 07 has not started.
