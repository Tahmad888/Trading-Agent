# Advertised quote quantities: provider clarification still required

G5 -> G5a parent CP3. Prepared questions, **not sent**. No credentials/account details
belong in the request. Do not treat another endpoint's trade size as a quote-size unit.

Copyable inquiry for Tradier support:

> For current production market data, please confirm these fields separately:
> 1. REST GET /v1/markets/quotes, type=option: are `bidsize` and `asksize`
>    individual option contracts or lots of 100 contracts? If `asksize` is 10,
>    does that advertise 10 contracts or 1,000 contracts?
> 2. Production WebSocket quote events for an OCC option: same question for
>    `bidsz` and `asksz`. Are their units identical to REST option quote fields?
> 3. For equities, are these REST and streaming fields individual shares or
>    round lots? Does a reported `lot_size` change their interpretation?
> 4. Are option quote quantities aggregated at the best price across venues or
>    quantities from the reported bid/ask exchange? Please identify any API/version
>    or effective-date differences and a public schema we can retain as evidence.
> Separately, please confirm the timezone and meaning of `greeks.updated_at`
> (calculation time versus publication time), and gamma/theta/vega units including
> calendar-day convention. We will keep these provisional until confirmed.

Retain dated first-party answers with each exact endpoint/field. A quotation in a
forum is corroboration, not certification. Cross-check actual observations using
the same OCC contract, side, price, exchange where supplied, timestamp precision
and narrow overlapping receipt brackets. Preserve all candidate matches when
timestamps are truncated; equal numbers alone do not establish event identity.
Check reference entitlement/units before making any provider call, count all calls,
stop on provider/local-budget refusal, and keep any scope limitations explicit.

Claude's supplied extras found matching option **trade** sizes against Alpaca and
often similar quote-size scales against tastytrade. Neither certifies Tradier
REST/stream advertised quantities. Until resolved, these fields remain raw and
excluded only from capacity/liquidity arithmetic. Chosen position quantity,
trusted contract multiplier, premium, exposure and maximum-loss math are unchanged.

Once first-party evidence and compatible observations support a mapping, a separate
reviewed change can enable that exact field's unit conversion and capacity checks.
Do not enable all providers/transport fields from one answer. No message has been
sent, source entitlement assumed or size conversion enabled by this document.
