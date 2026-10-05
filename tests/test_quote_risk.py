"""Real local signal/account/ticket pipeline; all provider data is synthetic.

The signal is the G3 vendor-history breakout fixture; tastytrade identities, trades,
profiles and the reviewed mapping are labelled fixtures, never provider evidence.
"""
from dataclasses import replace
from datetime import timedelta
import io
import json
from threading import Event, Thread

import pytest

from desk import scanner as sc
from desk.quote_mapping import MappingStore, QuoteMapping, build, main as mapping_main
from desk.risk_terms import RESERVED_QUOTE_SOURCES, EventRiskSource
from desk.tastytrade_quotes import SOURCE, QuoteService, QuoteUnavailable, instrument
from desk.tickets import TicketError, TicketStore, render
from tests.quote_support import QuoteDesk, record
from tests.test_tastytrade_quotes import AGE, service, stock, trade_row
from tests.ticket_support import approve


def legacy_source(q):
    return EventRiskSource(q.desk.src, q.desk.log, lambda symbol: (symbol, float(q.limit), q.desk.now))


def store(tmp_path):
    return TicketStore(tmp_path / "tickets.sqlite")


def prepared(tmp_path, **request):
    q = QuoteDesk(tmp_path)
    tickets = store(tmp_path)
    tid, version = tickets.prepare(q.request(**request), q.inputs(), now=q.at)
    return q, tickets, tid, version


def test_reserved_label_is_the_adapter_source_name():
    assert SOURCE in RESERVED_QUOTE_SOURCES


def test_quote_to_signal_to_ticket_single_use(tmp_path):
    q, tickets, tid, version = prepared(tmp_path)
    view = tickets.get(tid, version, now=q.at)
    assert view["state"] == "pending", view["binding"]["failed_checks"]
    proof = view["binding"]["quote_provenance"]
    assert proof["instrument_id"] == "synthetic:LEAD" and proof["mapping_digest"] == record().mapping_digest
    assert "reviewed mapping" in render(view)
    approve(tickets, tid, version, q.inputs(), now=q.at)
    result = tickets.consume(tid, version, request_id="fixture-request", inputs=q.inputs(), now=q.at)
    assert result["order_submitted"] is False and result["broker_action"] == "none"
    assert tickets.consume(tid, version, request_id="fixture-request", inputs=q.inputs(), now=q.at)["replay"] is True
    with pytest.raises(TicketError, match="already consumed"):
        tickets.consume(tid, version, request_id="different", inputs=q.inputs(), now=q.at)


def test_caller_source_claim_does_not_become_provenance(tmp_path):
    q, tickets, tid, version = prepared(tmp_path, quote_source="fake source")
    assert "quote_source_matches" in tickets.get(tid, version, now=q.at)["binding"]["failed_checks"]
    with pytest.raises(TicketError, match="blocking checks"):
        approve(tickets, tid, version, q.inputs(), now=q.at)


def test_reserved_label_without_independent_provenance_is_never_approved(tmp_path):
    """F6: a legacy terms source cannot gain tastytrade status from the caller's text."""
    q = QuoteDesk(tmp_path)
    tickets = store(tmp_path)
    legacy = q.inputs(terms=legacy_source(q))  # honest legacy fixture quote source, no provenance
    tid, version = tickets.prepare(q.request(quote_source=SOURCE), legacy, now=q.at)
    view = tickets.get(tid, version, now=q.at)
    assert view["state"] == "blocked" and "quote_source_matches" in view["binding"]["failed_checks"]
    assert "quote_provenance" not in view["binding"]
    assert "NOT backed by independent evidence" in render(view)
    with pytest.raises(TicketError):
        approve(tickets, tid, version, legacy, now=q.at)
    with pytest.raises(TicketError):
        tickets.consume(tid, version, request_id="r", inputs=legacy, now=q.at)


def test_honest_legacy_label_with_legacy_source_still_works(tmp_path):
    q = QuoteDesk(tmp_path)
    tickets = store(tmp_path)
    adapters = q.inputs(terms=legacy_source(q))
    tid, version = tickets.prepare(q.request(quote_source="synthetic quote fixture"), adapters, now=q.at)
    view = tickets.get(tid, version, now=q.at)
    assert view["state"] == "pending", view["binding"]["failed_checks"]
    approve(tickets, tid, version, adapters, now=q.at)


def test_new_quote_timestamp_same_generation_does_not_churn_ticket(tmp_path):
    q, tickets, tid, version = prepared(tmp_path)
    later = q.at + timedelta(seconds=1)
    q.feed(at=later)
    approve(tickets, tid, version, q.inputs(), now=later)
    assert tickets.get(tid, version, now=later)["state"] == "approved"


