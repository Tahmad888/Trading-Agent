# Discovery and watchlist coverage

Step 08 separates where a ticker came from from whether it qualifies for a setup.
It does not approve or execute trades.

| Source | When considered | Purpose |
| --- | --- | --- |
| Leadership | Friday after close | Existing strength/Trend Template ranking |
| Movers | 10:00 ET EP check and daily close | Existing gap/activity candidates |
| Bearish | Daily close | Negative DAY_1, DAY_5 and MONTH_1 loser candidates |
| Core ETFs | Every watchlist | SPY, QQQ and IWM; market context / appropriate setups |
| User additions | Next scheduled scan | Prepare on current completed data without waiting for Friday |

Put user changes in `taz-picks.json` in the scanner data directory:

```json
{"add": ["BRK.B", "NVDA"], "remove": ["EXAMPLE"]}
```

A removal wins over other sources except the three mandatory core ETFs. Changes
apply at the next scheduled slot; rerunning an already completed slot remains
idempotent. A removed ticker's existing events cannot pass fresh review. Daily
and EP user preparation are tracked separately for the session, so a pre-10:00
addition still receives its opening-volume check at or after 10:00. Failed data
preparation stays pending. A healthy setup rejection does not count as failure.

Security reference metadata supplies common-stock/ETF type and instrument ID;
unknown identities are skipped instead of guessed from spelling. Stock discovery
does not require options to exist or be liquid. Short discovery does not imply
borrow access. Reference responses are cached in-process for five minutes.

Daily requests still ask for up to 1,000 warm-up bars. Required history depends on
the setup: EP uses 60 prior completed daily sessions, including its 50-day volume
baseline; RSI(2) needs 200; template-dependent setups need at least 252. Other
setup windows and selected historical indicator values must also be complete.
Missing history for one setup is reported without blocking unrelated setups.

The Friday leader build is the one exception (G5a checkpoint 3,
`checkpoints/G5a-cp3-scoped-history.md`). Its reads are finite, so it asks Webull only
for the 260 completed sessions it uses (plus 5 earlier sessions that prove a missing
first session is a gap, not a young listing), under the typed scope in
`src/desk/history_scope.py`. A malformed row before that window is recorded as
`EXCLUDED_OUTSIDE_SCOPE` and no longer stops the ticker's ranking; a defect, gap or
stale end inside the window still rejects that ticker, and a bad required SPY row still
fails the build. Discovery evidence is labelled `webull-discovery-v1`: it cannot arm,
feed setups or revalidate a signal, and it never replaces full-history evidence. Every
setup, the market filter, entries and revalidation keep the strict 1,000-row request,
and the same old row still rejects them, now with its session, field and reason. An
adapter without scoped requests falls back to the unchanged full path and the build
report says so (`history_scope.path`).

After Astra's audit of that checkpoint, a reply that starts after the window's first
session is never taken as a young listing by itself. If any earlier accepted capture of
the same instrument (full or discovery) started earlier, the reply is incomplete; if no
accepted uncapped full-history capture shows the history starts there, coverage is
unverified. Both are per-ticker source failures, so a clipped reply cannot remove a
leader by making it look new. When every candidate fails its source data, the build is
FAILED and the previous list stays, with its age shown. Boolean OHLCV values and
duplicate rows are rejected inside the window and recorded outside it.

`scan-log.jsonl` discovery fields report source labels, per-source failures and user
preparation. `watchlist-status.json` records the latest weekly attempt and last
successful refresh. READY and EMPTY are successful builds; EMPTY keeps core/user
names. PARTIAL and FAILED retain the previous list. UNKNOWN/INVALID or a last
success older than seven days is marked stale. Staleness labels coverage, while
each candidate still requires current, validated data before eligibility.

Plan B: keep the previous list with visible degraded status and independently
validate core/user names. An S&P/Nasdaq constituent fallback is not implemented.
The ranked lists are limited candidate pools, not exhaustive market coverage.

Provider verification: Taz's iMac reports confirmed NVDA/SPY metadata and daily-bar
identity matches on 2026-10-01. Berkshire metadata was returned only for `BRK B`,
ID 916040668. The desk now accepts `BRK.B`, `BRK-B` and `BRK B` as this one reviewed
identity, stores `BRK.B`, and requests `BRK B` from Webull. Other share classes are
not silently rewritten. A different/missing instrument ID rejects the mapping.

After pulling the repair on the credentialed host, run:

```bash
python -m desk.metadata_check --symbols NVDA SPY BRK.B
```

This probes metadata plus D/M15 identities, with no price profiles, scanner or
orders. The repaired NVDA/SPY/BRK.B paths passed on the iMac sandbox connection
on 2026-10-01; see `evidence/step08-metadata-imac.md`. The probe prints the host
and suppresses raw exceptions. This does not establish production-host access.
No paid access or live execution is implied. See `checkpoints/08a-webull-symbols.md`.
