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

`scan-log.jsonl` discovery fields report source labels, per-source failures and user
preparation. `watchlist-status.json` records the latest weekly attempt and last
successful refresh. READY and EMPTY are successful builds; EMPTY keeps core/user
names. PARTIAL and FAILED retain the previous list. UNKNOWN/INVALID or a last
success older than seven days is marked stale. Staleness labels coverage, while
each candidate still requires current, validated data before eligibility.

Plan B: keep the previous list with visible degraded status and independently
validate core/user names. An S&P/Nasdaq constituent fallback is not implemented.
The ranked lists are limited candidate pools, not exhaustive market coverage.

Provider acceptance remains separate: the new reference route has documentation
and synthetic tests, but its entitlement and payload on your configured host
still need a read-only check before activation. No paid access or live execution
is implied. See `checkpoints/08-discovery.md` for evidence and verification.