@pytest.mark.parametrize("fault", ["disconnect", "identity", "reconnect", "expiry", "price", "chase",
                                   "mapping_removed", "mapping_changed", "halted", "streamer"])
def test_changed_evidence_after_recheck_refuses_final_write(tmp_path, fault):
    q, tickets, tid, version = prepared(tmp_path)
    adapters = q.inputs()
    observe = adapters.observe

    def race(*args):
        observation = observe(*args)
        if fault == "disconnect":
            q.quotes.disconnect()
        elif fault == "identity":
            q.quotes.register(q.changed_identity(cusip="changed-id"))
            q.feed()
        elif fault == "reconnect":
            q.quotes.begin(q.at + AGE)
            q.quotes.ready(q.at)
            q.feed()
        elif fault == "expiry":
            q.quotes.begin(q.at)  # token already expired at the final clock
        elif fault == "price":
            q.feed(price=146.0)  # through the approved stop (146.5)
        elif fault == "chase":
            q.feed(price=float(q.limit) * 1.2)
        elif fault == "mapping_removed":
            q.store.path.write_text(json.dumps({"schema": "desk-quote-mappings-v2", "mappings": []}))
        elif fault == "mapping_changed":
            q.store.path.unlink()
            q.store.add(record(webull_id="id:SOMEONE-ELSE"))
        elif fault == "halted":
            q.quotes.feed("Profile", dict(eventType="Profile", eventSymbol="LEAD", tradingStatus="HALTED"), q.at)
        else:
            q.quotes.register(q.changed_identity(**{"streamer-symbol": "LEAD:X"}))
        return observation

    with pytest.raises(TicketError):
        approve(tickets, tid, version, replace(adapters, observe=race), now=q.at)
    assert tickets.get(tid, version, now=q.at)["state"] == "pending"


def test_disconnect_after_approval_refuses_consumption(tmp_path):
    q, tickets, tid, version = prepared(tmp_path)
    approve(tickets, tid, version, q.inputs(), now=q.at)
    adapters = q.inputs()
    observe = adapters.observe

    def race(*args):
        result = observe(*args)
        q.quotes.disconnect()
        return result
    with pytest.raises(TicketError):
        tickets.consume(tid, version, request_id="fixture", inputs=replace(adapters, observe=race), now=q.at)
    assert tickets.get(tid, version, now=q.at)["state"] == "approved"


def test_normal_live_price_movement_is_rechecked_without_new_version(tmp_path):
    q, tickets, tid, version = prepared(tmp_path)
    adapters = q.inputs()
    observe = adapters.observe

    def update(*args):
        result = observe(*args)
        q.feed(price=float(q.limit) + 0.1)
        return result
    approve(tickets, tid, version, replace(adapters, observe=update), now=q.at)
    assert tickets.get(tid, version, now=q.at)["state"] == "approved"


def test_local_health_writer_waits_for_final_ticket_commit(tmp_path, monkeypatch):
    q, tickets, tid, version = prepared(tmp_path)
    entering, completed = Event(), Event()
    threads = []
    original = tickets._audit

    # If the exact audit hook changes, this test must follow the real final write.
    def write(*args, **kwargs):
        def disconnect():
            entering.set()
            q.quotes.disconnect()
            completed.set()
        thread = Thread(target=disconnect)
        threads.append(thread)
        thread.start()
        assert entering.wait(2)
        assert not completed.wait(.05)
        return original(*args, **kwargs)
    monkeypatch.setattr(tickets, "_audit", write)
    approve(tickets, tid, version, q.inputs(), now=q.at)
    for thread in threads:
        thread.join(2)
    assert completed.is_set() and not q.quotes.connected


