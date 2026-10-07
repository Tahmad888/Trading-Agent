# Local tickets, approval and single-use consumption (G4)

Serves the **approve** step. `desk.tickets` prepares a ticket from a request,
shows it in plain words, records Taz's explicit approval or rejection, revokes an
approval, shows state and audit history, and consumes an approval once through a
local test interface. **Nothing here submits, routes or simulates an order.** A
successful consumption means "these exact approved terms were still valid and have
now been used once"; broker submission, idempotency and fills are Step 15/16.

## Commands

```
python -m desk.tickets [--db PATH] [--adapters module:factory] <command>

prepare REQUEST.json           prepare version 1 and display it
revise TICKET REQUEST.json     prepare the next version (supersedes earlier ones)
show TICKET [--version N]      display a version (latest by default)
approve TICKET --version N --actor NAME   display, then type the budget and each warning code
reject TICKET --version N --actor NAME --reason TEXT
revoke TICKET --version N --actor NAME --reason TEXT
consume TICKET --version N --request-id ID   local single-use test interface
history TICKET                 append-only audit, one JSON line per event
list
```

- Storage: `--db`, else `DESK_TICKET_DB`, else `data/tickets.sqlite` (`data/` is
  git-ignored). One SQLite file holds ticket versions, approval records,
  consumptions and the audit table. Triggers make the audit table append-only.
- Adapters: `--adapters` or `DESK_TICKET_ADAPTERS` names a factory returning
  `RiskInputs`: the `RiskStateStore` account snapshots (including the manual stop),
  the event terms source (`EventRiskSource` in production shape), market context,
  contract metadata and current market observations. `prepare`, `revise`,
  `approve` and `consume` refuse to run without one. **No live factory exists yet**:
  wiring real account, market-context, contract and quote adapters is Step 11/13/
  15/20 work. `tests.ticket_support:demo_inputs` is a labelled synthetic demo.
- Approver name: `--actor` is required for approve, reject and revoke; there is no
  default name (the earlier `DESK_APPROVER`/`Taz` default was removed after the
  2026-10-02 audit). The OS login is recorded too. `approve` refuses unless standard
  input is an interactive terminal, so a script or pipe cannot answer the prompts.
  This is an engineering control, not authentication: the name is still a label
  and anyone at the keyboard can type it. Out-of-band confirmation (the Telegram
  approval path) is Step 13 work. Library callers of `TicketStore.approve` are
  trusted code; automated fixtures must use `fixture:` actors.
- Approval lifetime: `DESK_APPROVAL_LIFETIME_SECONDS` (1 to 3600, default 120).

The request JSON is `TicketRequest`: account, environment (`paper` or `live`),
event id, structure, legs (symbol, side, limit price, quantity), budget, sizing
mode, cost reserve, time stop, exit rules, quote source and optional plan/tier/
grade/sector. Share tickets default to `stop_budget`, and a share quantity is
rejected unless `sizing_mode` is `selected_quantity`. Option tickets must name
their sizing mode and contract quantities (engineering choice). Stop, target,
setup and instrument always come from the independently resolved event.

## What the ticket shows

Sizing mode in plain words; the entered budget; requested and final quantity;
entry limit; structural stop; target or "none set by the setup"; estimated stop
loss (or why it is unavailable); reserved costs; stop loss including costs; share
position value as information, or option strategy exposure and net premium; the
funding estimate labelled as not broker-verified; exits; failed blocking checks;
the warning levels it was checked against; and each warning with its meaning, values
and acknowledgement code. The next
earnings date, timeframe notes and option rationale (CLAUDE.md rule 4) are shown
as not supplied: they depend on Step 09 and Steps 11–13.

## State transitions

| From | Event | To |
| --- | --- | --- |
| (new) | prepare, all checks pass | pending |
| (new) | prepare, any blocking check fails or evidence is unavailable | blocked |
| pending | approve (exact budget + exact warning codes + rerun matches) | approved |
| pending, blocked | reject | rejected |
| approved | revoke | revoked |
| pending, blocked | event validity ends | expired |
| approved | approval expiry reached | expired |
| pending, blocked, approved | a newer version is prepared | superseded |
| approved | consume (rerun matches, still unexpired) | consumed |

