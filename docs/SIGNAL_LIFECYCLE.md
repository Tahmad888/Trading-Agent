# Signal lifecycle — Step 07

The scanner's `data/signals.sqlite` is the durable signal record. It contains no
approval tokens, broker orders or positions. Keep it alongside the scan log and
account/action databases; don't delete it to retry a scan.

## Interfaces and ownership

`ScanLog` creates/opens the store. `run()` consumes completed bars and persists
observations transactionally. A repeated scan may refresh evidence but cannot
create another copy of the same trigger. `ScanRecord.triggered` contains newly
observed events still surviving at the end of that scan, each with event_id,
trigger_at, observed_at, expires_at and valid_until.

`entry_hit` and `intraday_scan` without a store are historical diagnostics retained
for existing inspection/tests. They do not issue durable IDs or approval eligibility.
Runtime `run()` always supplies the store. Do not build approval logic on their
stateless results or on a copied JSON log row.

`SignalStore.events(now)` and `get(event_id, now)` return history and current signal
freshness. `history(event_id)` returns transitions; `armed_history(day)` retains
versions of the armed list. Trigger time is the completed crossing bar's end,
not a claimed tick timestamp; observation time may be later after a missed scan.
Bar hashes detect revisions to already-consumed evidence.

Before future approval AND execution, the trusted adapter must use
`scanner.revalidate_signal(source, log, event_id, now, symbol=..., price=...,
quote_at=..., decision_clock=...)` with the broker's current underlying quote.
This re-fetches completed bars, rechecks corporate-action compatibility and validates
current state, card version, entry/stop/chase direction and quote age. The result
is only a signal prerequisite at checked_at, never a reusable approval. Step 13
still owns exact terms, expiration and single-use approval; Step 15 owns orders.
Full setup/catalyst/risk/broker gates remain separate requirements.

The existing 3% risk-layer move limit is used, plus a card chase limit if stricter.
No new chase percentage or per-card tuning is introduced. The current-card
fingerprint captured at setup evaluation must survive load unchanged.

## State and re-entry

Armed candidate -> triggered event -> invalidated, expired or explicitly closed.
Missing/invalid data suspends eligibility without pretending the setup failed.
A valid refresh can recover that same event, without issuing a second trigger.
A close back through the entry invalidates the breakout; a later fresh crossing
can create another event the same day. Both long and short comparisons apply.
A stop touch blocks that candidate. A newly evaluated candidate can resume from
new observations, never by replaying the old crossing. A changed price/action
basis or card also requires fresh setup evaluation.

`close(event_id, now, reason)` records that a downstream consumer finished with
an event. It is not an order-close command. A later neutral-side completed bar
and subsequent new crossing are required before re-entry. Every future entry
still requires Taz's approval; there is no one-trigger-per-day cap.

Signal freshness lasts until the next expected M15 close. Even without new bars,
reads deny eligibility then; `run()` also advances expiry outside scheduled slots.
Candidates expire at their entry session close, including early closes. A later
day needs a newly evaluated setup. No 11:00 ET cutoff was added.

## Migration, recovery and limits

Legacy armed-YYYY-MM-DD.json files import once into SQLite as candidate inputs;
original files stay intact. Legacy records lacking a setup fingerprint require
rebuilding. Historical scan logs never become approvals. New armed changes are
transactional and recorded in armed_history; imported unknown receipt times stay
unknown instead of being fabricated.

SQLite transactions/unique IDs protect event creation under concurrent calls and
rollback partial writes. This does not establish a single operational runner or
exactly-once external notifications. Those remain later operational work.
If JSONL writing fails, recover events from SQLite. A retry must not manufacture
a duplicate to repair the funnel; JSONL is a scan report, not an authoritative
message queue. If the database cannot be read/written, fail closed and recover it;
never replace it with a blank database to resume automatically.

For rollback, stop the runner, preserve signals.sqlite, revert scanner/store changes
together and rebuild the next-session armed list. A leftover legacy JSON file may
be outdated. This development checkpoint did not start any runner.

## Step 09 fundamental qualification

The store's `eligible` field remains technical lifecycle state. Scanner trigger
output adds separate fundamental qualification; `revalidate_signal` enforces
required earnings/catalyst gates and records the evaluation in
`earnings-reviews.jsonl`. Persisted chart eligibility is not a substitute for this
fresh check. See `EARNINGS_EVIDENCE.md`; no approval/order is granted here.
