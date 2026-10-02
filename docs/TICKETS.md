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
approve TICKET --version N     display, then type the budget and each warning code
reject TICKET --version N --reason TEXT
revoke TICKET --version N --reason TEXT
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
- Approver name: `--actor`, else `DESK_APPROVER`, else `Taz`, plus the OS login. A
  local terminal is not authentication; these are audit labels.
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
and each warning with its meaning, values and acknowledgement code. The next
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
validity and contract-metadata validity. Quote, account and market freshness are
rechecked at consumption rather than shortening expiry, because their receipt
times refresh without changing the terms.

**Lifetime rationale (Assumption):** consumption reruns every check, so the
lifetime limits how stale Taz's intent can be, not data freshness. 120 seconds
follows the earlier `ApprovalRecord` design note; it is not trading research.

## Binding

`binding_sha256` covers ticket/version, account, environment, event id and digest,
setup id/version and direction, instrument, structure, each leg (symbol, broker
contract id for options, side, requested/ceiling/final quantity, limit, expiry),
stop, target, time stop and exit rules, budget, sizing mode, cost reserve, every
disclosure (stop loss and basis, totals, exposure, premium, position value, funding
estimate) and every warning's code, message, value and threshold. It excludes
receipt timestamps, so fresh evidence with unchanged terms gives the same binding.
`RiskDecision.terms_sha256` includes receipt times and is not an approval binding.

Approve and consume reload the stored ticket and rerun `risk.evaluate` against the
current account snapshot and fresh evidence; the rerun binding must equal the
stored one, and the approval record's snapshot must equal both. Any changed term,
warning value, contract id or event revision refuses with a message to prepare a
new version.

Each check reads the signal terms once; the risk rerun validates that same snapshot
(G4 fix 2). The final approve/consume transaction holds the account store's current
revision while it writes: if the revision differs from the one the check read (a
manual stop or a new snapshot arrived mid-check), nothing is approved or consumed
(fix 1), and a control arriving later waits for the write. That transaction takes a
fresh clock reading (`RiskInputs.clock`, else UTC wall clock, never earlier than the
check's start) for the expiry and time-stop tests (fix 3). A ticket whose time stop
has passed is blocked at prepare, cannot be approved or consumed, and an approval
never outlives the time stop (fix 4). Limit, stop and target are displayed exactly
as accepted, e.g. `$250.0049`, never rounded to cents (fix 5).

## Failure messages (all print `REFUSED: ...` and exit 2)

- `Budget confirmation does not match the ticket budget of $X` (wrong, missing or malformed amount)
- `Acknowledge exactly the warnings displayed on this ticket version: <codes>`
- `Ticket failed blocking checks and cannot be approved`
- `Ticket terms or warnings changed since this version was prepared; prepare a new version`
- `Recheck failed: blocking checks failed: <rules>` or `independent evidence unavailable or invalid (<type>)`
- `Ticket version is <state>; there is no usable approval` (rejected, revoked, expired, superseded, pending)
- `Approval was already consumed by another request`
- `Approval record is unknown, legacy or does not match this ticket version`
- `Current terms, evidence or warnings differ from the approved ticket`
- `The ticket's time stop has passed; no new entry is allowed`
- `Approval has expired` (also judged by the fresh final-transaction clock)
- `Account changed during the check (for example a manual stop or a new snapshot); nothing was consumed` (or approved)
- `Evidence validity ended during the approval check; nothing was approved`
- `Automated fixtures must be labelled 'fixture:'; terminal actors must not be`
- `No trusted adapters configured ...`

## Migration and rollback

`tickets.sqlite` is new; no existing store is rewritten. The old `ApprovalRecord`
shape had no producer and now fails validation, so a legacy or partial record can
never be consumed. Rollback: revert the G4 commit. Older code ignores
`tickets.sqlite`; keep the file for its audit history. Restoring G4 later reopens
the same file (schema tag `desk-tickets-v1`; a different tag is refused, not
reinitialised). A forger with write access to the database file could construct
a consistent record; the local file is not a signed ledger.
