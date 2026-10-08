# G5 → G5a parent CP3: Greek recovery F1/F2

Authorized immediate next step, 2026-10-08. Base 4776bea; clean local and matching
remote verified before editing. Serves analyze/plan. Step 09 paused; G5 open.

## Before-code requirements and evidence

**Checked:** Claude's review reports F1/F2; Astra independently reproduced all three
scenarios on 4776bea. F1 rejected protocol update clears rows and loses knowledge of
a newer accepted index. F2 identity/schema resets lose pending transaction knowledge,
allowing the closing record alone to become current.

**Sourced mechanism:** dxFeed's
[IndexedEvent contract](https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/IndexedEvent.html)
requires multi-event transaction updates to be applied together at TX-clear and
snapshot completion to wait for any pending transaction. **Engineering recovery
choice:** where content has been lost during a same-generation reset, discard its
tail through the first valid TX-clear boundary (including that closing record),
then accept new complete updates; an explicit replacement snapshot can recover
sooner. This is not a new trading rule or freshness tolerance.

Producer: negotiated Recorder Greek events and QuoteService identity epochs.
Consumer: separate analysis getter and historical terminal diagnostic view. No
signal/risk/ticket consumer, position quantity or valuation changes.

1. Retain highest validated accepted index through reject/reset for the same
   instrument and generation. Older standalone corrections may be historical but
   cannot restore current state. Equal/newer valid updates restore it. Never use
   unvalidated indexes as a floor. A completed full replacement snapshot is
   authoritative even if its newest index is older; empty snapshots stay empty.
2. Preserve interrupted transaction/snapshot state across changed or withdrawn
   Greek schemas and identity failure/epoch resets. Reject/discard closing tails;
   repeated resets cannot erase interruption knowledge. Corrupt protocol updates
   retain their stricter full-snapshot recovery. New generations start clean and
   normal flag-zero initial delivery remains supported.
3. Reset removes usable rows and queued values; retain only bounded per-symbol
   recovery metadata (identity, index floor, clock and interruption flags). A
   changed instrument does not inherit the old instrument's index floor, but an
   interrupted stream still needs a safe boundary. Healthy peers remain isolated.

## Acceptance, open work and rollback

Test F1 older/equal/newer recovery, authoritative and empty snapshots, malformed
large indexes; F2 observed/unobserved identity failure, epoch change without reading,
schema change/withdrawal/reacceptance through Recorder, repeated resets, TX tails,
lost snapshots, control/removal boundaries, explicit BEGIN and new generations.
Retain original Quote/Trade and transaction tests; strengthen mutation checks and
run one final combined strict suite. Capture before/after on the exact base.

Plan B: unavailable Greeks with specific recovery reasons and retained raw events.
No network call, iMac setting, subscription or order needed. Revert this commit to
4776bea; no persisted store migration. Independent re-audit and exact iMac commit
acceptance remain required. Local budget/reporting observations O3–O5, size units,
opening volume and other G5 items remain separate; do not implement them here.

## Results

Implemented in tastytrade_greeks.py: same-generation reset retains index floor,
identity/receipt and boundary flags while dropping numeric rows/pending values.
Identity-health failures use this reset; protocol corruption keeps its full-snapshot
requirement. Full authoritative snapshots reset the floor; added-then-removed newest
calculations still leave an index floor. Snapshot interruptions wait for END and any
deferred TX completion; SNIP still requires a new full snapshot.

New tests/test_greek_recovery.py has 33 cases. On an exact 4776bea Greek module in a
disposable source copy: **27 failed, 6 passed** (pytest exit 1), confirming defect
coverage and passing controls. On the final repair, focused Greek/Recorder/quotes/
conventions plus Tradier runtime: **267 passed in 22.88 s** on Python 3.14.6.

Expanded Greek safeguard mutations: **18/18 caught**; unchanged Tradier guards:
**7/7 caught**. Existing mutations were adapted to the new reset interface. The first
mutation run exposed a weakened teardown assertion: a getter could itself clear
values before the test inspected them. The test now asserts withdrawal and absence
of values before calling any getter; the teardown mutation is caught again. The
identity mutation's anchor was narrowed to the getter to avoid accidentally matching
the new feed-reset check. Import/collection errors never count as caught mutations.

Existing teardown tests now permit recovery metadata but assert that no rows/pending
values remain and the handler is withdrawn. No decision boundary guard was relaxed.
git diff --check is clean. An intermediate full run passed **2146 in 204.41 s**;
self-review added three reset-after-SNIP cases and the retained truncation latch.
Final strict suite, `PYTHONPATH=src ../verify-python314/bin/python -m pytest -q -W
error` on Python 3.14.6: **2149 passed in 204.80 s**. This covers all 33 final new
regressions and the retained truncation latch. No market-data provider calls,
credential reads, iMac changes or orders. Documentation and Git network access only.

