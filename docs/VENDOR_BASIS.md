# G3: automatic Webull price consistency

This opt-in source lets supported common shares and ETFs reach price-only setup
checks without a daily hand-reviewed corporate-action file. It does **not** certify
a complete corporate-action feed, volume comparability or trade eligibility.
The scanner is not activated by installing this change.

## What is verified

- Resolve fresh Webull security metadata for requested tickers, in existing scanner
  batches. Persist the instrument/currency/exchange/type identity. An identity
  change or conflicting alias requires reconciliation, not a silent replacement.
- Request up to 1,000 completed daily bars, ending at the latest completed session
  so a forming bar cannot displace the oldest retained row. Reject gaps and stale
  history. Require a complete RTH M15 session for that daily session, including
  exchange-calendar early closes.
- Compare the latest daily close with the final raw RTH close. Do not require
  daily open/high/volume to equal the RTH aggregates. Never infer an adjustment
  factor from a large gap or rewrite supplied OHLCV.
- Preserve the exact daily OHLCV used to arm each signal. Each intraday refresh
  compares against that original reference, even after the latest cache advances.
  Changed historical prices **or volume** retire the old candidate and its events.
  A real gap with unchanged prior history does not trigger this revision rule.
- Fresh setup detection through normal preparation/close scanning can rebuild
  from the changed history. Invalidation does not invent new levels immediately.
  Re-fetching unchanged history alone does not replace candidate/event identity.
  A replacement cannot reuse crossings through the revision-detection time.
- Bad rows attributable to one ticker are omitted from Webull's partial batch
  interface. Healthy peers continue. Duplicate identities, missing data and delayed
  bars remain failures for the affected ticker. Unattributable payloads/provider
  errors fail the shared request; no per-symbol retry storm follows that failure.
  The original strict Webull interface remains strict.

Missing original history suspends revalidation; it does not prove a split and does
not make an old signal safe. Original candidate history stays in its signal record;
SQLite also retains content-addressed snapshots and refresh/exception observations.
Switching between legacy and vendor evidence requires fresh detection.

## Evidence and limits

**Sourced:** [Webull historical-bars documentation](https://developer.webull.com/apis/docs/reference/historical-bars/)
states that daily-and-above bars are forward-adjusted and minute bars unadjusted.
**Checked, bounded prior observations:** [Step 06 channel evidence](evidence/step06-native-channels.md)
and the off-hours report corroborate six NVDA/SPY adjustment observations and ten
AAPL/SPY daily/raw close pairs. They do not prove universal adjustment coverage.
The viaNexus split dataset had dividend records and conflicting dates; it is not
selected as an automatic local fallback.

**Engineering assumptions:** absolute close tolerance 0.000001 (six-decimal prior
observations), bar receipt age at most 60 seconds, metadata age at most 300 seconds
(the existing client cache lifetime). These are operational consistency checks,
not trading thresholds, an independent price feed, or evidence of profitability.
A provider error consistently reflected in both channels may escape these checks.

A daily/raw mismatch stays unavailable for that ticker. A configured current
reviewed ledger may supply diagnostic action evidence for reconciliation, but an
old review cannot override contradictory prices. Resolving a mismatch requires
source evidence explaining it and a separately verified reconciliation; G3 does
not implement an economic adjustment calculator. The legacy reviewed path remains
available separately, with its original constraints.

Volume is separate. Existing current, instrument-matched ledger/channel evidence
can still support its bounded volume window. After a detected history revision,
that review must be newer than the revision observation before it is reused.
Otherwise volume-dependent checks
remain unavailable. Price-only checks continue; EP/VCP and other volume-dependent
checks are not relabelled as passing. Automatic whole-watchlist volume evidence is
still outstanding. Shared SPY/QQQ market prerequisites also retain their existing
behavior; ticker isolation does not remove those dependencies.

## Read-only iMac verification

From the existing checkout and activated environment, after pulling the repair
branch and loading the already-saved credentials:

```sh
git pull --ff-only origin codex/repair-step-01-baseline
source .venv/bin/activate
source "$HOME/.config/trading-desk/env"
python -m pytest -q -W error
mkdir -p "$HOME/Desktop/g3-check"
python -m desk.vendor_check \
  --database "$HOME/Desktop/g3-check/vendor.sqlite" \
  --symbols NVDA SPY QQQ AAPL \
  --output "$HOME/Desktop/g3-check/result.json"
```

The expected suite count was **892 passed** at G3; at the G5 checkpoint it is
**997 passed**; after the audit closure it is **1053 passed** (see
`checkpoints/G5-combined-verification.md`).
No Alpha Vantage call/key is required for the primary price test. The CLI writes
only its chosen local audit database/report. It neither exports a persistent
scanner setting nor runs scans, creates signals or places orders. It supports an
open-session check after the first completed M15 bar, or an off-hours check of the
latest completed session. The output explicitly labels which was tested.

`PASS` means the requested price/history checks passed. Read `volume_window`
separately; `action_coverage: NOT_ATTESTED` is intentional. A price mismatch,
provider error or unavailable symbol returns `INCOMPLETE` and a reason. Preserve
the report for review instead of repeatedly retrying a provider failure. Local
synthetic tests cannot substitute for this actual-host report.

After host acceptance, `DESK_VENDOR_BASIS_DB` selects this source in the existing
scanner configuration. Leave it unset until that acceptance and runtime setup.
Optional `DESK_ACTION_LEDGER`/`DESK_ACTION_CHANNELS` retain their separate reviewed
volume/diagnostic role. A malformed optional review is reported without disabling
the automatic price path. No populated runtime configuration is committed here.

Rollback: unset the opt-in setting or revert G3, retain the audit files, and rebuild
candidates under the selected source. Do not delete identity pins to force a ticker
through a conflict; reconcile that identity first.

G3 follow-up, implemented separately from Claude's G4/G5: see
[G3_FOLLOWUP.md](G3_FOLLOWUP.md) for targeted revision rebuild, optional ordinary
cash/split anchor reconciliation and automatic batch split-window volume evidence.
Its local fixtures do not establish new-source access or operational acceptance;
the documented read-only host checks remain required. Existing profiles/settings
are unchanged, and `action_coverage` remains `NOT_ATTESTED`.