# ---- F7: identity is verified before any signal-state change --------------------------------
@pytest.mark.parametrize("case,code", [
    ("missing", "QUOTE_MAPPING_MISSING"),
    ("webull", "QUOTE_MAPPING_WEBULL_MISMATCH"),
    ("cusip", "QUOTE_MAPPING_TASTYTRADE_MISMATCH"),
    ("streamer", "QUOTE_MAPPING_TASTYTRADE_MISMATCH"),
    ("environment", "QUOTE_MAPPING_TASTYTRADE_MISMATCH"),
    ("malformed", "QUOTE_MAPPING_INVALID"),
    ("ambiguous", "QUOTE_MAPPING_AMBIGUOUS"),
])
def test_unverified_identity_never_reaches_signal_revalidation(tmp_path, monkeypatch, case, code):
    q = QuoteDesk(tmp_path, mapped=case not in {"missing", "malformed", "ambiguous"})
    if case == "webull":
        q.store.path.unlink()
        q.store.add(record(webull_id="id:ANOTHER-ISSUER"))
    elif case == "cusip":
        q.quotes.register(q.changed_identity(cusip="SOME-OTHER-ISSUER"))
    elif case == "streamer":
        q.quotes.register(q.changed_identity(**{"streamer-symbol": "LEAD.X"}))
    elif case == "environment":
        q.quotes = QuoteService(environment="sandbox")
        q.quotes.register(instrument("LEAD", stock("LEAD"), q.at))
        q.quotes.begin(q.at + timedelta(hours=1))
        q.quotes.ready(q.at)
    elif case == "malformed":
        good = record().model_dump(mode="json")
        q.store.path.write_text(json.dumps({"schema": "desk-quote-mappings-v2",
                                            "mappings": [{**good, "mapping_digest": "tampered"}]}))
    elif case == "ambiguous":
        good = record().model_dump(mode="json")
        q.store.path.write_text(json.dumps({"schema": "desk-quote-mappings-v2", "mappings": [good, good]}))
    q.feed(price="3.10")  # a different issuer's price, far below the 146.5 stop
    before = q.event()
    called = []
    monkeypatch.setattr(sc, "revalidate_signal", lambda *a, **k: called.append(1))
    with pytest.raises(QuoteUnavailable, match=code):
        q.source().resolve(q.event_id, q.at)
    assert called == []
    after = q.event()
    for key in ("state", "eligible", "entry_level", "expires_at", "terms_digest", "valid_until"):
        assert after[key] == before[key], key
    assert after["signal"]["stop"] == before["signal"]["stop"]


def test_unverified_identity_blocks_the_ticket_with_its_code(tmp_path):
    q = QuoteDesk(tmp_path, mapped=False)
    tickets = store(tmp_path)
    tid, version = tickets.prepare(q.request(), q.inputs(), now=q.at)
    view = tickets.get(tid, version, now=q.at)
    assert view["state"] == "blocked"
    audit = [e for e in tickets.history(tid) if e["event"] == "prepared"][-1]
    assert any("QUOTE_MAPPING_MISSING" in p for p in audit["payload"]["problems"])
    assert q.event()["state"] == "triggered"


def test_verified_identity_still_reaches_the_approved_setup_rules(tmp_path):
    """Identity checks never stand in for price rules: a verified gap below the stop
    still invalidates the signal through the existing rule."""
    q = QuoteDesk(tmp_path)
    q.feed(price=140.0)
    with pytest.raises(ValueError):
        q.source().resolve(q.event_id, q.at)
    assert q.event()["state"] == "invalidated"


def test_legacy_price_basis_without_webull_host_is_unverified(tmp_path):
    mappings = MappingStore(tmp_path / "m.json")
    mappings.add(record())
    identity = instrument("LEAD", stock("LEAD"), q_time())
    basis = {"symbol": "LEAD", "security_id": "id:LEAD", "currency": "USD", "source": "reviewed ledger"}
    with pytest.raises(QuoteUnavailable, match="QUOTE_MAPPING_WEBULL_HOST_UNKNOWN"):
        mappings.verify("LEAD", price_basis=basis, identity=identity, environment="production",
                        webull_identity={"instrument_id": "id:LEAD", "currency": "USD", "sub_category": "COMMON_STOCK"})


def test_one_bad_record_leaves_peer_mappings_usable(tmp_path):
    mappings = MappingStore(tmp_path / "m.json")
    good = record().model_dump(mode="json")
    other = {**record("PEER").model_dump(mode="json"), "mapping_digest": "tampered"}
    mappings.path.write_text(json.dumps({"schema": "desk-quote-mappings-v2", "mappings": [good, other]}))
    assert [r.desk_symbol for r in mappings.records()] == ["LEAD"]


def test_share_class_alias_and_automatic_consistency_checks():
    alias = record("BRK.B")
    assert alias.tastytrade.provider_symbol == "BRK/B"
    with pytest.raises(ValueError):
        QuoteMapping.model_validate({**alias.model_dump(mode="json"), "desk_symbol": "BRK.A"})
    with pytest.raises(ValueError, match="ETF"):
        bad = record().model_dump(mode="json")
        bad["tastytrade"]["is_etf"] = True
        QuoteMapping.model_validate(bad)


