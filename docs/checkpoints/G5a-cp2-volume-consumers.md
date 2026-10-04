# G5a checkpoint 2 — volume consumers and partial discovery

2026-10-04. Implementer: Claude (cloud container). Auditor: Astra. Requested by Taz
("Implement G5a Checkpoint 2: volume consumers and partial discovery", 06:31Z, relayed).
Trader-day steps: **watch** (discovery, liquidity) and **analyze/plan** (setup volume
checks, signal and ticket prerequisites). Nothing here is Astra's approval. Checkpoint 3,
Step 09, runner activation and iMac changes are not started.

## Before-code record (written before any code change)

### Baseline

- `codex/repair-step-01-baseline` at `df994721db017728b4dd5e0d8fd5d512babb7281`
  (Astra closed F1–F3 there). `git fetch` shows no newer upstream commit; the working
  tree is clean; no local changes. Checked 2026-10-04.
- Read: `AGENTS.md`, `CLAUDE.md`, `docs/REPAIR_PLAN.md` (G-steps, Step 17 volume
  repairs), `docs/ALPACA_VOLUME.md`, `docs/G5_ACCEPTANCE.md`,
  `docs/checkpoints/G5a-alpaca-volume.md`, the 01:47Z handoff and this prompt.

### Requirement (User policy, Taz 06:31Z)

Separately evidenced Alpaca SIP volume feeds the existing volume calculations; valid
discovery candidates publish when other candidates fail; saved signals and tickets
cannot keep using unavailable or revised volume qualification. Every trading rule,
threshold, weight, limit, sizing and approval policy stays as it is.

### Inventory of volume reads (Checked: `rg -n -i "volume|vwap"` over `src/desk`)

| # | Consumer | What it reads today | Dependency window | Decision? |
| --- | --- | --- | --- | --- |
| 1 | `watchlist.leader_scan` liquidity | `volume_basis(df[-50:])`, mean of Webull `volume` ≥ 1,000,000 | latest 50 completed sessions | yes (discovery gate) |
| 2 | `indicators.daily_features` `rel_volume` | Webull `volume ÷ rolling(50).mean()` (denominator includes the bar itself) | 50 sessions per point | **no decision consumer** (only `screen_check` display, `data_acceptance` diagnostic) |
| 3 | `triggers.minervini_vcp` dry volume | `volume_basis(f[-50:])`; 10-day mean < 0.7 × 50-day mean | last 50 sessions ending at the signal bar | yes |
| 4 | `triggers.oneil_cup_with_handle` light handle | `volume_basis(f[min(r+1, n-50):])`; handle mean < 50-day mean | last 50 sessions (handle ≤ 25 bars per card) | yes |
| 5 | `triggers.episodic_pivot` + `scanner.episodic_pivots` | Webull M15 first two bars vs Webull daily 50 mean, `compatible_volume`, float compare | 50 sessions before entry + 09:30/09:45 RTH bars | yes |
| 6 | `triggers.luk_reclaim` anchored VWAP | `volume_basis(f.loc[anchor:])`, Webull typical price × Webull volume | from the 63-bar low anchor | yes (level selection) |
| 7 | `indicators.session_vwap` | Webull price × volume | session | no consumer |
| 8 | `bar_contract.developing_daily_from_m15` | sum of Webull RTH M15 volumes (labelled developing) | today | RSI(2) only; `rel_volume` masked |
| 9 | `watchlist.movers` | Webull ranking field `relative_volume_10d` ≥ 2 | provider list | candidate source only; EP check decides |
| 10 | `watchlist.universe` | Webull `most_active` VOLUME/TURNOVER lists | provider list | candidate source only |
| 11 | Darvas `breakout_volume` 1.5, cup `breakout_volume` 1.4, VCP "rising volume" | card text/params only | entry bar | **not implemented** in any entry path (Step 10/17); text is not a check |
| 12 | `vendor_check`, `data_acceptance`, `screen_check` | Webull volume diagnostics | — | diagnostics only |
| 13 | `signal_state`, `revision_rebuild`, `risk_terms`, `tickets` | no volume today | — | must carry and re-check volume qualification |

