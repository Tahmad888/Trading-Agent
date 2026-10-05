# G5a parent Checkpoint 3 — repairs from the 2026-10-05 live run

Hierarchy: G5 → G5a parent Checkpoint 3 → children 1–6. These are follow-up repairs and
measurement work inside that hierarchy (child 3 code paths; child 5 evidence), not new
parent checkpoints. Step 09 stays paused; G5 is not complete.

Inputs: Taz's handoff `CLAUDE_G5_LIVE_RUN_REPAIR_PROMPT_2026-10-05.md` and Astra's
independent audit of the live evidence (`G5_LIVE_TEST_INDEPENDENT_AUDIT_2026-10-05.md`,
with `G5_LIVE_CHAIN_REPRO_2026-10-05.json`), external attachments kept in the scratch
area. The live evidence was collected on `0ad7a1d` (cloud, Python 3.12.3; not the iMac).

Base: `e2373098bbf0ba943c6a998834af7169e3cf349a` (parent `0ad7a1d`), Claude's
overlapping-identity ordering repair (O1). **Astra has not yet reviewed `e237309`;
child 3 is not approved.** That commit is preserved; each package below is a separate
commit on top of it. No provider calls are made in this batch; scratch fixtures only.

Corrections Astra made to Claude's live report, accepted: the 16:14 close slot skipped
85 stale-history names and 5 invalid-row names (not 90 stale); the 16:37 scratch scan
covered 9 watchlist names (not the scheduled 90-name universe) and saved its candidates
under the source date, so it did not show Tuesday's desk armed; SPY's closing values did
not all agree (Summary 774.88, last RTH Trade 774.94, Alpaca and Webull 774.83); the
Greeks ages used the newest Trade time as the clock (one came out negative after the
close); "BRK.B never scans" overstated it (scoped discovery is a separate path).

Labels: *Checked* = observed in this repository or the saved evidence; *Sourced* =
provider documentation; *Assumption* = unverified; engineering choices are named as such
and are not retail-trading conventions.

## Package 1 — share-class option-chain lookup (written before code)

Trader-day step: analyze / plan.

- **Defect (Checked).** `ReadClient.resolve` sends a canonical Equity share-class symbol
  (`BRK.B`) to tastytrade as `BRK/B`, URL-encoded. `chain_options` canonicalizes the
  requested underlying (so `BRK/B` also becomes `BRK.B`), sends `/option-chains/BRK.B/
  nested` and compares the returned `underlying-symbol` literally with `BRK.B`. Astra's
  fixture reproduction: SPY available; `BRK.B` and `BRK/B` both request the dotted path
  and refuse a valid `BRK/B` Standard chain (`OPTION_CHAIN_UNAVAILABLE`).
- **Requirement.** One shared Equity provider-boundary conversion (canonical `BRK.B` →
  tastytrade `BRK/B`, encoded as one path component `BRK%2FB`) used by the instrument
  lookup and the chain lookup (Sourced: tastytrade symbology documentation — Equity
  share classes use a slash; options have their own OCC and streamer symbols). Returned
  chain underlyings are compared through the canonical Equity form; classes stay
  distinct (`BRK/A` never satisfies `BRK.B`). Option identifiers (OCC symbols with their
  spaces, streamer symbols) are returned exactly as received; no Equity slash
  substitution touches them. Standard-only chains, expiry/strike parsing (Decimal),
  contract validation and request-stop behaviour (401/403/429/budget) are unchanged; no
  retry is added.
- **Ambiguity (engineering choice).** A reply whose matching Standard chains give the
  same (expiry, strike) two different call or put symbols refuses with
  `OPTION_CHAIN_AMBIGUOUS` rather than silently choosing one. Identical duplicates are
  merged, as before.
- **Producers/consumers.** `ReadClient.resolve`, `ReadClient.chain_options` (and its
  caller `chain_pair`, `quote_check.diagnostic`). New helper
  `tastytrade_quotes.equity_provider_symbol`.
- **Acceptance (offline).** SPY control; `BRK.B` and `BRK/B` request
  `/option-chains/BRK%2FB/nested` and accept a valid matching `BRK/B` Standard chain;
  `BRK/A`, another underlying, a malformed underlying and an ambiguous reply cannot
  satisfy `BRK.B`; non-Standard chains excluded; expiry and Decimal strikes intact; OCC
  symbols returned verbatim; denial, rate limit and budget stop as before with no extra
  request. The live `BRK/B` lookup is a later bounded provider check, not done here.
- **Rollback.** Revert the package commit; nothing is stored.

### Package 1 record

Changed: `src/desk/tastytrade_quotes.py` (`equity_provider_symbol`),
`src/desk/tastytrade_transport.py` (`resolve` uses it; `chain_options` requests the
provider symbol, compares canonical underlyings, refuses conflicting contracts),
`tests/test_share_class_chain.py` (new, 22), `docs/TASTYTRADE_QUOTES.md`.

Before (base `e237309`, the new tests copied onto it): 10 failed (every share-class
request, the encoded-path stops, the shared resolve conversion), 12 controls passed.
After: 22 passed. The eight quote test files: 240 passed on Python 3.12.3 and 3.13.14.
