# G5a CP3: Tradier quotes in signal and ticket checks

Active scope: Taz's 2026-10-07 authorization to implement Step 2. Base:
686576d4eb4cef831b89a3020caf27836fa4410c. Step 09 remains paused; G5 is open.

## Before-code requirements and evidence

- **User policy:** use verified current data for live decisions; keep chosen contract
  quantities and verified multipliers in risk arithmetic. Advertised quote sizes and
  provisional Greeks cannot supply position quantities, capacity or stop valuations.
- **Sourced:** [Tradier quotes](https://docs.tradier.com/docs/quotes) supplies distinct
  bid_date, ask_date and trade_date. None substitutes for another. [Market data](https://docs.tradier.com/docs/market-data)
  documents production consolidated real-time stocks/options, sandbox delay, slash
  equity share classes, and hourly Greeks. Account access still requires observation.
- **Checked:** the iMac comparison's latest SPY/QQQ/NVDA and selected SPY option sides
  had independently recomputed ages under the existing policy. This is bounded
  evidence, not whole-watchlist, opening-volume or operational acceptance.
- **Engineering choice:** a persistent REST store, independent of command lifetime.
  Healthy refreshes preserve its health generation. Failures, global stops, store
  replacement and reviewed crosswalk changes cannot carry old approvals forward.
  This is not a claim about a universal retail-trader caching convention.
- **Sourced mechanism:** [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
  permit a write reservation across final local checks and commit. Use EXCLUSIVE
  before sampling the final clock so rollback-journal readers cannot delay commit.

## Producers, consumers and pipeline

1. Existing GET-only Tradier transport: one batched stock/option quote request before
   each ticket recheck, never inside the final ticket transaction. Existing TLS,
   no-redirect, bounded-error and request-budget behavior remains.
2. New durable quote store: append-only request-start and component success/failure
   events, retained captures, production/sandbox partition, persistent global STOP.
   An unresolved newer attempt or a failure completed after a success started refuses.
   Explicit resume permits a new fetch; it does not make old captures eligible.
3. Reviewed Webull/Tradier crosswalk in that same store. Bind current Webull host,
   ID, class and USD basis to the exact Tradier symbol/type/description. Tradier's
   stock quote has no immutable shared issuer ID: the record is explicitly human
   reviewed, never inferred solely from matching tickers. Last-trade exchange is
   not a listing identifier and must not be used as one.
4. Signal terms: verify mapping and fresh underlying trade before scanner
   revalidation can change a signal. Keep price history and volume producers.
5. Ticket evidence: separately require current stock BBO and every requested option
   BBO, compare full OCC terms and reported contract_size with the trusted ContractBook.
   Compute each leg's spread using Decimal, use the worst spread for the existing
   limit and the oldest side timestamp for age. Keep account/status/OI/market/contract
   adapters mandatory; never fabricate missing inputs.
6. Final lock order: ticket -> signal -> existing volume/vendor guards -> Tradier
   quote/mapping store -> account. Re-read health, identities, current trade and all
   BBOs and risk at the final clock; hold reservations through ticket commit.
   No network, credentials or signal write under these reservations.
7. An explicit composer wraps a trusted existing RiskInputs factory. It is not a
   complete broker/account factory or runner activation. Legacy tastytrade generation
   semantics and other sources remain unchanged.

## Acceptance examples

Positive: real synthetic scanner event -> fresh Tradier -> prepare -> approve ->
single-use consumption; healthy refresh and a store reopen between commands preserve
approval. Verified option quantity/multiplier/premium math stays unchanged.
Negative: missing/revoked/wrong mapping, wrong symbol/type/option terms/multiplier,
zero/missing/negative/future/stale timestamps, crossed/locked/invalid prices, partial
reply, request failure/STOP, late stale success, late failure, unresolved newer check,
restart after failure, and source-label spoof all refuse only the relevant dependency.
A newer check begun after failure recovers health but requires new ticket approval.
Final races: failure/mapping change/option spread or age change before the fence
refuses; a writer arriving during the fence waits until ticket commit.

## Open requirements, Plan B and rollback

No production mappings will be invented. Current iMac commit, actual factory wiring,
read-only live consumer acceptance, opening-volume evidence and remaining G5 matrix
checks stay open. Tradier sizes/Greeks remain unverified for arithmetic; no clock
allowance, signal threshold, budget rule or order path is added. If a quote dependency
is unavailable the ticket remains visible and blocked. Revert this integration commit
to restore previous code; retain the new SQLite file for audit, do not silently select
a substitute quote source. Run focused integration cases and full strict suite before
push; record actual results below.


## Implemented files and boundary checks

- `tradier_client.py`: existing GET-only transport factored without policy changes;
  diagnostic module reexports its historical interface. No account/order route.
- `tradier_fields.py`: quote-only decimal prices, exact equity/OCC identity,
  epoch-millisecond side/trade times, informational sizes and guarded capture output.
  It imports no provisional option conventions or volume/Greek measurements.
- `quote_secrets.py`: unchanged credential-text guard shared with the existing
  measurement/report module, which reexports the historical names.
- `tradier_quotes.py`: SQLite health ledger/captures/reviewed mapping versions;
  interactive local review/resume, explicit revoke and one-GET scratch capture.
- `tradier_risk.py`: mapped trade before state-mutating revalidation; current BBOs
  and trusted option metadata; final store fence; explicit composer/factory contract.
- `risk_terms.py`, `tickets.py`: reserved provider label, provider-specific generation
  wording, optional outside-lock refresh and local final BBO callback, bound leg
  provenance and plain quantity disclosure. Legacy paths retain their behavior.
- `test_tradier_quotes_runtime.py`: real synthetic scanner/account/ticket fixture,
  request ordering, STOP/reopen/recovery, malformed fields, full option metadata,
  quantity/loss math, preliminary-to-final races, actual decimal JSON decoding,
  invalid parameter rejection, CLI human-review gates, credential guard and
  inter-process/final-commit writer exclusion. No live broker call.

The first combined strict run found two architectural guard failures (2 failed,
2043 passed). They correctly flagged dependencies on the diagnostic/measurement
modules. The guards were **not changed or relaxed**: transport, quote-only fields
and the credential-text helper were separated. Focused quote/diagnostic/conventions/
measurement suites then passed 196 tests. No provisional Greek or volume math is
used to qualify a ticket. Final strict run and mutation results are recorded below.


Final self-review added start attribution: every completed result must reference a
real START for that environment/symbol, and a START completes once. An absent,
boolean, forged or another ticker's check ID cannot clear a failure or inject a
new capture. The 2047-test combined run before this addition passed; the final
expanded run below covers the committed version. This does not change identity
ordering policy or expose broker data.


## Final verification (local, not iMac/live acceptance)

- Python **3.14.6**, `PYTHONPATH=src ../verify-python314/bin/python -m pytest -q -W error`:
  **2051 passed in 210.09 s** on the final runtime code and expanded tests.
- Targeted runtime suite: **76 passed in 21.53 s**.
- Factored transport plus old quote/conventions/measurement boundaries: **196 passed**
  on the preceding version (the final full run includes all of them again).
- Seven deliberate regressions: attributed request ID, late failure ordering,
  persistent failure generation, current time recheck, reviewed mapping, final
  option BBO reread, and denial/STOP before another request: **7/7 caught**.
  Reproduce using `python tools/check_tradier_quote_guards.py --output NEW_FILE`.
- CLI help smoke checks pass; `git diff --check` clean. All development/provider
  test traffic was synthetic: **zero actual provider requests, zero orders**.
- Existing architecture tests were retained unchanged; no clock tolerance, loss/
  budget rule, quote-size multiplier or Greek convention was added.

Implementation is ready for independent audit and the new iMac strict suite. It
is **not** real consumer acceptance: actual reviewed crosswalks, operational
RiskInputs factory and host/provider checks remain pending. G5 and parent CP3 stay
open; Step 09 stays paused. Rollback retains the audit store and disables the
opt-in composer; no automatic source fallback or order path exists.
