# G2 — event evidence, sizing and exposure

This is a planning/risk interface. `RiskDecision.approved` means local eligibility;
`order_authorized` is always false. G4 still must implement exact-ticket approval.
There is no new broker order path, valuation model or activated runner.

## Required hand-offs

Schema 3 proposals explicitly name `event_id`, `stop_price` and `sizing_mode`.
`target_price` must match the event target, including None when there is no target.
Schema 2 proposals must be rebuilt. `max_loss_usd` is an optional assertion of the
full requested quantity's conditional stop loss; it never controls sizing. A false
assertion fails. `stop_estimate_source` is retained as caller annotation only;
`RiskDecision.stop_loss_basis` describes the independently supported calculation.

Supply `risk.evaluate(..., terms_source=...)` independently of proposal JSON.
`EventRiskSource(price_source, scan_log, quote_source)` loads the exact event,
uses scanner revalidation (current completed bars, action basis, quote, lifecycle,
card and required fundamentals), and resolves its immutable entry/stop/target.
The quote callback returns `(symbol, price, quote_at)` from the trusted underlying
quote adapter. Never implement it by copying proposal fields. Unknown events,
missing sources and failed/stale revalidation cannot pass. Fixed clocks are for
reproducible tests; runtime callers must supply a current decision clock and rerun
all checks immediately before approval/execution. G4/15 own those later boundaries.

The entry limit is the proposed executable price, checked against independently
resolved entry, current underlying quote, stop, target and original chase reference.
The existing 3%/stricter-card chase policy is retained. For EP the chase is measured
from the selected, frozen opening-range high (the event's entry level), with the
executable limit (Taz 2026-10-02, User policy); other setups measure it from the
candidate trigger. The 3% stays an Assumption. Scanner revalidation uses the same
reference (`risk_terms.chase_reference`). An option must have the same
directional exposure as its signal, or be a supported neutral structure. Short
share arithmetic is tested, but short share execution remains unsupported.

## Sizing choices and labels

- `stop_budget`: whole units within the chosen budget after reserving full declared
  round-trip estimated costs. Shares use `(entry limit - event stop) * shares`.
  No stop is moved to fit that budget. Options require independent conditional
  option exit prices for every leg; an underlying stock stop is insufficient.
- `maximum_loss_budget`: an optional explicit choice using the independently
  computed intact-strategy loss plus costs. It is not imposed on all options.
- `selected_quantity`: keep the requested quantities, subject to data, structure,
  liquidity and funding checks. Exceeding the entered budget produces a warning
  for exact-ticket confirmation, not a dollar cap. This is not consent to execute.

For example, three calls at $1.50 have $450 premium exposure. Independent planned
option exits at $1.30 imply $60 conditional price loss before costs. A stock-price
stop alone cannot establish that $60: the standard event adapter leaves that
estimate unavailable. Selected quantity and optional maximum-loss sizing still
work. Full exposure, conditional stop loss and costs remain separate fields.

The independent exit-price interface supports conditional arithmetic; it does not
supply a live option-exit feed or predict fills. Existing contract metadata checks
still verify standard deliverables and structures. Calendar bounds assume the
covered strategy remains intact; early assignment, legging/mishandling and actual
broker margin remain separate concerns documented in Step 04. Fees and slippage
reserves are estimates, not broker-confirmed costs. Stops do not guarantee their
execution price; stop-limit orders may not execute at all (FINRA source in G2).

Requested quantities/exposure remain in the decision even when local eligibility
fails. Final executable quantities are zero on failure. A missing option-dollar
estimate is None, never zero. HALF/account/market policies are unchanged.

## Event stop evidence

Breakout and EP armed candidates now have `stop=None`, `stop_basis=session_low`
and the completed-daily ADR%. At a completed M15 crossing, the event records the
session low observed through that bar and retains the original candidate separately.
That event stop cannot widen on later scans. Subsequent touches invalidate it.
An earlier low cannot rise; a future/incomplete bar cannot set it. The event is a
post-completion observation, not a claimed intrabar entry or fill.

ADR is a width check, not a stop-placement formula. The existing 1x breakout and
1.5x EP caps are applied with actual entry as denominator, including the proposed
share limit. This conversion is an engineering assumption recorded in G2.
Observed low precision is retained; broker tick/exit-order construction is later
work. A too-wide event remains recorded with `stop_width_valid=false`.

Cards 1/5 have new fingerprints. The SQLite migration adds event terms without
rewriting old history; legacy events missing those terms cannot become eligible.
Changed old observation hashes require rebuilding. Back up the database before
an actual-host upgrade; restoring pre-G2 code also needs its pre-G2 database backup.

The decision's `terms_sha256` binds proposal, resolved evidence and selected
quantities; edits change it. It is not an approval token, and because it includes
receipt times (such as `quote_as_of`) G4 does not use it as an approval binding.
Warning-review expiry also respects resolved evidence expiry.

## G4 changes (2026-10-02)

- Share tickets no longer emit `exposure_above_budget`: a long share position's
  full exposure is its position value, which always exceeds a stop budget. The
  decision reports `position_value_usd` as information. Options keep the warning.
  `stop_estimate_above_budget` (selected quantity over budget) is unchanged, as are
  `maximum_loss_budget` arithmetic and the buying-power/margin checks.
- `signal_event_valid_until` reports the event's own validity, separately from the
  receipt-freshness bound in `signal_terms_valid_until`.
- Exact-ticket approval, revocation, expiry and single-use consumption live in
  `desk.tickets`; see [TICKETS.md](TICKETS.md). `RiskStateStore.review/acknowledge`
  remain a codes-only warning-review audit and are not approval.
