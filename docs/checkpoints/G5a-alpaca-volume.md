# G5a — Alpaca SIP volume producer and acceptance probe (checkpoint 1)

2026-10-04. Implementer: Claude (cloud container). Auditor: Astra. Requested by Taz
("Claude handoff: G5 volume and discovery follow-up", 01:47Z): implement checkpoint 1
only, verify, push and stop for Astra's audit. **Status:** Astra's audit of `2d97ea6`
(2026-10-04 06:07Z relay) did not sign off; her findings F1–F3 are repaired in the
audit-closure commit (section at the end), pending her re-audit. Trader-day step: **watch**. Nothing here
is Astra's approval. Checkpoints 2 and 3, Step 09 and any iMac change are not started.

## Before-code record

- **Base:** `codex/repair-step-01-baseline` at `d22bd03af1758cb0b558414da020936d3c5c168f`
  (the audited reference; no later commits, clean tree). Baseline strict suite:
  1142 passed on Python 3.12.3 and 3.13.14 (cloud).
- **Requirement:** an opt-in, read-only Alpaca historical SIP volume client with typed
  evidence and a CLI probe; separate from Webull price/OHLCV evidence; no consumer
  changes (User policy, handoff "Preparation and scope").
- **Contract:** `docs/ALPACA_VOLUME.md` (route, headers, feed, adjustment, paging,
  stop codes, budget, identity, channels, definitions, comparison policy, cache).
- **Producers:** `desk.alpaca_volume.AlpacaVolumeClient` only.
- **Planned consumers (checkpoint 2, untouched now):** discovery 50-day liquidity,
  daily relative volume, setup volume comparisons, EP RTH30/native-D50, stored
  qualification evidence, scanner refresh/revalidation, revision rebuild, tickets.
- **Research:** the four links in the handoff, restated with labels in the contract.
- **Acceptance cases:** the handoff's test list (mapped below) plus one bounded
  provider run for NVDA/SPY/QQQ/AAPL, replay entry session 2026-10-02.
- **Rollback:** delete the two modules, their test file and `docs/ALPACA_VOLUME.md`;
  revert the doc and `.env.example` edits. No other code imports the module.
