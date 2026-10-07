# tastytrade indexed Greek analysis

G5 -> G5a parent CP3, options-specific follow-up; Step 09 remains paused.
Serves analyze/plan. This adds no option stop valuation, trade eligibility, orders
or approved maximum Greek age. Current means the latest consistently reduced
provider calculation, not a calculation certified fresh enough for execution.

## Evidence and definitions

**Sourced:** [dxFeed Greeks](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/option/Greeks.html)
defines an option calculation, its millisecond timestamp, derivatives and time-series
index. [IndexedEvent](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/IndexedEvent.html)
defines pending transactions, replacement/removal and snapshot flags. The
[provider SDK encoding](https://github.com/dxFeed/dxfeed-graal-cxx-api/blob/main/src/event/option/Greeks.cpp)
uses seconds shifted 32 bits, milliseconds shifted 22, then a 22-bit sequence.
Index integers are never cast to floating point. Greek events also have lasting-event
delivery: ordinary flag-zero updates do not require artificial snapshot boundaries.

**Checked by synthetic tests:** same-index corrections, timestamp ordering, empty and
overlapping snapshots, END+TX deferral, removals, SNIP and failures. **User-supplied
live observations:** Claude's 2026-10-07 cloud capture contained Greek calculations
58–121 seconds old. We have not independently replayed its unattached raw evidence.

Field mappings retain documented units: delta per underlying share, gamma per share
per $1 underlying move, theta dollars per share per day, vega per IV percentage point,
rho per rate percentage point. Gamma's label now includes its underlying-price
denominator. Provider values remain unchanged. Each field has its own validation;
an invalid/missing field cannot borrow a value from an earlier calculation.
`price` is the provider's calculation input option price, not an executable bid/ask
or a hypothetical future price. IV is separately labelled as an observed decimal
fraction. No signed portfolio quantity or deliverable is inferred from this state.

## Pipeline and state

`quote_check --measure` -> existing separate measurement channel -> negotiated
per-kind fields -> `Recorder.greek_state` -> `GreekState.current(symbol, aware_time)`.
The getter verifies the live session, token and resolved option identity on every
read, preserves source/receipt times and reports dynamically increasing age.
Snapshot/transaction processing never touches QuoteService's Quote/Trade caches.

New/changed/withdrawn Greek schemas clear reduced state. Identical repeated schemas
retain it; missing optional numeric fields make calculations partial. A reconnect
starts a new empty generation; identity contradictions and disconnects withhold
state immediately. An atomic identity invalidation epoch prevents even an
unobserved identity failure followed by recovery from reviving an older calculation;
a new Greek event is required. A healthy same-identity refresh preserves the source
calculation time. A pending transaction or snapshot is unavailable until completion;
repeated BEGIN discards residual pending events. Truncated snapshots stay unavailable.
A malformed multi-event update requires a new snapshot or generation; its tail cannot
recover the incomplete view. A bad standalone update can recover with a valid atomic
update. The newest calculation is chosen by index/time/sequence, not packet arrival.

**Conservative engineering choice:** removal of the latest calculation does not
silently promote an older one to current; a replacement at that index or later, or
an explicit full snapshot, restores state. Earlier rows remain internally indexed.
4096 pending/retained entries per option is a memory bound; overflow withholds that
symbol rather than silently trimming history. It is not a trading rule.

The normal raw `measurements.greeks` records and offline observation normalizer keep
their original labels. The new `measurements.greek_state` reports reduced state.
After finite capture closes it correctly says unavailable. Each capture attempt also
records `capture.final_attempt.greek_terminal_state`: an explicitly historical,
ineligible view at that attempt's end. Failed/reconnected earlier attempts never
supply the final attempt's Greek evidence. Current read time and receipt time never
freshen the calculation timestamp. No quote's 60-second policy is imposed on Greeks.

## Remaining acceptance and Plan B

Developer tests are not independent audit, iMac verification or live Greek acceptance.
On the reviewed commit run the iMac strict suite first. Then a bounded existing
`quote_check --measure` capture can inspect negotiated fields, the terminal Greek
state and ages; retained raw events remain available for independent replay.
Operational ticket/factory/mappings remain open. This analysis handler is not an
all-day service and is not consumed by risk/ticket eligibility. Tradier's provisional
Greek conventions remain informational and separate from tastytrade.

Plan B: show the specific unavailable/partial state and retain raw observations;
no fallback provider, guessed multiplier or theoretical price is substituted.
Revert this follow-up to disable the reducer; no host/account settings change.