# ---- F8: approvals never carry across quote sessions ---------------------------------------
def test_independent_quote_session_gets_an_actionable_refusal(tmp_path):
    q, tickets, tid, version = prepared(tmp_path)
    second = service(("LEAD",), at=q.at)  # another process: same identity, same price
    second.feed("Trade", trade_row("LEAD", q.at, price=str(q.limit)), q.at)
    with pytest.raises(TicketError, match="quote session changed"):
        approve(tickets, tid, version, q.inputs(terms=q.source(quotes=second)), now=q.at)


# ---- Reviewed mapping command: never automatic ------------------------------------------------
class Terminal(io.StringIO):
    def isatty(self):
        return True


def captures(tmp_path, *, name="Synthetic LEAD Inc", etf=False):
    webull = {"host": "api.sandbox.webull.com", "checks": [{
        "symbol": "LEAD", "instrument_id": "id:LEAD", "name": name, "security_type": "ETF" if etf else "COMMON_STOCK",
        "exchange_code": "NSQ", "currency": "USD"}]}
    tasty = {"environment": "production", "identity_capture": [
        instrument("LEAD", stock("LEAD", description="Synthetic LEAD Inc common stock", **{"is-etf": False,
                                 "listed-market": "XNAS"}), q_time()).capture()]}
    paths = tmp_path / "webull.json", tmp_path / "tasty.json"
    paths[0].write_text(json.dumps(webull))
    paths[1].write_text(json.dumps(tasty))
    return paths


def q_time():
    from datetime import datetime, timezone
    return datetime(2026, 9, 29, 14, tzinfo=timezone.utc)


def test_mapping_review_requires_a_person_and_consistent_captures(tmp_path):
    webull, tasty = captures(tmp_path)
    path = tmp_path / "store.json"
    args = ["--store", str(path), "review", "--symbol", "LEAD", "--webull-capture", str(webull),
            "--tastytrade-capture", str(tasty), "--reviewer", "fixture:automated-test"]
    with pytest.raises(SystemExit):
        mapping_main(args, stdin=io.StringIO("MAP LEAD\n"), stdout=io.StringIO())  # not a terminal
    assert not path.exists()
    assert mapping_main(args, stdin=Terminal("yes\n"), stdout=io.StringIO()) == 1
    assert not path.exists()
    assert mapping_main(args, stdin=Terminal("MAP LEAD\n"), stdout=io.StringIO()) == 0
    stored = MappingStore(path).records()
    assert [r.desk_symbol for r in stored] == ["LEAD"] and stored[0].tastytrade.cusip == "synthetic:LEAD"
    assert path.stat().st_mode & 0o777 == 0o600
    webull_etf, _ = captures(tmp_path, etf=True)
    with pytest.raises(ValueError, match="ETF"):
        build("LEAD", json.loads(webull_etf.read_text()), json.loads(tasty.read_text()), "x", q_time())


def test_live_ticket_refuses_sandbox_quote_provenance(tmp_path):
    """Environment is checked from independent provenance, never from request text."""
    q = QuoteDesk(tmp_path, mapped=False)
    q.store.add(record(environment="sandbox"))
    q.quotes = QuoteService(environment="sandbox")
    q.quotes.register(instrument("LEAD", stock("LEAD"), q.at))
    q.quotes.begin(q.at + timedelta(hours=1))
    q.quotes.ready(q.at)
    q.feed()
    tickets = store(tmp_path)
    paper = tickets.prepare(q.request(), q.inputs(), now=q.at)
    assert tickets.get(*paper, now=q.at)["state"] == "pending"
    live = tickets.prepare(q.request(environment="live"), q.inputs(), now=q.at)
    assert "quote_source_matches" in tickets.get(*live, now=q.at)["binding"]["failed_checks"]


def test_provenance_without_a_reviewed_mapping_is_refused_by_risk(tmp_path):
    """Defense in depth: any terms source claiming tastytrade evidence must carry a mapping."""
    q = QuoteDesk(tmp_path)

    class Unmapped(type(q.source())):
        def resolve(self, event_id, now):
            terms = super().resolve(event_id, now)
            return terms.model_copy(update={"quote_provenance": terms.quote_provenance.model_copy(
                update={"mapping_digest": None})})
    tickets = store(tmp_path)
    tid, version = tickets.prepare(q.request(), q.inputs(terms=Unmapped(q.desk.src, q.desk.log, q.quotes, q.store)),
                                   now=q.at)
    assert "quote_source_matches" in tickets.get(tid, version, now=q.at)["binding"]["failed_checks"]
