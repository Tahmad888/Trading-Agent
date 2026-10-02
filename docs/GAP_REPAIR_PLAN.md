# Agreed gap repairs before resuming Step 09

2026-10-01. Base 9ba1cb8. Taz authorized recording the plan and resuming repairs.
Work one checkpoint at a time; verify and push each completed checkpoint to
`codex/repair-step-01-baseline`. No implicit merge, live activation or orders.
Step 09 remains incomplete. Claude and DeepSeek reviewed the plan via Taz; neither
has reviewed the resulting implementation yet. No messages have been sent to either
reviewer by this task.

**Active ownership (Taz, 2026-10-02):** Claude implements; Astra (Codex) audits.
This supersedes "Codex owns shared contracts and integration" from 2026-10-01.
G1–G3 remain Codex-implemented as their checkpoints record; G4 onward is Claude's
implementation pending Astra's audit. Self-review is not Astra's approval.

G1 status: implemented and locally verified, **803 strict tests** on Python 3.12
and 3.14. See `checkpoints/G1-earnings-isolation.md`. G2 is implemented and locally verified: **856 strict tests** on both Python
versions. Evidence is recorded in `checkpoints/G2-stop-risk.md`.
G3 automatic vendor price/history handling is implemented and locally verified
(**892 strict tests on both Python versions**);
see `checkpoints/G3-vendor-basis.md` and `VENDOR_BASIS.md`. Actual-host acceptance
is pending; unknown volume evidence and unresolved price conflicts stay explicit.
G4 local ticket approval is implemented and locally verified by Claude, awaiting
Astra's audit: see `checkpoints/G4-ticket-approval.md` and `TICKETS.md`. G2 hashes
are not G4 approval. Astra's G3a follow-up (`da33b59`, `checkpoints/G3a-targeted-rebuild.md`)
is integrated; its provider checks remain open. G5 combined verification (Taz asked to
continue, 2026-10-02) is implemented by Claude with the G4 audit still pending: see
`checkpoints/G5-combined-verification.md` for the end-to-end evidence and tracked
findings. Step 09 has not resumed; it waits for the audits and Taz's go-ahead.

## Latest user policy

Show candidates and their limitations rather than hiding opportunities behind a
hardcoded dollar limit. For options, show full premium/strategy exposure, the exact
planned exit and a supported stop-loss estimate (or explicitly unavailable).
Taz chooses quantity/exposure and confirms the exact final ticket. Do not silently
exceed an approved amount, infer unlimited consent, or force maximum-loss sizing
as the only way to consider an option. Full exposure and estimated stop loss must
have separate labels; the word "risk" alone is insufficient.

An option-price exit allows conditional loss arithmetic. Translating an underlying
stock stop into a future option-dollar loss needs a validated estimate; a valuation
model is useful for that feature, but not a prerequisite to displaying a trade and
accepting its disclosed exposure. No fabricated stop-loss amount. Retain calls,
puts, verticals, calendars and condors in scope, with structure-specific checks.
HALF and approved account/market conditions remain warnings. The separate explicit
manual stop remains blocking. Budget acknowledgement belongs in ticket approval.

## Sequence and exit evidence

| Checkpoint | Change | Acceptance |
| --- | --- | --- |
| G1 | Separate earnings configuration/refresh failures from working price data. | Malformed policy/cache, startup/refresh/read failures leave unrelated technical setup checks working; EP and cup remain pending; prices/market failures are not weakened; safe diagnostics persist. |
| G2 | Fix actual breakout/EP stop construction and the risk trust boundary. | Use session evidence available at the decision for the card's day-low stop; do not use an ADR-distance substitute or future session low. Resolve versioned event evidence independently. Size shares from executable entry limit, supported stop and costs. Test wrong-side/altered stops, chased entry, short-direction arithmetic without silently enabling unsupported short execution, and changed approval terms. Implement option exposure disclosure/selection under the user policy above. |
| G3 | Remove daily manual corporate-action enrollment using a tested vendor-basis design. | Compare overlapping history against the history used to arm each signal, rebuild on revisions, validate daily/minute compatibility and isolate ticker exceptions. Test pre-split stored trigger with self-consistent current prices, real gaps, ordinary/special dividends, identity changes and missing history. Establish source fallback only for unresolved cases; no automatic completeness claim. |
| G4 | Bind budget, warnings and executable terms to one approval and reconcile policy wording. | Budget/quantity/entry/stop edits invalidate approval; exposure and stop estimates stay distinct; no invented dollar cap; user warning overrides cannot validate corrupt data/arithmetic. Repo and external blueprint changes have separately recorded status. |
| G5 | Verify the combined repairs, then resume unfinished Step 09. | Integration evidence and review findings resolved. Whole-watchlist earnings queue, source budgets, per-symbol status, catalyst confirmation on the ticket and actual-host acceptance remain tracked. No Step 10 advance or operational-day count implied. |

