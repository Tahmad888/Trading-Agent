# G5a parent Checkpoint 3, child Step 3 — quote audit repairs

Hierarchy: G5 → G5a parent Checkpoint 3 → child Step 3 (code review, repairs and
independent verification). The repair packages A–C below are internal to child Step 3.
Trader-day steps served: **watch** (current quotes), **approve** (independent ticket
revalidation). Implementer: Claude. Auditor: Astra. Nothing here is Astra's sign-off.

## Attribution and history

- Child Step 2 (quote adapter, `78da588972406d4d21a1cfacea42151785e6ad1b`, parent
  `b747904ac13c5f95c89bcc3cd6b9e40519c2b573`) was implemented by Astra.
- Claude did not commit code or requirements for child Step 2. Claude started reading
  the interfaces for the original prompt; Taz then asked Claude to stop and let Astra
  build it. The earlier record's "usage pause" and "wrote requirements" wording is
  corrected here.
- Claude's independent review of `78da588` (13 synthetic stress tests, 18 targeted
  mutations, full strict suites on Python 3.12.3 and 3.13.14: 1475 passed each) found
  the defects below. Astra reproduced F1–F8 with production classes, synthetic provider
  replies and scratch stores (her probes were a temporary script, not a committed file).

## Before-code record (written before any code change)

Baseline: `codex/repair-step-01-baseline` at `78da588`, fetched 2026-10-05; no newer
commit, clean tree. Read: `AGENTS.md`, `CLAUDE.md`, `docs/REPAIR_PLAN.md`,
`docs/G5_ACCEPTANCE.md`, `docs/TASTYTRADE_QUOTES.md`, the child-2 checkpoint,
`docs/RISK_TERMS.md`, `docs/TICKETS.md`, and the code and tests they name.

### Evidence labels used here

**Sourced** (primary documents, checked 2026-10-05):