- **Unresolved (not chosen silently):** independent provider identity (Alpaca's asset
  id lives on the trading host, outside this component's allowed route); current-
  session timeliness under Taz's actual entitlement; how checkpoint 2 maps this
  evidence into `data_basis.VolumeBasis`.

## What changed

| File | Change |
| --- | --- |
| `src/desk/alpaca_volume.py` | New: request model, `VolumeObservation`, `BatchResult`, `RequestBudget`, `AlpacaVolumeClient`, `VolumeCache`, `ComparisonPolicy`, `EPVolumeComponent`, `ep_volume_component`, `revalidate`, `evaluate` |
| `src/desk/alpaca_probe.py` | New: `python -m desk.alpaca_probe` (two batch queries, six-request cap, sanitized `result.json` and raw pages) |
| `tests/test_alpaca_volume.py` | New: 52 tests, synthetic fixtures, no network |
| `docs/ALPACA_VOLUME.md` | New: source contract, consumers, Plan B, rollback |
| `docs/checkpoints/G5a-alpaca-volume.md` | This record |
| `docs/G5_ACCEPTANCE.md`, `docs/checkpoints/G5-combined-verification.md` | G5a rows and status; earlier G5/iMac evidence unchanged in scope |
| `CLAUDE.md`, `.env.example` | Pointer to this checkpoint; `APCA_*` names with empty values |

Unchanged on purpose: `data_basis.py` (no new accepted volume policy), scanner,
watchlist, triggers, tickets, cards, iMac files, and the Webull cross-split refusal
test (`tests/test_native_volume.py`).

## Tests mapped to the handoff (Acceptance 1–2)

| Handoff case | Test(s) in `tests/test_alpaca_volume.py` |
| --- | --- |
| Complete reply | `test_complete_reply_gives_each_ticker_a_volume_component` |
| Paginated reply | `test_paginated_reply_is_followed_and_joined` |
| Missing one symbol | `test_missing_symbol_is_isolated` |
| Duplicate / non-finite data | `test_malformed_ticker_does_not_erase_healthy_tickers` (11 variants) |
| Wrong symbol / feed / channel | `test_unrequested_symbol_is_never_attributed_to_a_requested_ticker`, `test_request_model_refuses_other_feeds_limits_and_bad_symbols`, `test_observation_refuses_inconsistent_labels_or_digest`, `test_pre_market_bar_is_a_wrong_channel_row_for_rth`, `test_off_grid_rth_bar_is_a_wrong_channel_row`, `test_wrong_channel_and_identity_are_refused` |
| Incompatible raw/split | `test_raw_and_split_evidence_never_share_a_basis` |
| Stale observation despite fresh receipt | `test_stale_observations_fail_despite_a_fresh_receipt`, `test_receipt_before_the_intervals_complete_is_refused` |
| Failed auth/entitlement | `test_auth_entitlement_and_rate_limit_stop_the_run` (401, 403, 429) |
| Truncation | `test_remaining_pagination_is_incomplete_not_a_truncated_success`, `test_probe_never_exceeds_six_requests`, `test_pagination_loop_is_refused` |
| Same-content cache reuse | `test_same_content_is_reused_without_a_request` (the `2d97ea6` reuse path was defective: F1, F2 below) |
| Changed content | `test_changed_content_is_a_revision_and_invalidates_an_earlier_result` |
| 49 versus 50 sessions | `test_exactly_50_sessions_pass_and_49_fail`, `test_gap_inside_the_window_is_not_zero_filled` |
| Entry-day exclusion | `test_entry_session_row_is_excluded_from_the_baseline` |
| DST (and holidays, shortened session) | `test_dst_change_and_holidays_use_exchange_sessions`, `test_shortened_session_rows_after_the_early_close_are_wrong_channel` |
| 0.5 boundary | `test_threshold_boundary_is_exact_decimal` |
| Malformed ticker keeps healthy ones | parametrized malformed test, `test_missing_symbol_is_isolated`, `test_failures_are_kept_per_ticker_in_the_cache` |
| Known split, raw vs split (offline) | `test_known_split_raw_versus_split_adjusted_offline_fixture` (NVDA 10-for-1, 2024-06-10; synthetic volumes) |
| Secrets | `test_requests_use_documented_headers_host_and_explicit_sip`, `test_credential_echo_is_refused_and_not_saved`, `test_probe_writes_sanitized_evidence` |
| Not wired into consumers | `test_producer_is_not_wired_into_any_consumer` |

No real Alpaca split-window check was run; the split case is offline only.

## Provider acceptance [P] (cloud, historical replay, 2026-10-04 01:57Z)

Command (through the production client, real keys, real receipt clock):

```
python -W error -m desk.alpaca_probe --symbols NVDA,SPY,QQQ,AAPL --entry-session 2026-10-02 \
  --daily-start 2026-07-01 --out <evidence> --cache <evidence>/volume-cache.sqlite
```

The two module blobs used are recorded in `source-blobs.txt` and equal the committed
files. Evidence (project folder):
`research/ai-trading/alpaca-volume-checkpoint1-2026-10-04/` (`REPORT.md`,
`result.json`, `raw/`, `volume-cache.sqlite`, `independent_check.py/.json`).

| Request | Bounds (UTC) | HTTP | Pages | Status |
| --- | --- | --- | --- | --- |
| D `1Day` split sip | 2026-07-01T04:00:00Z – 2026-10-02T03:59:59Z | 200 | 1 (`next_page_token` null) | COMPLETE, 65 rows per ticker, no missing or non-session rows |
| RTH30 `15Min` split sip | 2026-10-02T13:30:00Z – 13:59:59Z | 200 | 1 | COMPLETE, 2 rows per ticker |

Two HTTP requests of the six allowed. No auth, entitlement or rate-limit stop.

| Ticker | First 30 min (09:30 + 09:45) | Prior 50 sessions (07-23 … 10-01) mean | Ratio | ≥ 0.5 |
| --- | --- | --- | --- | --- |
| NVDA | 30,116,098 | 122,612,769.06 | 0.245620 | no |
| SPY | 5,310,035 | 44,321,701.22 | 0.119807 | no |
| QQQ | 4,675,689 | 35,837,406.62 | 0.130470 | no |
| AAPL | 3,615,028 | 46,022,426.40 | 0.078549 | no |

All four volume components are available; none meets 0.5, which is expected on an
ordinary day for these names and says nothing about any setup. An independent
recomputation from the saved raw pages (stdlib + exchange_calendars, no desk import)
gives the same numbers. The six SPY/QQQ September sessions that overlap the earlier
`adjustment=raw` sample are identical under `split` (no split in that window; this is
consistency, not proof of corporate-action coverage).

## Verification (cloud container)

| Python | `tests/test_alpaca_volume.py` | `python -m pytest -q -W error` |
| --- | --- | --- |
| 3.12.3 | 52 passed | **1194 passed** (baseline 1142 + 52) |
| 3.13.14 | 52 passed | **1194 passed** (baseline 1142 + 52) |

`git diff --cached --check`: clean. `tests/test_native_volume.py` (Webull cross-split
refusal) unchanged: 16 passed. A first 3.13 run flagged a `ResourceWarning` from an
unclosed SQLite connection in one new test; the test now closes it and both versions
pass strict.

## Remaining failures and limits

1. **Identity:** symbol-level only; no independent provider identity. Checkpoint 2
   must decide how Alpaca rows are tied to the Webull instrument before any consumer.
2. **Historical only:** this run proves bounded historical SIP access with the
   present keys. It proves nothing about real-time entitlement or current-session
   timeliness; no zero-delay label is stamped. A current-session acceptance under
   Taz's actual plan is still needed before any live use.
3. **No real split-window check** through Alpaca; the split case is an offline fixture.
4. **Daily coverage differs from RTH:** the daily bar is the provider's own aggregate
   (observed to include extended hours). The comparison is directional by design.
5. **Discovery is still blocked** by the d22bd03 non-volume failures (14 ambiguous
   identities, 82 price/history failures, whole-universe publication veto). That is
   checkpoints 2 and 3.
6. Not run on the iMac or Python 3.14.

## Assumptions for Taz

- The extended-hours daily aggregate is final by the ET midnight after the session
  (used to refuse earlier daily receipts).
- Alpaca's default symbol mapping (no `asof`) is acceptable for these four tickers'
  2026 history; any rename would need an explicit identity check.

## Audit closure (Astra's audit of 2d97ea6, 2026-10-04)

Astra reproduced three defects with synthetic probes against `2d97ea6`; Claude
reproduced all three with her script before changing code (same outputs). Nothing
else in checkpoint 1 changed: no thresholds, consumers, risk, tickets or provider calls.

| Finding | Defect at `2d97ea6` | Fix | Regressions (`tests/test_alpaca_volume_audit.py`) |
| --- | --- | --- | --- |
| F1 (P2) | `fetch(reuse=True)` returned cached data before checking `self.stopped`, so a 401/403/429 stop could be bypassed | The active run stop is checked first on every path; a stop is also persisted as a `STOP` event, so after a restart every earlier success is ineligible until that key is refreshed successfully | `test_f1_active_stop_refuses_cached_daily_and_rth` (401, 403, 429; daily and RTH caches populated), `test_f1_recorded_stop_survives_restart_until_a_successful_refresh` (×3) |
| F2 (P2) | Failures were logged but `current()` ignored them; after a restart an earlier good snapshot came back `COMPLETE` | Append-only `events` log (`OK`/`FAILED`/`STOP`) ordered by sequence; eligibility = latest event for the ticker/key is `OK` and no later `STOP`. Unchanged successful refreshes add `OK` and restore eligibility. Snapshots are kept. `reuse=True` is cache-only (never sends a request) and reports each ticker's reason | `test_f2_failed_refresh_is_not_forgotten_across_restart_until_recovery` (healthy → bad SPY → reuse → restart/reuse → unchanged recovery at the same receipt clock → reuse), `test_f2_missing_ticker_on_refresh_stays_unavailable`, `test_f2_request_level_failure_is_persisted` (transport, HTTP), `test_f2_identity_change_is_persisted`, `test_f2_reuse_never_sends_a_request`; auth/entitlement covered by the F1 tests |
| F3 (P2) | `ep_volume_component(threshold=...)` let a caller relax 0.5 under the same policy id; `revalidate` trusted the stored threshold | No threshold parameter; `approved_rule()` reads the EP card's `early_volume` and fingerprint at every calculation and revalidation. The component records `rule_version`; its validator rejects a threshold other than the approved one and a pass flag that does not follow from its numbers. `revalidate` refuses a changed rule (`VOLUME_RULE_CHANGED_REQUALIFY`) or a result that differs from a fresh calculation (`VOLUME_RESULT_MISMATCH`). The 0.5 rule is unchanged | `test_f3_caller_cannot_supply_a_threshold`, `test_f3_unsupported_threshold_cannot_claim_the_policy` (0.001, 0.9), `test_f3_stored_altered_result_is_rejected_on_revalidation`, `test_f3_approved_rule_change_requires_fresh_qualification`, `test_f3_legitimate_boundary_unchanged` |

All 18 regressions fail on `2d97ea6` (run against a checkout of that commit) and pass on
the repair. `test_f3_legitimate_boundary_unchanged` is a control: on `2d97ea6` it fails
only because `rule_version` did not exist; its 0.5 boundary assertions held there too.
Astra's script on the repaired tree: F1 `reuse_status=UNAVAILABLE`,
`all_volume_components_available_again=false`; F2 `reuse_status=PARTIAL`,
`reuse_failures={SPY: NONFINITE_OR_NEGATIVE_VOLUME}`; F3 the call with `threshold=`
raises `TypeError`.

Provider evidence: the 2026-10-04 01:57Z historical run keeps its stated scope. It
exercised fresh fetches only (no reuse path), and its `volume-cache.sqlite` predates the
event log, so none of it is eligible under the repaired cache. No new provider call was
made for this closure.

### Verification (cloud container, audit-closure commit)

| Python | `test_alpaca_volume.py` + `test_alpaca_volume_audit.py` | `python -m pytest -q -W error` |
| --- | --- | --- |
| 3.12.3 | 70 passed | **1212 passed** (1194 + 18 regressions) |
| 3.13.14 | 70 passed | **1212 passed** |

Regressions against a `2d97ea6` checkout: 18 failed. `git diff --cached --check`: clean.
Not run on the iMac or Python 3.14. Nothing here is Astra's approval.
