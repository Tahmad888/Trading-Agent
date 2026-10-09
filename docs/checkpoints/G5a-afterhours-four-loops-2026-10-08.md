# G5 after-hours batch: four remaining loops

Taz explicitly authorized all four sequentially on 2026-10-08. Base e2cb9f5;
clean checkout and matching remote checked. Its Greek code is independently cleared
and Taz's iMac strict suite passed 2235 at that exact SHA. G5 OPEN; Step 09 paused.

## Requirements before implementation

1. Budget/reporting: keep diagnostic caps; remove the operational lifetime diagnostic
   cap. One explicit quote-refresh workload is one batched GET, with separate local
   minute-rate reservations using Tradier's documented production 120/sandbox 60
   market-data requests per minute. Local refusals invalidate affected requested
   components and old approvals, but are not persistent provider STOPs. Provider
   denials/transport failures retain explicit STOP/resume. Reserve before dispatch,
   disclose attempted dispatch versus confirmed send, never infer network send from
   an injected opener. Keep raw Greek rows labelled raw and separately link reduced
   current state. Do not change Greek recovery/freshness policy.
2. Identity/wiring: prepare crosswalk candidates from authentic saved Webull and
   Tradier metadata with file hashes; never install or approve from a diagnostic.
   Human review is still required. Add zero-provider readiness checks for required
   composition inputs and meaningful synthetic consumer acceptance; missing real
   account/status/contract/market adapters remain missing, not invented defaults.
3. Sizes: check current first-party endpoint definitions separately. Public docs
   still say hundreds for REST and give no stream quote-size unit; retained matched
   observations do not certify units. Prepare exact support inquiry and observation
   evidence packet, without sending a message absent explicit authorization. Enable
   no conversion until the contradiction is resolved.
4. Greeks/saved evidence: verify documented dxFeed units and reanalyse authentic saved
   capture data offline; identify source/contract/time/index/flags, preserve exact
   decimals and distinguish historical observations from current eligibility. Compare
   model calculations only as supporting analysis with explicit assumptions; never
   infer units/timezone or option stop valuations from a fitting result.

Evidence sources checked: https://docs.tradier.com/docs/rate-limiting (per-token
one-minute policy, header fields), https://docs.tradier.com/docs/quotes (in-hundreds
wording and Greek fields), https://docs.tradier.com/docs/streaming (separate event
schema), https://docs.dxfeed.com/dxfeed/api/com/dxfeed/event/option/Greeks.html.
Rate limiting design is conservative engineering within documented limits, not a
trading rule or a claim about all retail traders. Other programs using the same token
still consume provider quota; no local ledger proves unused external capacity.

Acceptance: >12 healthy operational refreshes; unchanged diagnostic cap; shared/reopen
rate reservations; local exhaustion blocks the affected final refresh and changes
approval evidence, healthy peers isolated, later workload recovery cannot revive old
approvals; provider STOP unchanged; no request under quote fence. Reporting must not
call external pre-send rejection a sent request. Mapping failures/contradictions and
missing trusted adapters stay unavailable. Offline history cannot become live state.
Run focused checks then one combined strict suite; record actual outcomes. No order,
account request, activation, scheduler, guessed mapping or unit conversion.

Rollback: revert this batch's commits; preserve quote evidence and reservations as
audit history. Current-code live acceptance and provider answers remain separate
requirements even if every offline check passes.

## Results and individual dispositions

Developer verification, Python 3.14.6: 88 focused cases passed; final combined
`python -m pytest -q -W error` passed **2265** in 213.14 seconds. The first combined
run had one old raw-report key assertion fail (2262 passed); its expectation was
updated for the intentional `current_state` -> `summary_scope` report change.
A newly added identity-conflict fixture initially altered a stock instead of the
audited option. It was corrected and passed before the final seven batch mutations
and combined suite. An intermediate full run was interrupted after that fixture
failure; it is not acceptance evidence. Final evidence is
`../evidence/G5a-afterhours-offline-2026-10-08.json`.

Existing seven Tradier quote/ticket mutations and seven new batch mutations are
caught in disposable copies. Import/collection errors do not count. New tests
exercise repeated ticket checks, same-store concurrent/reopened minute reservations,
local failure isolation, generation invalidation/recovery, no budget reset by mode
or ledger switching, and incomplete/contradictory mapping and saved identity data.

The same synthetic consumer probe against e2cb9f5 passes 12 of 20 checks, first
fails at 13, and records STOP. On this repair it passes 20 of 20, uses operational
workloads and records no global STOP. Diagnostic lifetime limits are unchanged.
No actual market-data request, order, account query, schedule or iMac edit occurred.

| Loop | Completed offline | Still required |
|---|---|---|
| Request budgets/reporting | Operational composer and persisted minute reservations; local vs provider refusal; raw/reduced summary labels; regression and mutation verification. | Independent review and new-commit actual-host/consumer acceptance. |
| Identity/wiring | Webull/Tradier issuer capture retention, non-installable hashed drafts, zero-call configuration inventory, meaningful synthetic signal/ticket consumer checks. | Authentic complete issuer captures and human-reviewed real crosswalks (including existing tastytrade review path); independently supplied real account/status/OI/contract/market adapters. These are not created from placeholders. |
| Advertised sizes | Current first-party REST/stream schemas rechecked; contradiction and exact support questions documented. No conversion or capacity arithmetic enabled. | Dated Tradier clarification of each transport/security type, followed by compatible observations and reviewed exact-field implementation. Inquiry prepared, not sent. |
| Greek conventions/saved analysis | dxFeed definitions rechecked; six authentic indexed Greek observations verified and ages recomputed; contradictory identity captures refused. | Current reducer's live delivery acceptance; Tradier provisional scaling/timezone clarification. The incomplete saved measurement log cannot establish a complete current snapshot. |

Raw source archive located on this MacBook:
`/Users/talhaahmad/Downloads/g5-webull-followup-YRbDz4/comparison-evidence.zip`.
Extracted/recomputed evidence is outside Git at
`../afterhours-evidence-2026-10-08/`. No raw private capture is committed.
The old Webull/Tradier reports omit complete issuer metadata, so SPY/QQQ/NVDA
produce INCOMPLETE_CAPTURE_EVIDENCE, with zero mappings installed. The readiness
configuration result describes this developer process only, not the configured iMac.
The saved tastytrade capture has six observations for two SPY options with own
source-to-original-receipt ages 101.271857–200.967142 seconds. It does not restore
live state, establish a maximum Greek age or measure quote-feed delay.

G5 -> G5a -> parent CP3 remains the same outline. e2cb9f5's accepted 2235-test iMac
result remains valid for that commit; it does not verify this newer batch. Current
RTH comparison, opening-window consolidated volume, scheduled Friday/close and
revision/rebuild acceptance, real mappings/consumer wiring and other existing parent
rows retain their separate open status. Step 09 stays paused. This is not all-four
external closure, G5 sign-off or trading activation.
