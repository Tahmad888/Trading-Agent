"""Synthetic quote-backed ticket path (child 3). No provider call, no real mapping.

The signal comes from the G3 vendor-history fixture (Webull host recorded), the
tastytrade identity and trades are synthetic rows, and the reviewed mapping is a
labelled fixture record: none of it is provider or operational mapping acceptance.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal

from desk import scanner as sc
from desk.quote_mapping import MappingStore, QuoteMapping, TastytradeSide, WebullSide, _digest
from desk.quote_risk import TastytradeRiskSource
from desk.signal_state import signal_payload
from desk.tastytrade_quotes import SOURCE, instrument
from desk.tickets import TicketLeg
from tests.risk_support import MARKET
from tests.test_g5_integration import BREAKOUT, Desk
from tests.test_tastytrade_quotes import service, stock, trade_row
from tests.test_vendor_basis import HOST, NOW
from tests.ticket_support import inputs, observer, share_request


def record(symbol="LEAD", *, host=HOST, webull_id=None, cusip=None, streamer=None, environment="production",
           etf=False, reviewer="fixture:automated-test") -> QuoteMapping:
    webull = WebullSide(host=host, symbol=symbol, instrument_id=webull_id or f"id:{symbol}",
                        name=f"Synthetic {symbol} Inc", exchange_code="NSQ",
                        sub_category="ETF" if etf else "COMMON_STOCK", currency="USD")
    tasty = TastytradeSide(environment=environment, provider_symbol=symbol.replace(".", "/"),
                           streamer_symbol=streamer or symbol.replace(".", "/"),
                           instrument_id=cusip or f"synthetic:{symbol}", cusip=cusip or f"synthetic:{symbol}",
                           description=f"Synthetic {symbol} Inc common stock", listed_market="XNAS", is_etf=etf)
    return QuoteMapping(desk_symbol=symbol, webull=webull, tastytrade=tasty, reviewed_by=reviewer,
                        reviewed_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
                        webull_capture_sha256="fixture", tastytrade_capture_sha256="fixture",
                        mapping_digest=_digest(QuoteMapping.semantic(symbol, webull, tasty)))


class QuoteDesk:
    """A triggered vendor-basis breakout, a quote service and a reviewed-mapping store."""

    def __init__(self, tmp_path, *, mapped=True):
        self.desk = Desk(tmp_path)
        rec, armed = sc.close_scan(self.desk.src, ["LEAD"], NOW, preparing=True)
        candidate = next(s for s in armed if s.setup_id == BREAKOUT)
        rec.armed = [signal_payload(candidate)]
        self.desk.log.save_armed(NOW.date(), rec)
        first = sc.run(self.desk.src, ["LEAD"], self.desk.log, NOW)
        self.event_id = first.triggered[0]["event_id"]
        self.at = self.desk.now
        event = self.desk.log.signals.get(self.event_id, self.at)
        self.limit = Decimal(str(event["entry_level"])).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        self.quotes = service(("LEAD",), at=self.at)
        self.store = MappingStore(tmp_path / "quote-mappings.json")
        if mapped:
            self.store.add(record())
        self.feed()

    def feed(self, price=None, at=None):
        at = at or self.at
        return self.quotes.feed("Trade", trade_row("LEAD", at, price=str(price or self.limit)), at)

    def source(self, quotes=None, store=None):
        return TastytradeRiskSource(self.desk.src, self.desk.log, quotes or self.quotes, store or self.store)

    def inputs(self, terms=None, **changes):
        self.desk.snapshot()
        adapters = inputs(self.desk.state, terms=terms or self.source(),
                          market=MARKET.model_copy(update={"as_of": self.desk.now}),
                          observe=observer(quote_as_of=self.desk.now - timedelta(seconds=1)))
        return replace(adapters, **changes) if changes else adapters

    def request(self, quote_source=SOURCE, **changes):
        return share_request(event_id=self.event_id, legs=(TicketLeg(symbol="LEAD", limit_price=self.limit),),
                             budget_usd=Decimal("100"), time_stop=self.at + timedelta(days=15),
                             quote_source=quote_source, **changes)

    def event(self):
        return self.desk.log.signals.get(self.event_id, self.at)

    def changed_identity(self, **row):
        return instrument("LEAD", stock("LEAD", **row), self.at)