G1 does not solve pre-existing broad market-filter dependencies or corporate-action
coverage. It preserves missing SPY/QQQ diagnostics and existing no-arm behavior;
it must show healthy ticker data still gets processed before that shared gate.
Broader independent-setup handling requires its own explicit dependency review.
G4 includes approval infrastructure that does not yet exist: completion requires
that implementation, not only a new warning flag in the risk engine.

## Evidence and corrections from review

- Checked: `risk._size` currently trusts caller-declared stop loss. Actual cards
  specify day-low stops while breakout/EP detectors use ADR-distance stops.
- Checked: earnings refresh/configuration shares the scanner price-source error
  handler. Malformed earnings configuration can replace good prices with NoKeys.
- Checked: existing earnings evaluator already prevents unknown required growth
  from qualifying. EP accepts supported EPS OR sales; cup's current gate is EPS.
  This is a regression requirement, not a newly confirmed missing check.
- Checked: the older corporate-action report exists on the Codex host Desktop,
  not necessarily on `/Users/taz` on the iMac. It confirms six adjustment dates
  for NVDA/SPY, not universal semantics. viaNexus observations include conflicting
  dates and dividend records in its split dataset. Connector access is not iMac
  API access. The later earnings capability report has not been inspected here.
- A current-price jump test alone is rejected: it can miss a stale pre-split
  trigger and falsely reject a genuine EP gap. Overlap/revision checks must be
  tested before replacing existing corporate-action safeguards.
- Never derive Q4 EPS by annual-minus-YTD subtraction. A normalized vendor field
  needs methodology verification. Revenue subtraction requires matching periods,
  units and accounting/restatement basis. Defer a general issuer-layout parser;
  unknown data does not pass a required growth gate.
- A price-only cup is not a qualified earnings-backed cup. Its blueprint fallback
  and separate classification must be verified before enabling it.
- Catalyst confirmation can be included in ticket approval; retain original source,
  publication/receipt times, relevance and evidence available at trigger.
- Ten operational trading days start only with a verified, authorized, recorded
  runner start. Paper/sandbox versus production labels alone do not prove freshness.

## Research basis (not claims of profitable performance)

Fidelity describes chosen dollar risk divided by per-share risk:
https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/learning-center/100721_Plan_%20your_escape_TRANSCRIPT.pdf
FINRA distinguishes stop trigger from execution and stop-limit nonexecution risk:
https://www.finra.org/investors/insights/stop-orders-factors-consider-during-volatile-markets
OIC describes theoretical estimates, not guaranteed future option values:
https://www.optionseducation.org/advancedconcepts/understanding-options-greeks
Webull documents adjusted daily and unadjusted minute bars:
https://developer.webull.com/apis/docs/reference/historical-bars/
SEC-filed disclosure explains quarterly EPS need not sum to annual EPS:
https://www.sec.gov/Archives/edgar/data/99302/000120677420001786/R18.htm
IBKR documents user-overridable precautionary size/value warnings; its thresholds
are not adopted for this desk:
https://www.interactivebrokers.com/en/trading/tws-order-presets.php/1000

Engineering cadence, tolerances and source selection require explicit evidence or
an identified assumption. No universal retail convention is inferred from these
sources. Existing studies/checkpoints are retained; scoped passes do not close
broader operational requirements.
