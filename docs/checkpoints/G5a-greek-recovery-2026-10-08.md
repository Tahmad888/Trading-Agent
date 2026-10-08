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