Independent re-audit remains required. This record does not close G5 or claim any
actual-host/live transaction/snapshot event. Original live flag-zero evidence is
retained in its scope; rare protocol behavior is synthetic acceptance only.

## Re-audit follow-up: first record lost during withdrawal (before code)

Claude's re-audit of f67f027 accepts F1 and reports a remaining F2 case. Astra
reproduced all four reported paths on the exact commit: identity failure loses
the first TX or BEGIN (including no prior symbol state); a map missing index
fields loses first TX; an invalid map makes the first TX undecodable. Each
published the tail as CURRENT_CALCULATION. Remote and local f67f027 match.

Requirement: observe discarded protocol boundaries even before a symbol has a
calculation. On a decoded row withheld by identity/schema health, retain only
validated flags and same-generation recovery metadata, never its numeric values
or unvalidated index. Discard its incomplete transaction/snapshot through the
closing boundary. If that boundary is itself dropped but decoded, record its
completion; subsequent complete updates can recover. Existing reset semantics
and the F1 validated-index floor remain intact.

Undecodable Greek data cannot reveal whether a BEGIN was lost: mark the currently
registered option symbols as requiring a full replacement snapshot or a fresh
connection generation. A TX-clear record alone cannot repair an unknown missing
snapshot. This stricter engineering choice follows the documented snapshot/TX
mechanism; it is not a freshness threshold or a claim about retail practice.
Merely withdrawing a map, with no dropped data, does not impose this stronger
requirement. Only Greek state is affected; Quote/Trade state is unchanged.

Acceptance: first TX/BEGIN before any state and after an atomic calculation;
identity and limited/invalid map windows; healthy-peer isolation where the symbol
is known; unknown-schema loss, repeated resets, END+TX, truncation and explicit
snapshot/new-generation recovery; no values retained in dropped-row metadata.
Add failing regressions on f67f027, independent mutation guards and one final
combined strict suite. No provider calls or iMac update. Roll back this follow-up
to f67f027. The held iMac instructions remain held until re-audit passes.

## First-record-loss follow-up results

Implemented discarded-row boundary tracking in GreekState, including creation of
value-free metadata for already resolved options without a prior calculation.
It does not attest identity health: the healthy identity/epoch check still runs
before any accepted calculation. Decoded BEGIN/TX/END/SNIP flags track the lost
group; even a closing boundary dropped during the withdrawal is recorded. Invalid
flags cannot clear uncertainty. No dropped numeric values or indexes are retained.

Recorder reports undecodable Greek payloads and malformed compact row lengths as
unknown gaps. These require a full new replacement snapshot or a fresh generation
because a TX-clear alone cannot reveal a missed BEGIN. All registered options are
withheld when the symbol is undecodable; Quote/Trade state is unchanged. Empty valid
payloads and map withdrawal without lost data add no unknown gap. Snapshot-required
reasons survive resets rather than being replaced by a generic waiting message.
No automatic resubscription is added: in an unknown-gap case a provider sending
only ordinary updates will remain unavailable until a new capture/reconnect.

Claude's original new probes were not attached. Astra recreated the four reported
paths against exact f67f027 and the repair: all four previously published current
calculations; all four now remain unavailable with boundary-specific reasons.
Before/after and mutation records: `../evidence/G5a-greek-dropped-start-offline.json`.

`tests/test_greek_dropped_start.py` adds 40 cases. Against the exact f67f027 Greek
and Recorder modules in a disposable source copy: **36 failed, 4 passed**, no
collection errors. The prior 33 recovery cases are unchanged. Focused command:
`PYTHONPATH=src:. ../verify-python314/bin/python -m pytest -q -W error
tests/test_greek_dropped_start.py tests/test_greek_recovery.py
tests/test_tastytrade_greeks.py tests/test_quote_measure.py
tests/test_tastytrade_quotes.py tests/test_option_conventions.py
tests/test_tradier_quotes_runtime.py`: **307 passed in 22.05 s**.

`tools/check_greek_state_guards.py`: **26/26 caught** (eight new guards).
Unchanged `tools/check_tradier_quote_guards.py`: **7/7 caught**. Import/collection
errors are never counted as caught mutations. Final combined strict suite on
Python **3.14.6**, `PYTHONPATH=src:. ../verify-python314/bin/python -m pytest -q -W
error`: **2189 passed in 209.78 s**. `git diff --check` clean. No market-data calls,
credential reads, iMac changes, orders or runner activation. Git/documentation
network access only. Independent re-audit remains required, then exact-commit iMac
and bounded live acceptance. F1's accepted audit evidence is retained; this does
not close G5, resume Step 09 or implement O3–O5.
