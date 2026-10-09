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
The newest validated index remains a recovery floor through rejection or a
same-instrument/schema reset. An older standalone history correction cannot become
current after newer rows were discarded. Equal/newer calculations can restore the
view; a completed full replacement snapshot is authoritative and can reset that
floor, including to an empty view. Invalid indexes never establish it.

Same-generation identity/schema resets discard values but retain interruption
metadata. If transaction content was lost, its remaining records and its TX-clear
closing record are discarded. Lost snapshots also wait for END; if END has TX set,
the closing transaction must finish too. SNIP retains the existing requirement for
a new complete snapshot. A new BEGIN can restart cleanly, and a new connection
generation starts empty with normal lasting-event delivery. This recovery behavior
is a conservative engineering choice implementing the provider transaction rules;
it is not a trading threshold. Stored recovery metadata has no numeric Greek values
or executable quotes. See `checkpoints/G5a-greek-recovery-2026-10-08.md`.

Discarded decoded events still update boundary metadata during identity/schema
withdrawal, including before the first stored calculation. A dropped BEGIN keeps
subsequent snapshot body records unavailable until END and any pending TX finish;
a dropped TX keeps its tail unavailable through TX-clear. If the closing boundary
is decoded while still withdrawn, the next complete update can recover after
health returns. No discarded calculation values or indexes establish a current
calculation or index floor.

If Greek data arrives while its map cannot decode it (or cannot expose valid
flags), a lost snapshot start cannot be ruled out. Registered option states then
require a complete new replacement snapshot or a new connection generation.
When the symbol cannot be decoded, all currently registered options are affected;
stock Quote/Trade state remains untouched. Merely withdrawing a map without
dropping a payload adds no such requirement. Normal first flag-zero delivery in a
new generation is still accepted. This does not implement automatic resubscription
or prove that a live provider will send a replacement snapshot after a map repair.

A row with the wrong event type is malformed even if its field map decoded it.
Recorder and the direct reducer both latch full-snapshot/new-generation recovery;
the row's flags, values and index are not trusted. When the registered option is
identifiable, only its analysis state is withheld. An unidentifiable or non-option
wire identity withholds registered option analysis without allocating unknown
symbols or modifying stock Quote/Trade state. This closes the wrong-eventType
missing-start path from Claude's cd9f891 re-audit.
Missing/invalid flags on an otherwise mapped row use the same latch: they cannot
prove that the discarded row was an atomic update. Valid flag-zero rows with a
bad calculation index retain the existing standalone recovery and F1 floor.
A first rejected TX requiring a snapshot allocates recovery metadata even if
there is no previous calculation (including a locally invalid receipt time).

**Conservative engineering choice:** removal of the latest calculation does not
silently promote an older one to current; a replacement at that index or later, or
an explicit full snapshot, restores state. Earlier rows remain internally indexed.
4096 pending/retained entries per option is a memory bound; overflow withholds that
symbol rather than silently trimming history. It is not a trading rule.

The raw `measurements.greeks` block is explicitly labelled RAW_OBSERVATION_SUMMARY
with `summary_scope=RAW_OBSERVATIONS_ONLY` and `arithmetic_eligibility=NONE`; the
ambiguous old `current_state` marker is removed. Its `reduced_state_path` links
`measurements.greek_state`, which reports reduced state. Raw values remain unchanged.
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

## Saved-data audit (2026-10-08)

`desk.saved_greek_audit` checks retained indexed observations against their captured
option identities, keeps exact indexes/decimals, and computes age from source time
to original receipt time. Contradictory duplicate identities cannot silently replace
each other. The bounded measurement log omits some control/health events, so it is
not replayed as a complete snapshot or a live connection.

The authentic MacBook comparison archive yielded six valid indexed Greek observations
for two SPY options. Original receipt ages were 200.967142, 200.967142, 128.550859,
128.654318, 101.271857 and 101.271857 seconds. These are calculation ages at receipt,
not measured quote-feed delay. No records were reported dropped by that capture.
This validates those retained observations and documented field conventions; it
does not attest the latest reducer against live delivery or a usable Greek age policy.
Tradier conventions remain provisional. No option future-stop valuation was derived.
