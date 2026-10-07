# G5 -> G5a parent CP3: diagnostic repairs and option Greek state

Authorized batch: Taz's 2026-10-07 "lets do it" following the four-item sequence.
Base af98bd8. Serves analyze/plan/approve; Step 09 stays paused and G5 open.

## Before-code requirements

1. **Checked in code:** provider_check ignores argv and therefore --help contacts
   Webull. Parse help/invalid arguments before reading credentials or creating clients.
2. **Checked in code:** TradierClient already uses REST_REQUEST_BUDGET and preserves
   QuoteUnavailable. Claude's outer scratch ceiling used an untyped exception,
   which the transport intentionally sanitizes as REST_TRANSPORT_FAILURE. Supply a
   typed budget exception for collectors, categorize it locally and prove that a
   final refused fetch cannot retain an earlier freshness PASS. Do not guess the
   cause of arbitrary network exceptions from their text.
3. Verify the existing Tradier integration with its synthetic end-to-end suite and
   recorded mutation probes. This is developer verification, not an independent
   audit, actual factory wiring, live consumer or new iMac acceptance.
4. **Sourced:** dxFeed Greeks are indexed/time-series events. Follow transaction,
   removal, overlapping snapshot and deferred-end semantics from
   https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/IndexedEvent.html and
   https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/option/Greeks.html . Index
   encoding is specified in the provider SDK:
   https://github.com/dxFeed/dxfeed-graal-cxx-api/blob/main/src/event/option/Greeks.cpp .
   Preserve integer indexes, independently validate time/sequence/index consistency,
   and select the newest indexed calculation rather than the last packet received.
5. Producer: Recorder's negotiated Greeks map and registered option identities.
   Consumer: analysis/diagnostic Greek getter and explicit terminal capture report.
   No provisional analytics enter signal/risk/ticket decision modules. New transport
   generations, changed/withdrawn schema, disconnect, token expiry and identity
   failure invalidate state. Ordinary flag-zero events are atomic updates (Greeks
   also implement LastingEvent); they do not require an invented snapshot marker.
6. A transaction/snapshot in progress withholds its symbol until committed. A
   repeated BEGIN discards pending leftovers; END+TX waits; removal markers need no
   numeric values. SNIP remains incomplete, never advertised as full coverage.
7. Keep each calculation's source and receipt times and dynamically computed age;
   reject future/invalid times, no tolerance or invented Greek freshness limit.
   Expose documented field units individually; missing/nonfinite fields never borrow
   earlier values. No new stop valuation, portfolio sizing or chosen-quantity rule.

## Acceptance, open items and rollback

Help/invalid CLI input makes zero requests; typed external and internal ceilings are
distinct from provider limits/network errors. Positive and negative Tradier runtime
tests/mutations, existing architecture guards, Greek atomic updates/corrections/
removals/transactions/overlapping/empty/truncated snapshots, large indexes, schema
changes, malformed fields, identity isolation, token expiry, reconnect and final
capture closure must pass. Run focused tests then the combined strict suite.

Advertised Tradier REST and streaming quote-size units remain unresolved. Prepare
separate provider questions; no messages are sent without authorization. Cloud
comparison reports are user-supplied observations, not independently re-read raw
evidence. No real provider calls are needed for this batch. Actual iMac integration,
reviewed mappings, runtime factory and bounded Greek live acceptance remain open.
Plan B: show unavailable Greeks with reasons and retain labelled raw observations;
do not substitute a provider or freshen by receipt. Revert this batch to return to
af98bd8; no host settings, broker state or scheduler is changed.

## Results

Implemented: safe provider CLI parsing; typed local budget failure/category;
separate tastytrade_greeks analysis state, Recorder schema/lifecycle integration,
per-attempt historical terminal state and unconditional core quote disposal.
Gamma's displayed unit now includes its underlying-price denominator. Tradier
provisional mappings, position sizing and loss math are unchanged.

Focused diagnostic/provider repairs: 26 passed. Tradier runtime: 76 passed in
22.10 s and 7/7 existing guard mutations caught. Greek/state/quotes/diagnostics/
conventions targeted batch: 222 passed before four final close-fault/report/index
regressions were added; the initial final Greek suite: 54 passed. Greek mutations: initially 9/9
caught. The first index mutation used a zero-time-invalid sample that remained
refused by another guard; replaced with a plausible nonzero mismatched timestamp,
which fails when index validation is removed. This is disclosed test strengthening,
not a claim that the earlier mutation caught the defect.

An earlier combined run passed 2108 tests in 205.99 s. Final self-review then added
socket-close-fault withholding and guaranteed core disposal even when a measurement
report/teardown raises. The final strict result below must cover those changes.

The architecture importer guard adds exactly one authorized analysis module,
tastytrade_greeks.py. Decision modules remain excluded. No secret, real provider
request, account/order call, host setting, scheduler, mapping or activation was
needed. The supplied extras numerical comparisons were not independently replayed.

A subsequent run passed 2112 tests in 199.69 s, preceding the final identity-cache
repair. Self-review found a same-identity failure/recovery could revive old analysis
state if no getter ran during failure. QuoteService now exposes an atomic identity
plus invalidation epoch for this separate cache; its quote/trade policy is unchanged.
Greek getters compare the epoch, and an observed metadata-health failure clears the
analysis cache. Four regressions cover unobserved/observed failure recovery, healthy
refresh and stale metadata refresh. Targeted Greek/quote/re-audit: 144 passed in
15.59 s; Greek suite now has 58 cases. Expanded Greek mutations: 10/10 caught.

Final combined verification: `PYTHONPATH=src ../verify-python314/bin/python -m
pytest -q -W error` on Python 3.14.6: **2116 passed in 199.96 s**. This run covers
the final identity-cache repair. `git diff --check` is clean. No market-data
requests, orders, account calls, schedules or iMac changes were made. Provider
documentation was read over the network; no credentials were read for this batch.
Independent audit and actual-host acceptance remain pending; G5 remains open.