- [dxLink specification](https://github.com/dxFeed/dxLink/blob/main/dxlink-specification/asyncapi.yml),
  `FeedConfigMessage`: "The server can send this message to the client after receiving
  the `FEED_SETUP` message. The server can send this message to the client if the
  `FEED` service configuration has changed. Parameters are lazy therefore the server
  may not send the notification immediately, but before the first `FEED_DATA` is sent."
  `eventFields` is optional; `FeedEventFields` is "an object where keys are event types".
  The specification's own example subscribes first and then receives a `FEED_CONFIG`
  carrying only the `Quote` map, immediately before Quote data. Connection section:
  each side must send some message before the other side's `keepaliveTimeout`, else the
  other side closes. `ERROR`: "does not require any action and is for informational
  purposes only" (this desk still treats it as a reason to withhold evidence; see A).
- [dxFeed Quote](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Quote.html):
  bidTime/askTime are the "time of the last bid change"/"ask change", "by default …
  transmitted with seconds precision". An old side-change time is not delay evidence.
- [dxFeed Trade](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Trade.html):
  `time` is the time of the last regular-trading-hours trade; extended-hours trades do
  not update it; after the daily reset the RTH values stay until the next RTH trade.
  The page does not state an unknown-value convention for size; NaN is treated as
  unavailable (engineering choice below).
- [dxFeed Profile](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/Profile.html)
  and [TradingStatus](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/market/TradingStatus.html):
  `tradingStatus` ∈ {UNDEFINED "undefined, unknown or inapplicable", HALTED, ACTIVE};
  `haltStartTime`, `haltEndTime`. Documentation does not prove this account receives it.
- [tastytrade streaming guide](https://developer.tastytrade.com/docs/guides/stream-market-data/)
  and [AsyncAPI](https://developer.tastytrade.com/asyncapi/tastytrade-streaming.asyncapi.json):
  quote token, returned endpoint, streamer symbols, ~30 s keepalive, 24 h token.
- [tastytrade instruments OpenAPI](https://developer.tastytrade.com/openapi/instruments.json):
  `Equity` exposes `cusip`, `description`, `listed-market`, `is-etf`,
  `instrument-sub-type`, `streamer-symbol`, `active`.
- Webull: this repo's Step 06 evidence (`docs/evidence/step06-native-channels.md`)
  records that the NVDA/SPY crosswalk was a reviewed name/listing/symbol match and is
  "not an assertion that Webull shares CUSIP/FIGI fields". `SecurityMetadata` carries
  instrument ID, name, sub-category, exchange code and currency only.
- [FINRA Rule 6820](https://www.finra.org/rules-guidance/rulebooks/finra-rules/6820):
  50 ms industry-member clock tolerance — context only, not a desktop tolerance.

**Checked**: Astra's reproductions F1–F8 and Claude's stress tests on `78da588`.

**Engineering assumptions** (not trading rules):

- E1 Field maps merge per event type: a later `FEED_CONFIG` replaces only the types it
  names; an absent type keeps its previous map. The specification shows per-type
  delivery but does not define merge versus replace.
- E2 A changed map for a type withholds that type's existing observations until a new
  event decodes under the new map (no new generation; no ticket-version churn).
- E3 Profile runs on its own FEED channel so an error or closure there withholds only
  Profile. A channel-0 or Quote/Trade-channel `ERROR` still withholds everything.
- E4 Trade size NaN or missing is "size unavailable"; price and time stay usable.
  Boolean, negative or infinite size is corrupt and rejects the row.
- E5 A quote whose two side-change times move in opposite directions is ambiguous and
  withheld until a snapshot at or after both retained watermarks, or a new generation.
- E6 No clock-skew tolerance is adopted. Future source times stay ineligible; the
  diagnostic reports the lead with a clock-uncertainty explanation.

### Requirements, producers and consumers

| Item | Producer → consumer | Requirement |
| --- | --- | --- |
| F1 lazy config | `Session` → `FeedDecoder` | Subscribe after `CHANNEL_OPENED` without waiting for a map; decode only types with an accepted map; bounded schema deadline |
| F2 re-config | `Session` → `FeedDecoder` → `QuoteService` | Identical config harmless; changed order adopted before later data and withholds that type's old values; invalid map withholds that type; teardown when no usable Quote/Trade map remains |
| F3 clock | `QuoteService.feed` → diagnostic | Future source time stays ineligible; diagnostic shows signed lead/lag, precision, explanation; `sntp` check documented |
| F4 quote age | `QuoteService.quote` → diagnostic | Side-change age labelled as exceeding policy, never as delay proof |
| F5 mixed order | `QuoteService.feed` | Withhold symbol's Quote with `QUOTE_ORDER_AMBIGUOUS`; watermark recovery |
| F6 reserved label | `risk.evaluate` | `tastytrade-dxlink` without provenance fails a blocking check; with provenance: source, environment, symbol and mapping digest checked |
| F7 identity | new `quote_mapping` → `TastytradeRiskSource` → `revalidate_signal` | Reviewed Webull↔tastytrade mapping verified before any signal-state write; bound into provenance; rechecked in the final fence |
| F8 lifetime | tickets final check | Keep generation binding; actionable refusal naming the session change; long-lived shared service is a documented future design |
| F9 halts | Profile → `QuoteService.status` → bridge | Known HALTED refuses before revalidation and in the final fence; UNDEFINED/missing is unknown, never ACTIVE |
| F10 recovery | `capture` | Bounded heartbeat-timeout recovery within the existing attempt/time/request budget; auth/schema/rate-limit/token expiry stop; report faults and recoveries |

Unchanged consumers: Webull history/revision/rebuild, Alpaca SIP decision volume and
its guards, EP card/50 sessions/0.5, scoped discovery, risk quote age 60 s, budgets,
sizing, warnings, stop/chase/entry rules. No tastytrade Candle or volume is requested.

### Accepted and refused examples (written first)

1. Server sends a fieldless config, waits for the subscription, then sends the Trade
   map and data → subscription sent once, SPY trade decoded at the right price/time.
2. Early map → unchanged behaviour. Identical second config → no change.
3. Second config with Trade fields reordered → later data decodes at the new positions;
   the earlier Trade for SPY is withheld until a new event arrives.
4. FEED_DATA of a type with no accepted map → counted as undecodable, no observation.
5. Quote/Trade map missing a required field → that type withheld; the session stops
   with `FEED_SCHEMA_UNSUPPORTED` only when no Quote/Trade map remains usable or the
   format is unsupported (refined during implementation, see the record below).
   Profile map invalid → Profile withheld only.
6. Ticket request labelled `tastytrade-dxlink` with a legacy (non-tastytrade) terms
   source → prepared as blocked, approve and consume refused; label never verified.
7. Honest legacy label with a legacy source → works as before.
8. No reviewed mapping, a mismatched Webull instrument, a changed tastytrade CUSIP or
   streamer symbol → `QUOTE_MAPPING_*` unavailable before revalidation; signal state,
   levels and expiry unchanged; peers continue.
9. Reviewed mapping present and matching → existing setup rules run (a legitimate gap
   is not refused by identity logic).
10. Mapping removed or tastytrade identity refreshed between recheck and commit →
    final write refused.
11. Mixed quote times → withheld; later snapshot at/after both watermarks → available.
12. Trade +1 ms future → ineligible with clock-uncertainty explanation in diagnostics.
13. Profile HALTED → resolve refused before revalidation; cached young trade cannot pass
    the final fence.
14. Heartbeat timeout with a retry left → old values cleared, fresh token and
    generation, new events required; auth/429/schema faults never retried.
15. Two independent quote services (new process) → refusal that says the quote session
    changed and a new version is needed.

### Open decisions (not decided here)

- Clock tolerance/calibration: none adopted (E6). Proposal is recorded in
  `docs/TASTYTRADE_QUOTES.md`; adopting one needs Astra/Taz approval.
- Corrected or busted prints: last-trade stop/entry/chase rule unchanged; how a later
  bust should treat a signal invalidated by that print is an open policy/data item.
- Long-lived shared quote service (daemon, IPC, token renewal): future design.
- Operational reviewed mappings for watchlist names: pending Taz's review per name.

### Rollback

Code-only change; no host state. Revert this commit to return to `78da588`. A reviewed
mapping file (if any is later created) is ignored by older code. Ticket rows prepared
with mapping-bound provenance stay as history; older code does not reinterpret them.

## Implementation record (2026-10-05)

Zero provider calls, zero iMac changes, no approvals, orders, schedules or mappings for
real names. All provider data in tests is synthetic and labelled.

### Changed files

| File | Change |
| --- | --- |
| `src/desk/tastytrade_quotes.py` | Per-type field maps (E1/E2), Profile observations and `status()`, optional Trade size, distinct no-bid/missing codes, ordering watermark (E5), future-time lead evidence, separated `inspect()` views, `identity()`/`last_trade()`/`withhold()` |
| `src/desk/tastytrade_transport.py` | Subscribe on channel open; config changes while streaming; Profile on channel 5 (E3); schema deadline; `SESSION_NOT_CONNECTED`/`SESSION_SUPERSEDED`; ERROR codes; bounded heartbeat/transport recovery with fault and recovery reports; `stream_endpoint_allowed` (also in `connect_ws`); injectable opener; `select_pair` (ET market date) |
| `src/desk/quote_mapping.py` (new) | Reviewed Webull↔tastytrade mapping records, store, verification and interactive review command |
| `src/desk/quote_risk.py` | Mapping → halt → trade verified before revalidation and again in the final fence; fixed-code refusal reasons |
| `src/desk/risk_terms.py` | `EvidenceUnavailable`, `RESERVED_QUOTE_SOURCES`, `QUOTE_ENVIRONMENTS`, `mapping_digest`, `EventStatus.price_basis`/`reason` |
| `src/desk/risk.py` | Reserved label requires matching source, symbol, environment and mapping digest |
| `src/desk/tickets.py` | Fixed-code evidence problems; quote-session refusal text (F8); unbacked-label warning; mapping shown |
| `src/desk/quote_check.py` | Identity capture, observed-price option selection, `--profile`, `--option-median-fallback`, `lag_evidence`, clock section, expanded missing-input list |
| `src/desk/metadata_check.py` | Report exchange code and currency (needed for a mapping review) |
| tests | `test_quote_repairs.py` (new, 67), `test_quote_risk.py` (rebuilt on the vendor-basis path, 35), `quote_support.py` (fixture), 3 updated cases in `test_tastytrade_quotes.py` |
| docs | This record, `TASTYTRADE_QUOTES.md`, `G5_ACCEPTANCE.md`, `CLAUDE.md`, child-2 attribution note, `.env.example` (`DESK_QUOTE_MAPPINGS`) |

### Dependency map

| Producer / interface | Consumer | Boundary |
| --- | --- | --- |
| `ReadClient` OAuth, metadata, quote token | `Session` / `capture` | Read-only routes, no redirects, token only to the `*.dxfeed.com` allowlist |
| `Session` + `FeedDecoder` (accepted per-type maps) | `QuoteService` | No decoding without the server's map; Profile isolated on channel 5 |
| `QuoteService` Trade/Quote/Profile, health, generation | `TastytradeRiskSource`, `quote_check` | Fresh same-generation values only; diagnostics label ineligible history |
| `MappingStore` (reviewed records) + signal's stored Webull basis | `TastytradeRiskSource._verified` | Verified before `revalidate_signal` and in the final fence; local file read, no network |
| `TastytradeRiskSource` terms + provenance (mapping digest, generation) | `risk.evaluate`, ticket binding, final fence | Reserved label checked; ticket → signal → volume/identity → quote lock (mapping read) → account |
| Webull history, Alpaca SIP decision volume, EP card | Scanner, indicators, EP | Unchanged; no Candle/dayVolume requested |

### F1–F10 dispositions and regressions

| Item | Code remedy | Regressions (`tests/…`) | Remaining |
| --- | --- | --- | --- |
| F1 | Subscribe on `CHANNEL_OPENED`; maps per type; 10 s schema deadline | `test_quote_repairs::test_lazy_config_after_subscription_reaches_data_once`, `::test_early_map_still_works`, `::test_responsive_server_without_any_usable_map_reports_schema_unavailable`, `::test_partial_map_decodes_trade_without_waiting_for_quiet_quote` | Live ordering (child 5) |
| F2 | Config handled while streaming; changed map withholds old values; invalid map withholds its type | `::test_identical_repeated_config_while_streaming_changes_nothing`, `::test_changed_field_order_is_adopted_and_withholds_earlier_values`, `::test_data_before_its_map_is_never_decoded`, `::test_invalid_map_for_one_type_withholds_that_type_only`, `::test_no_usable_core_map_withholds_then_stops` | Live behaviour (child 5) |
| F3 | No tolerance (E6); lead, explanation and `sntp` guidance reported | `::test_future_source_time_is_explained_not_clamped[1, 3600000]`, `::test_zero_or_missing_trade_time_is_unavailable`, `::test_backward_clock_jump_never_makes_a_trade_fresh` | iMac offset (child 4); tolerance is an open decision |
| F4 | Side-change age labelled "not delay evidence"; trade lag classified | `::test_old_side_change_on_a_freshly_received_option_quote_is_not_delay_evidence`, `::test_clearly_old_liquid_trade_is_visibly_aged_and_ineligible`, `::test_diagnostic_lag_evidence_in_session`, `::test_diagnostic_closed_session_ages_are_not_live_evidence`, `::test_yesterdays_print_received_today_stays_yesterdays` | Option quote freshness policy (open) |
| F5 | `QUOTE_ORDER_AMBIGUOUS` withholding with watermark recovery | `::test_mixed_side_order_withholds_the_quote_until_a_consistent_snapshot`, `::test_new_generation_clears_the_ordering_watermark`, `::test_purely_older_quote_and_trade_are_ignored_without_freshening` | Live occurrence unverified |
| F6 | Reserved label checked in `risk.evaluate`; display warning | `test_quote_risk::test_reserved_label_without_independent_provenance_is_never_approved`, `::test_honest_legacy_label_with_legacy_source_still_works`, `::test_caller_source_claim_does_not_become_provenance`, `::test_live_ticket_refuses_sandbox_quote_provenance`, `::test_provenance_without_a_reviewed_mapping_is_refused_by_risk` | — |
| F7 | Reviewed mapping verified before revalidation and in the fence | `test_quote_risk::test_unverified_identity_never_reaches_signal_revalidation[7 cases]`, `::test_unverified_identity_blocks_the_ticket_with_its_code`, `::test_verified_identity_still_reaches_the_approved_setup_rules`, `::test_legacy_price_basis_without_webull_host_is_unverified`, `::test_one_bad_record_leaves_peer_mappings_usable`, `::test_share_class_alias_and_automatic_consistency_checks`, `::test_mapping_review_requires_a_person_and_consistent_captures`, `::test_changed_evidence_after_recheck_refuses_final_write[mapping_removed, mapping_changed, identity, streamer]` | No operational mapping; Taz reviews each name |
| F8 | Actionable "quote session changed" refusal; generation binding kept | `test_quote_risk::test_independent_quote_session_gets_an_actionable_refusal`, `::test_changed_evidence_after_recheck_refuses_final_write[reconnect]` | Long-lived shared service (future design) |
| F9 | Optional Profile on channel 5; HALTED refuses before revalidation and in the fence | `test_quote_repairs::test_profile_status_is_reported_separately[3]`, `::test_profile_error_or_absence_never_stops_quotes_or_trades`, `::test_profile_traffic_after_profile_is_withheld_never_stops_the_session`, `::test_an_invalid_optional_map_withholds_profile_only`, `::test_invalid_profile_status_is_unknown_not_active`, `test_quote_risk::…[halted]` | Profile delivery (child 5); `security_tradable` wiring |
| F10 | Bounded recovery; denial/schema/429/expiry stop; fault/recovery report | `test_quote_repairs::test_heartbeat_timeout_recovers_with_fresh_generation_and_new_events`, `::test_exhausted_retries_report_the_fault_not_a_deadline`, `::test_deadline_is_distinct_from_a_fault`, `::test_denials_are_not_retried[2]`, `::test_rate_limited_token_refresh_is_not_retried`, `::test_token_expiry_stops_and_reports_renewal_pending`, `::test_keepalive_after_disconnect_never_revives_the_session`, `::test_message_for_a_superseded_session_cannot_disturb_the_new_one` | Live reconnect (child 5); token renewal |
| Secrets | Endpoint allowlist (also in `connect_ws`); redirect refusal tested with real header semantics | `::test_quote_token_destination_allowlist[15]`, `::test_forbidden_destination_never_receives_the_token[2]`, `::test_redirect_is_refused_and_credentials_go_nowhere_else` | — |
| C3 | Observed-price/explicit strike, ET expiry, distinct missing codes, optional size | `::test_option_pair_prefers_explicit_or_observed_strike_on_the_et_market_date`, `::test_diagnostic_selects_the_pair_near_the_observed_underlying_trade`, `::test_missing_quote_parts_have_distinct_codes[4]`, `::test_missing_trade_size_keeps_price_and_time_but_corrupt_size_rejects` | Representative live option check (child 5) |

### Self-review findings fixed before delivery (same independent-audit method)

1. A Profile frame error followed by more Profile traffic fell through to
   `DXLINK_PROTOCOL_UNEXPECTED` and killed Quote/Trade. Fixed; regression
   `test_profile_traffic_after_profile_is_withheld_never_stops_the_session` fails on the
   pre-fix code (mutation R23).
2. A one-type invalid map stopped the whole session, losing healthy Trade evidence when
   only the Quote map was unusable. Refined to withhold that type; stop only when no
   Quote/Trade map remains or the format is unsupported.
3. A refused KEEPALIVE overwrote the original disconnect reason. Fixed.
4. The first redirect test used case-sensitive dict headers, so urllib never attempted
   the redirect and the mutation survived. Rewritten with real header semantics; the
   mutation is now caught.

### Verification

Runtime: Linux container (Claude cloud), CPython 3.12.3 and 3.13.14; websockets 17.1,
pydantic 2.13.5, numpy 2.5.3, pandas 3.0.6, TA-Lib 0.8.1, exchange-calendars 4.13.2,
pytest 9.1.1. Not the iMac: its Python 3.14.7 has not run this commit (child 4).

| Command | Result | Type |
| --- | --- | --- |
| `python -m pytest -q -W error` (Python 3.12.3) | **1566 passed in 188.28s** | [S] full strict suite, exact committed code |
| `python -m pytest -q -W error` (Python 3.13.14) | **1566 passed in 193.49s** | [S] full strict suite, exact committed code |
| Five quote files (`test_quote_repairs`, `test_quote_risk`, `test_tastytrade_quotes`, `test_quote_diagnostic`, `test_quote_setup`) | 156 passed in 35.78s | [S] targeted, zero provider calls |
| `git diff --check` | Clean | Formatting |
| Baseline `78da588`, full strict (earlier, same container) | 1475 passed on 3.12.3 and 3.13.14 | [S] review baseline |

Before/after (scratch copies; the baseline was not rewritten): the 13 defect-confirming
review probes all pass on `78da588` (each defect present); on the repair 10 of them fail
(defect gone). The 3 that still pass are intended: the eager-config control, F3 (a future
time is still ineligible, now explained) and F4 (60 s side-change policy unchanged, now
labelled). The new regression files cannot be collected on `78da588` (no per-type maps,
no `quote_mapping`), so every new regression fails there.

Targeted mutations (each removes one protection in a scratch copy; the five quote files
must then fail): **30 of 30 detected** — lazy subscription (R1), repeated config fatal
(R2), guessed field order (R3), changed/invalid map keeping old values (R4, R4b),
reserved label (R5), mapping after state mutation (R6), final fence skipping the mapping
(R7), ambiguous BBO accepted or kept (R8, R9), endpoint suffix/userinfo (R10, R11),
redirect followed (R12), heartbeat not recovered and denial retried (R13, R13b), halt
ignored (R14), future time clamped (R15), Trade size required (R16), KEEPALIVE revival
(R17), Profile error fatal (R18), generic session message (R19), mapping digest and
environment unchecked in risk (R20, R20b), older trade freshening (R21), schema deadline
removed (R22), the original Profile-channel bug (R23), mapping Webull/tastytrade checks
(R24, R25), automatic mapping review (R26). R12 and R20 first survived; their tests were
strengthened and re-run (see the self-review list and the provenance test).

### Required inputs and current availability

| Input | Status |
| --- | --- |
| Reviewed Webull↔tastytrade mapping per watchlist name | **None exist.** Needs a `desk.metadata_check` capture, a `desk.quote_check` identity capture and Taz's review per name |
| iMac clock offset | Not measured (offline checkpoint); `sntp time.apple.com` in the host block |
| Profile/trading status delivery for this account | Unknown; opt-in `--profile` run in child 5. `security_tradable` still comes from the separate observation input |
| Long-lived shared quote service, IPC, token renewal | Not built (future design) |
| Account, market regime, option ContractBook, open interest, valuation | Unchanged: not supplied by this adapter |
| Immediate consolidated intraday volume | Unresolved |

### Acceptance status

Closed by code and regressions (pending Astra's audit): F1, F2, F5, F6, F7 (interface),
F9 (known HALTED), F10 (bounded recovery). Diagnostic-closed: F3, F4, F8. Still open:
child Step 3 independent sign-off (Astra), child 4 (reviewed-commit iMac setup, `sntp`,
strict suite on 3.14.7, private credentials), child 5 (regular-session stock/option
timing, live config ordering, Profile delivery, reconnect), child 6 (remaining G5 rows),
operational mappings, parent Checkpoint 3 and G5. Step 09 stays paused.

## Follow-up repairs R1–R4 (Astra's re-audit of `12705aa`, 2026-10-05)

Astra withheld child-3 sign-off. Her audit, probe script and result JSON arrived with
Taz's prompt (outside the repository; probe kept unchanged in the scratch area and run
before and after). Base: `12705aa2fbc6ffb2a5320ffe1e171a1d3fd68db8`, branch head
unchanged at fetch, clean tree. Before output on a scratch clone of `12705aa`, Python
3.12.3: R1 approve and consume, R2, R3 and R4 all `defect_reproduced: true` (Astra's
own run, Python 3.12.14, matched). These are implementation repairs against approved
contracts, not trading strategy.

### Requirements (written before code)

- **R1 mapping fence.** Verification and every supported writer (`MappingStore.add`,
  used by the review command) share a lock on a stable sidecar file
  (`<store>.lock`, never renamed; the JSON is still replaced atomically, so locking the
  JSON inode would be bypassed). `fcntl.flock` locks belong to an open file description,
  so separate opens conflict across threads and processes
  ([flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html)). Verification holds a
  shared lock; writers take an exclusive lock only after the reviewer has confirmed. The
  ticket fence holds the shared lock from before the final clock until the ticket
  COMMIT, for approval and consumption. Bounded wait (engineering: 10 s); a busy or
  unreadable store refuses the action (`QUOTE_MAPPING_STORE_BUSY`,
  `QUOTE_MAPPING_STORE_UNREADABLE`, `QUOTE_MAPPING_LOCK_UNAVAILABLE`). No network inside.
  With no mapping file, nothing is created and nothing verifies (read-only). No removal
  command exists; updates replace a symbol's record.
- **R2 classification.** Webull: `SecurityMetadata` carries `sub_category`
  (COMMON_STOCK/ETF); the vendor path pins `[instrument_id, currency, exchange_code,
  sub_category]` per host and symbol and refuses any change on every fetch
  (`SECURITY_IDENTITY_CHANGED`). The bridge reads that pinned identity locally and
  compares instrument ID, currency and sub-category with the review. tastytrade:
  `is-etf` (documented boolean) becomes part of the instrument identity (digest) and is
  compared with the review at verification and in the final fence. Only a real JSON
  boolean counts; missing, `None`, `"false"` or malformed is
  `QUOTE_MAPPING_CLASSIFICATION_UNAVAILABLE`. Classification joins the mapping's
  semantic digest; names, descriptions, receipt times and capture hashes do not.
  Schema `desk-quote-mappings-v2`; v1 records are kept verbatim as
  `legacy_unverified` and refused (`QUOTE_MAPPING_REVIEW_REQUIRED`) until re-reviewed;
  peers are never erased. Limitations: Webull exposes no CUSIP/FIGI; tastytrade
  `instrument-sub-type` values are not enumerated in the OpenAPI, so only `is-etf` is
  bound; a Webull reclassification is detected by the next vendor fetch (pin refusal),
  not inside the no-network final fence.
- **R3 halt latch.** A HALTED Profile for a symbol is retained by `QuoteService`
  independently of Profile delivery: a channel ERROR/CLOSED, invalid map or row,
  UNDEFINED, disconnect/reconnect, a new Trade or an accepted map do not clear it. Only
  an ACTIVE Profile for that symbol, received in the current session, clears it. No
  timer. While latched, `trade()`/`quote()` refuse with `SECURITY_HALTED` (no halted
  price serves as current evidence); the bridge refuses before revalidation and in the
  final fence. Superseded sessions cannot clear it (session generation guard). No
  Profile ever received stays UNKNOWN and never ACTIVE; peers are unaffected. The
  latch lives in the process: a restart starts with no live values and no latch
  (a new process has no quote evidence until fresh events, and approvals never carry
  across sessions). Retention is our engineering contract; dxFeed's TradingStatus enum
  (ACTIVE, HALTED, UNDEFINED) does not prescribe it. Exchange halts have no fixed
  duration and end on an explicit resumption, which is why no timer clears it.
- **R4 final-attempt reporting.** `QuoteService` counts accepted events per symbol and
  component for the current generation. `capture` reports each attempt's generation and
  per-symbol Quote/Trade/Profile receipt, `final_attempt`, and recoveries by symbol and
  component (`recovered` only when every subscribed symbol received both Quote and
  Trade in the new generation). `diagnostic` builds stock/option status and
  `lag_evidence` from the final generation only; earlier observations appear under
  `historical_observations`, labelled ineligible. A deliberate finite end
  (`CAPTURE_COMPLETE`) stays distinct from faults, exhausted retries, denial, rate
  limit and token expiry.

### Lock order (final approve/consume transaction)

ticket (`BEGIN EXCLUSIVE`) → signal (`held_event`) → volume/identity (only when the
event depends on Alpaca) → **mapping (shared sidecar flock)** → quote health
(in-process lock) → account (`held_account`) → final clock → COMMIT → release in
reverse. Mapping is taken before the in-process quote lock so a wait on another
process never blocks the quote feed thread. A mapping writer takes only the exclusive
mapping lock and no other lock, so no cycle exists.

### Acceptance cases (written first)

1. Astra's approval and consumption races: the writer waits until COMMIT, or the
   action refuses; never "approved/consumed while an earlier completed change
   contradicts". Repeat with a separate `MappingStore` object and a separate process.
2. Mapping changed before the fence refuses; unchanged mapping approves and consumes.
3. Busy lock (bounded), unreadable store and failed write refuse the action; no
   deadlock, no partial file, no leftover temp file, lock free afterwards; a bad peer
   record stays isolated.
4. Common stock → ETF and ETF → common stock (tastytrade `is-etf`) refuse before signal
   mutation and in the final fence; review vs pinned Webull sub-category mismatch
   refuses; missing/`None`/`"false"` classification is unavailable; a correctly mapped
   common stock (full path) and ETF (pinned Webull identity) verify; a refreshed
   identity with a newer receipt, new description or new capture hash stays usable;
   v1 records are refused and preserved; share-class and wrong-issuer tests retained.
5. HALTED then ERROR, CLOSED, invalid map, invalid row, UNDEFINED: refused at resolve,
   approval and consumption. HALTED → reconnect → fresh Trade: refused. ACTIVE clears
   it (with valid inputs). No Profile: UNKNOWN. Peers unaffected; a superseded
   session's ACTIVE cannot clear it.
6. Empty second attempt: no final-attempt Trade or lag evidence; earlier Trade only as
   history. One ticker recovers and another does not; Quote-only and Trade-only
   recovery; Profile-only recovery is not Quote/Trade recovery; genuine recovery
   reported; distinct stop outcomes; closed market never PASS.

### R1–R4 implementation record

Changed files: `src/desk/quote_mapping.py` (sidecar flock fence, `MappingView`, schema v2
with legacy preservation, StrictBool classification, review command reports a busy
store), `src/desk/quote_risk.py` (fence held to COMMIT, pinned Webull identity,
`webull_identity`), `src/desk/vendor_basis.py` (`VendorHistoryStore.pinned`, local
read), `src/desk/tastytrade_quotes.py` (strict `is_etf` in the instrument identity,
halt latch, per-generation event counts), `src/desk/tastytrade_transport.py`
(`attempt_log`, `final_attempt`, per-symbol/component recoveries),
`src/desk/quote_check.py` (final-generation status/coverage/lag, labelled history);
tests `tests/test_quote_reaudit.py` (new, 43) and updated fixtures/expectations in
`test_quote_risk.py`, `test_quote_repairs.py`, `test_quote_diagnostic.py`,
`test_tastytrade_quotes.py` (`stock()` now carries `is-etf: false`); docs.

Halt latch scope (documented choice): kept per symbol, including across an identity
refresh of that symbol (fail closed; a new identity also needs its own reviewed
mapping); cleared only by an ACTIVE Profile for that symbol in the current session.

| Item | Before (`12705aa`, Astra's probe unchanged) | After (repair, same probe) | Regressions |
| --- | --- | --- | --- |
| R1 approve | writer finished before COMMIT; approved; `defect_reproduced: true` | writer still waiting at COMMIT; approved under the unchanged mapping; later verification sees the change; `false` | `test_quote_reaudit::test_thread_writer_with_its_own_store_waits_for_the_ticket_commit[approve, consume]`, `::test_separate_process_writer_waits_for_the_ticket_commit[approve, consume]`, `::test_writer_that_finishes_before_the_fence_makes_the_action_refuse`, `::test_busy_fence_refuses_then_releases_cleanly`, `::test_unreadable_store_refuses_and_leaves_the_lock_free`, `::test_failed_write_leaves_no_partial_file_temp_file_or_lock`, `::test_no_mapping_file_stays_read_only`, `::test_symlinked_lock_is_refused`, `::test_v1_records_are_preserved_refused_and_migrated_without_erasing_peers`, `::test_review_command_reports_a_busy_store_instead_of_crashing` |
| R1 consume | consumed; `true` | writer waiting at COMMIT; consumed under the unchanged mapping; `false` | (same) |
| R2 | identity digest unchanged; approved; `true` | refused at recheck `QUOTE_MAPPING_CLASSIFICATION_MISMATCH`; ticket stays pending; `false` | `::test_common_stock_becoming_an_etf_refuses_before_any_signal_change`, `::test_reclassification_between_recheck_and_commit_refuses[approve, consume]`, `::test_correctly_mapped_etf_verifies_and_an_etf_turning_common_refuses`, `::test_review_against_a_differently_classified_webull_pin_refuses`, `::test_missing_or_corrupt_classification_is_unavailable_never_common_stock[5]`, `::test_review_refuses_a_string_classification`, `::test_harmless_refresh_needs_no_new_review`, `::test_the_signal_path_pins_the_webull_classification`, `::test_classification_is_part_of_the_bound_instrument_identity` |
| R3 | after ERROR: `UNKNOWN`; approved; `true` | after ERROR: `HALTED / HALT_RETAINED_NO_RESUMPTION_EVIDENCE`; refused; `false` | `::test_halt_is_retained_through_profile_faults[6]`, `::test_halt_then_profile_fault_between_recheck_and_commit_refuses[approve, consume]`, `::test_halt_survives_reconnect_and_fresh_trades_until_active`, `::test_superseded_session_cannot_clear_a_newer_halt`, `::test_halt_is_per_symbol_and_no_profile_is_unknown`, `test_quote_repairs::test_profile_status_is_reported_separately[HALTED]` |
| R4 | `OBSERVATIONS_ONLY`, lag `AVAILABLE_WITHIN_QUOTE_POLICY`, attempt-1 Trade shown; `true` | `NO_FINAL_ATTEMPT_OBSERVATIONS`, lag `UNAVAILABLE`, no final rows, attempt-1 Trade only under `historical_observations`; `false` | `::test_empty_final_attempt_keeps_earlier_trade_as_history_only`, `::test_one_ticker_recovers_and_another_does_not`, `::test_single_component_recovery_is_reported_as_partial[Quote, Trade]`, `::test_profile_only_recovery_is_not_quote_or_trade_recovery`, `::test_genuine_recovery_is_reported_with_new_observations`, `test_quote_repairs::test_heartbeat_timeout_recovers_with_fresh_generation_and_new_events` |

Before/after of the new file: on a scratch clone of `12705aa` (one import given a
no-op fallback so the file collects; no baseline behaviour changed) 39 of the then 41
tests fail; the 2 passing are controls (a mapping changed before the fence already
refused; a superseded session already could not speak). All pass on the repair.

Self-review during this round: the review command let a busy store surface as a
traceback (now a plain "Not recorded … Nothing was changed"); the first mutation run
showed no test proved classification is part of the bound instrument identity (test
added; mutation Q2d now caught).

#### Verification (R1–R4 commit)

Runtime: Linux container (Claude cloud), CPython 3.12.3 and 3.13.14; websockets 17.1,
pydantic 2.13.5, pytest 9.1.1 (other versions as recorded above). Not the iMac.

| Command | Result |
| --- | --- |
| `python -m pytest -q -W error` (3.12.3) | **1609 passed in 216.70s** |
| `python -m pytest -q -W error` (3.13.14) | **1609 passed in 224.11s** |
| `tests/test_quote_reaudit.py` | 43 passed |
| `git diff --check` | Clean |
| Astra's probe, unchanged, before (`12705aa`) / after (repair) | R1 approve/consume, R2, R3, R4: `defect_reproduced` true → false (outputs kept in the implementer's scratch area; key fields in the table above) |
| Probe outputs and new test file scanned for credentials | No token/secret/bearer text; only labelled synthetic strings |

Targeted mutations for the four safeguards (each removes one protection in a scratch
copy; the six quote test files must then fail): **20 of 20 detected** — final fence
without the mapping lock (Q1), writer without the lock (Q1b), busy store not refused
(Q1c), lock file created with no store (Q1d), legacy records trusted (Q1e), review
command crashing on a busy store (Q1f), tastytrade classification unchecked (Q2),
unavailable treated as common stock (Q2b), Webull pinned classification unchecked
(Q2c), classification outside the instrument digest (Q2d), string classification
accepted in a review (Q2e), any Profile clearing a halt (Q3), Profile withholding
clearing it (Q3b), reconnect clearing it (Q3c), halted trade still served (Q3d), status
forgetting the latch (Q3e), summary from any generation (Q4), recovery by any event
(Q4b), coverage not per generation (Q4c). Q2d first survived; a test was added.

**Not completed:** the rerun of the first-round 30-mutation set (R0–R26) on this code
was stopped after 2 of 31 runs (control passed; R1 lazy subscription detected by 16
tests). Those 30 were all detected on `12705aa`; their detection on this commit is not
re-established beyond those two.

Unchanged pending acceptance: child 3 sign-off (Astra's re-audit), child 4 (iMac,
Python 3.14.7, `sntp`, credentials), child 5 (regular-session timing, live config order,
Profile delivery, reconnect), child 6 (remaining G5 rows), operational mappings and
runtime inputs. Hierarchy G5 → G5a parent Checkpoint 3 → children 1–6; parent CP3 and G5
stay open; Step 09 stays paused.