Approval expiry is the earliest of the configured lifetime, the signal event's own
validity and contract-metadata validity. Quote, account, market and signal-terms
freshness are rechecked at approval and consumption, at the final clock, rather
than shortening expiry, because their receipt times refresh without changing the
terms.

**Lifetime rationale (Assumption):** consumption reruns every check, so the
lifetime limits how stale Taz's intent can be, not data freshness. 120 seconds
follows the earlier `ApprovalRecord` design note; it is not trading research.

## Binding

`binding_sha256` covers ticket/version, account, environment, event id and digest,
setup id/version and direction, instrument, structure, each leg (symbol, broker
contract id for options, side, requested/ceiling/final quantity, limit, expiry),
stop, target, time stop and exit rules, budget, sizing mode, cost reserve, every
disclosure (stop loss and basis, totals, exposure, premium, position value, funding
estimate), every warning's code, message, value and threshold, and the account-warning
policy the ticket was checked against (`warning_policy`: the daily loss, weekly loss
and drawdown thresholds, the 10% band and how each is measured). It excludes
receipt timestamps, so fresh evidence with unchanged terms gives the same binding.
`RiskDecision.terms_sha256` includes receipt times and is not an approval binding.

Approve and consume reload the stored ticket and rerun `risk.evaluate` against the
current account snapshot and fresh evidence; the rerun binding must match the
stored one, and the approval record's snapshot must equal the stored binding. Any
changed term, contract id or event revision refuses with a message to prepare a new
version, and so does any change to a warning other than the three account warnings
below.

**Account warning band (User policy, Taz 2026-10-02; his suggested starting policy,
not a researched trading rule).** For `daily_loss`, `weekly_loss` and
`account_drawdown`, small P&L moves no longer refuse. The ticket is re-asked
(refused, prepare a new version) when:

- a warning appears that the ticket did not show;
- a warning's threshold or wording changes;
- a warning has worsened from the value shown and acknowledged on the ticket by 10%
  of its threshold or more. At the defaults that is $20 more daily loss, $40 more
  weekly loss or 1 percentage point more drawdown.

Deterioration is cumulative from the acknowledged value; the baseline never resets
on later updates. Any change to the bound `warning_policy` re-asks, even when it
makes a warning disappear or the ticket showed no warning at all (Astra's re-audit of
`5e24e02`: without the policy, a warning missing from a later check could mean an
improvement or a relaxed threshold, and the two could not be told apart). Refusing a
no-warning ticket on a policy change is a deliberate fail-closed engineering choice;
it costs one new version. An improvement, including the warning clearing, does not re-ask.
Each successful approval or consumption records the values its final check saw
(`warning_values` in the audit), and the ticket display shows them as "latest final
check" next to the acknowledged value. Fresh account evidence, funding checks and
the manual stop stay mandatory in every rerun. Every other warning still has to
match exactly (G4.C).

**Price increments (User policy, Taz 2026-10-02).** An invalid limit is refused when
the ticket is prepared; the desk never rounds it.

