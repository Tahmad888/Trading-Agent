# Finance connector review — 2026-10-01

Read-only inventory/research. No connectors installed, permissions changed,
subscriptions purchased, messages sent to Claude, or credentials accessed.

## Observed connections and fit

Claude desktop Customize → Connectors → Yours showed **Connected** for viaNexus
vAST, Alpha Vantage MCP Server, Webull and Coinversa Pulse. IBKR, MT Newswires,
Clear Street and Gemini displayed **Connect**, not Connected. tastytrade did not
appear in this account/window's inventory; that does not negate earlier results
from another Claude surface. We did not verify entitlements by executing Claude's
connector tools; those tools are not callable from this Codex session.

Here, the exposed trading tools were Binance (crypto). Installed-tool and plugin
searches did not surface a callable US-stock earnings/SEC source. This is a session
capability finding, not a claim that the entire marketplace has no finance tools.

| Source | Useful gap | Evidence/limitation | Decision |
| --- | --- | --- | --- |
| SEC direct + issuer releases | Actuals, filing provenance, comparative quarters and explicit Q4 | Three-company iMac source access succeeded; no API key required. SEC facts can arrive after the release and omit custom/quarterly contexts. | Keep as working foundation and fallback; normalize correctly. |
| viaNexus vAST, connected | Normalized reported financials, news/transcripts, potential calendar cross-check | Connector listing describes entitlement-aware Core/Edge datasets and read-only search/fetch. Public catalog advertises a 14-day free trial, then Starter $10/month / Pro $15/month at review time; individual entitlements were not inspected. | Best next existing connection to test for the actual gaps. Do not assume a permanent free SEC-only tier or iMac entitlement. |
| Alpha Vantage, connected | Quarterly EPS, income statements, earnings calendar | Official API documents distinct EARNINGS, INCOME_STATEMENT and EARNINGS_CALENDAR functions. This project has observed rate limits; GAAP/basis and source timing need independent validation. | Secondary cross-check, not a critical dependency until quota/semantics verified. |
| Webull, connected | Upcoming ranges, recent reported actuals, price/metadata | Current three-company report: alert/calendar populated, income empty. | Use proven fields; do not reinterpret estimates as reported history. |
| Coinversa Pulse, connected; Binance here | Crypto/Hyperliquid market data | Coinversa's own repository describes Hyperliquid analytics, not US issuer financial statements. | Does not fill this earnings gap. |
| MT Newswires, not connected here | Timestamped original news for catalyst review | Potential partner data through viaNexus, subject to entitlement. | Check existing viaNexus access first; not an implemented catalyst source. |
| FMP, Twelve Data, FactSet, S&P (public Claude catalog) | Alternative fundamentals/earnings datasets | Provider offerings exist; no user access, price suitability or actual field acceptance established here. | No reason yet to add another service before testing the already connected source. |

Primary sources:
- [SEC APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [viaNexus vAST and entitlement model](https://vianexus.com/vast/)
- [viaNexus public catalog/pricing](https://console.blueskyapi.com/catalog)
- [Alpha Vantage API documentation](https://www.alphavantage.co/documentation/)
- [Alpha Vantage normalization discussion](https://documentation.alphavantage.co/FundamentalDataDocs/index.html)
- [Coinversa's repository](https://github.com/coinversaa/mcp-server)
- [Claude financial-services catalog](https://claude.com/marketplace/connectors-plugins?categories=financial-services)
- [Twelve Data connector](https://claude.com/connectors/twelvedata)

A marketplace listing does not prove quality, completeness, account access, or
permission to use the same feed in an unattended local program. No current service
is being replaced on marketing claims alone.

## Bounded optional Claude test prompt (prepared, not sent)

Use only read-only viaNexus tools. Inspect the datasets my current connection is
actually entitled to; do not add symbols, change a universe/rule, subscribe, purchase
or expose tokens. Identify whether the SEC/reported-financials dataset is permanently
free, a trial, or paid using explicit entitlement evidence; otherwise say UNKNOWN.
For NVDA, AAPL and MSFT, fetch latest reported quarterly diluted EPS/revenue and the
same prior-year quarter, with period start/end, fiscal labels, GAAP/adjusted basis,
units, provider/source links and publication/receipt/revision times where supplied.
Do not use estimates or annual/YTD EPS as quarterly actuals. Inspect MSFT Q4 explicitly.
Fetch the next earnings event with estimate/confirmed status and range/time precision.
If entitled, fetch one original linked catalyst article with a publication timestamp;
a generated summary alone is insufficient. Report dataset IDs, actual success/error,
missing fields and local Python/MCP access requirements supported by documentation.
Stop on denial or rate limit; no retries, purchases, orders or repo changes. Save a
redacted structured report and raw public observations. Connector access is not iMac
acceptance. Return the evidence, not a recommendation to mark Step 09 complete.