### Contracts to add

- **Identity** (`alpaca_assets.py`): one documented read-only GET
  `https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity` per
  run, sharing the run's request budget, TLS on, redirects refused, error bodies never
  read, no account/order/position route. Persisted, versioned mapping per desk symbol:
  desk symbol, Webull symbol and instrument ID, Alpaca symbol and asset ID (separate
  fields), asset class, exchange, names, status, method (`exact-symbol` or an explicit
  class-share alias such as BRK.B), asof policy (omitted: Alpaca's current entity
  mapping), receipt and digest. Unresolved, ambiguous, conflicting, reused or changed
  identity makes that ticker's volume unavailable with the reason kept. A changed
  identity gets a new mapping version and cache namespace; windows that reach back
  before a detected change are `UNSUPPORTED_HISTORICAL_MAPPING`. A first pin relies on
  Alpaca's entity mapping, corroborated only by the Webull instrument's own price
  history covering the window (Assumption: not independent proof).
- **Decision volume** (`data_basis.AlpacaDecisionVolume`, new policy ids, never the
  Webull `webull-rth30/native-daily50-v1` label): attached to a Webull frame as a
  separate `attrs["decision_volume"]`; the Webull `volume` column and `volume_basis`
  stay untouched. Daily joined by NY session date; M15 by exact interval start.
- **Consumer view** (`data_basis.decision_window`): one source per comparison. With
  Alpaca configured the Alpaca view is the only volume source for rows 1–5 (no Webull,
  IEX or native fallback); without it, the existing Webull path is unchanged.
- **Signal evidence**: `Signal.volume_evidence` holds the used values, sessions or
  intervals, definitions, share basis, both identities and mapping version, request
  keys, snapshot digests, receipt, rule fingerprint, result and a dependency digest.
  Candidate identity hashes only the dependency (not receipts or snapshot digests).
- **Eligibility gate**: before trigger observation (cache only), signal revalidation
  (fresh refetch of the same keys), and the ticket's final fence (cache only, local).
  Unavailable → suspend; changed volume/source/share basis/identity/rule → invalidate
  and queue the existing rebuild.
- **Partial discovery**: `leader_scan_job` classifies each candidate's terminal outcome
  (selected, criterion, source failure, limit) and publishes READY / PARTIAL / EMPTY;
  FAILED (universe or SPY) and INCOMPLETE (failures, no leaders) retain the previous
  list. One atomic bundle (generation, digest, list, status) is the commit point; the
  derived `watchlist.json` and `watchlist-status.json` are repaired from it on read.
  SPY must reach the latest completed session at the real clock.

### Timing consequences (truthful availability; not chosen silently)

- Native-daily SIP includes extended hours; Checkpoint 1's Assumption treats it as
  final at the following ET midnight. At the 16:10 close scan today's native daily is
  not final, so VCP/cup volume is `DAILY_NOT_COMPLETED_AT_RECEIPT`; those names are
  re-prepared at the next session's first slot (existing preparation path), with the
  same price terms. Price-only setups arm at 16:10 as before.
- The Friday 16:40 build's liquidity window ends at the latest session whose native
  daily is final (Thursday at 16:40; Friday on a weekend preview). Disclosed per build.
- Free historical SIP needs a 15-minute-old end, so EP's 09:30/09:45 bars are usable
  from 10:15 ET. The 10:00 mover EP check reports them unavailable; user EP picks retry
  at later slots through the existing pending path. No zero-delay entitlement is assumed.

### Acceptance cases

The prompt's ten regression groups (section 10), mapped to tests in the final section,
plus a deterministic replay of the saved 365-name artifacts and one bounded read-only
preview (at most six Alpaca HTTP calls, scratch cache).

### Rollback

Unset `DESK_ALPACA_VOLUME_CACHE` (default): no provider, every consumer takes the
unchanged Webull path. Code rollback: revert this checkpoint's commit; the new tables
and files (`watchlist-build.json`, identity tables) are additive and ignored by the
previous code, which keeps reading `watchlist.json`.