- Shares: whole cents at $1.00 and above, $0.0001 below. Sourced: SEC Rule 612,
  https://www.law.cornell.edu/cfr/text/17/242.612. The amended half-cent tier is
  not assumed: its compliance was delayed to November 2027
  (https://www.sec.gov/files/rules/exorders/2026/34-105656.pdf).
- Options, every leg: whole cents at most.
- Single-leg options: the class's simple-order schedule. Sourced: Cboe Rule 5.4(a)
  as quoted in SEC filings (e.g. https://www.govinfo.gov/content/pkg/FR-2025-12-22/html/2025-23533.htm):
  Penny Interval Program classes $0.01 below $3.00 and $0.05 at $3.00 and above;
  QQQ, SPY and IWM $0.01 at every price; other classes $0.05 below $3.00 and
  $0.10 at $3.00 and above. The class comes from the adapter's contract book
  (`OptionContract.price_increment`); when it is missing the ticket is blocked, not
  guessed.
- Multi-leg options: whole cents. Sourced: Cboe Rule 5.33(f)(1), complex-order bids
  and offers and their legs trade in $0.01 increments
  (https://www.sec.gov/files/rules/sro/c2/2022/34-95342.pdf). This assumes the
  later order adapter (Step 13/15) sends a multi-leg ticket as one complex order;
  sent as separate simple orders, each leg would need its class schedule
  (engineering Assumption, for that step to check).

Each check reads the signal terms once; the risk rerun validates that same snapshot
(G4 fix 2). Limit, stop and target are displayed exactly as accepted, e.g. a
stop of `$248.7512`, never rounded to cents (fix 5). A ticket whose time stop has passed is
blocked at prepare, cannot be approved or consumed, and an approval never outlives
the time stop (fix 4).

### The final transaction (revised after the 2026-10-02 audits)

Provider calls (account load, terms resolution and revalidation, contract book,
quote, market context) all happen in the check, before any lock. The final
approve/consume step then takes three locks in a fixed order and holds them only
for local computation and the ticket write:

1. the ticket store (`BEGIN EXCLUSIVE`, revised after Astra's re-audit);
2. the signal store, through the terms source's `held_event` (`BEGIN IMMEDIATE`);
3. the account store, through `RiskStateStore.held_account` (`BEGIN IMMEDIATE`).

Under those locks it reads a fresh clock (`RiskInputs.clock`, else UTC wall clock,
never earlier than the check's start), re-reads the event's eligibility and digest
and the current account snapshot, and reruns `risk.evaluate` at that clock on the
same quote, market, terms and contract snapshots. Every age limit (account 30 s,
quote 60 s, market context 60 s, signal terms 60 s and their event deadline,
contract metadata) is therefore judged at the moment of the write, with the
existing boundaries unchanged. Approval or consumption commits only if the event is
still eligible with the same digest, every check still passes and the binding still
equals the stored one. The ticket write commits before the signal and account locks
are released, so an invalidation, suspension, withdrawal, revision, snapshot or
manual stop either shows in the final check or lands after the write.

**No reader can delay the write after the final clock (re-audit P1b).** In
rollback-journal mode `BEGIN IMMEDIATE` lets existing readers keep their shared
locks, and COMMIT then waits for them; a reader could therefore delay the commit
after the freshness check had passed. The ticket store now opens the final
transaction with `BEGIN EXCLUSIVE`, before the signal and account locks and before
the clock is read, so any wait for readers happens first and the clock reflects it.
After the clock come only local computation, the writes and a commit that needs no
further lock. In WAL mode readers never block a commit and `EXCLUSIVE` behaves like
`IMMEDIATE`. Both modes are tested with a real reader holding a read transaction
during the check (`tests/test_reaudit_closure.py`): with account, quote, market and
signal-terms evidence half a second inside its limit and a one-second reader, the
rollback-mode write is refused (the clock is read after the wait) and the WAL-mode
write succeeds; the approval-lifetime and event deadlines are covered the same way.
While the exclusive lock is held, other readers of the ticket store wait (up to the
10 s busy timeout) for one final computation and commit.

`BEGIN IMMEDIATE` on the signal and account stores takes SQLite's single writer lock
in both rollback-journal and WAL modes, so the account and signal fences do not depend on the journal mode, and the
stores never rewrite an operator's mode. The account lock is taken last, so a manual
stop never waits behind a ticket operation that is itself waiting for a lock; it
waits at most for one final computation and commit. A routine account snapshot that
changes nothing in the binding no longer refuses the ticket: the final rerun uses
the current snapshot. `set_manual_halt(True)` has no precondition: neither a newer
revision nor the as-of time of a routine snapshot that committed while the stop
waited for the lock can refuse it (re-audit P1a). The audit keeps the request time
(`at` and `requested_at`), the commit time read under the lock (`committed_at`;
UTC wall clock unless a clock is passed) and the snapshot's `snapshot_as_of`. It
retries for up to 30 s; if the store stays busy it raises `RiskStateError` saying
the stop was NOT recorded. A resume still requires the current revision and a
request time no earlier than the snapshot. A terms
source without `held_event` cannot approve or consume (fail closed).

Successful approvals record the final clock as `decided_at`; consumptions record it
as `consumed_at`. These mechanics are engineering decisions (Assumption), not trading
research.

### Single use

A consumption is a row in the insert-only `consumptions` table (primary key
`approval_id`; triggers refuse UPDATE and DELETE). Consume and replay read that row
and also refuse when the append-only audit already has a `consumed` event for the
version, so editing the ticket and approval rows cannot reopen a spent approval.
The permission id hashes the approval id, the request id and `binding_sha256`, and
the outcome carries `binding_sha256` so a later order adapter (Step 15) can verify
it against the stored ticket rather than trusting the payload.

## Failure messages (all print `REFUSED: ...` and exit 2)

- `Budget confirmation does not match the ticket budget of $X` (wrong, missing or malformed amount)
- `Acknowledge exactly the warnings displayed on this ticket version: <codes>`
- `Ticket failed blocking checks and cannot be approved`
- `Ticket terms or warnings changed since this version was prepared (<why>); prepare a new version`, where `<why>` is e.g. `new warning daily_loss`, `warning daily_loss threshold changed` or `warning daily_loss worsened from $250.00 against $200.00 to $270.00 against $200.00, ...`
- `Recheck failed: blocking checks failed: <rules>` or `independent evidence unavailable or invalid (<type>)`
- `Ticket version is <state>; there is no usable approval` (rejected, revoked, expired, superseded, pending)
- `Approval was already consumed by another request`
- `Approval record is unknown, legacy or does not match this ticket version`
- `Current terms, evidence or warnings differ from the approved ticket (<why>)`
- `The ticket's time stop has passed; no new entry is allowed`
- `Approval has expired` (also judged by the fresh final-transaction clock)
- `Final check at <time> failed: <rules>; nothing was consumed` (or approved), e.g. `not_halted`, `quote_fresh`, `account_state_fresh`
- `The signal event is no longer eligible at the final check (invalidated, suspended, closed, expired or revised)`
- `Terms, evidence or warnings changed by the final check (<why>); prepare a new version`
- `The signal source cannot hold the event for the final check; nothing was changed`
- `The audit trail shows this approval was already used; the record is inconsistent`
- `Approval needs an interactive terminal; piped or scripted input cannot approve a ticket`
- `Evidence validity ended during the approval check; nothing was approved`
- `Automated fixtures must be labelled 'fixture:'; terminal actors must not be`
- `No trusted adapters configured ...`
- At prepare (invalid request): `Limit price <x> is not a valid price increment: whole cents at $1.00 and above, $0.0001 below (SEC Rule 612). The desk does not round it; enter a valid limit` (options: `whole cents or coarser for options`)
- Blocked at prepare: `limit $<x> is not a multiple of $<step>, the minimum increment for this option (<class> class, Cboe Rule 5.4(a)); the desk does not round it` or `option price increment class for <symbol> is unavailable; the limit cannot be checked`

## Migration and rollback

`tickets.sqlite` is new; no existing store is rewritten. The old `ApprovalRecord`
shape had no producer and now fails validation, so a legacy or partial record can
never be consumed. Rollback: revert the G4 commit. Older code ignores
`tickets.sqlite`; keep the file for its audit history. Restoring G4 later reopens
the same file. Schema `desk-tickets-v2` adds the `consumptions` table: opening a
`desk-tickets-v1` file copies every earlier consumption into it and retags the file,
keeping all rows and the audit history; any other tag is refused, not reinitialised.
Code older than v2 refuses a v2 file (unsupported schema), which fails closed.
A ticket prepared before the warning-policy snapshot existed (no `warning_policy` in
its stored binding) cannot be approved or consumed: it is refused with "this ticket
predates the warning-policy snapshot" and needs a new version. An absent current
warning is never taken as proof that the policy is unchanged.
Someone with write access to the file can still drop triggers and forge consistent
rows; the local file is not a signed ledger. A keyed signature was suggested by the
outside review; it needs a key kept outside the database and is not built here.

## Tradier quote composition (G5a Step 2, 2026-10-07)

See [Tradier ticket quote integration](TRADIER_TICKET_QUOTES.md) for the opt-in
composer and explicit CLI factory. It refreshes quotes before revalidation and
fences durable health/mappings plus every option BBO through final commit. It does
not provide fabricated account, market, contract, OI or halt evidence. Separate
healthy REST commands preserve approvals; failures/revocations require new versions.
Tastytrade connection-session rules stay unchanged. Chosen contract quantities and
verified multipliers remain in loss arithmetic; advertised quote sizes do not.
