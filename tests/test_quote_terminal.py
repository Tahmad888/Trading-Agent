"""Child 3, D1: diagnostics report each attempt's terminal health, not its last data.

Scripted dxLink server only (no network, no provider, no credentials). Runtime getters
already refused these cases; the defect was the report. None of this is a live PASS.
"""
from datetime import timedelta
import json

import pytest

from desk.quote_check import diagnostic
from desk.tastytrade_quotes import FIELDS, QuoteService
from desk.tastytrade_transport import PROFILE_CHANNEL, capture
from tests.test_quote_reaudit import Attempts, report
from tests.test_quote_repairs import PROFILE_MAP, Server, compact, profile_row, run
from tests.test_tastytrade_quotes import AT, client_fixture, instrument, quote_row, stock, trade_row

BOTH = [compact("Trade", trade_row()), compact("Quote", quote_row())]
INVALID_QUOTE_MAP = dict(type="FEED_CONFIG", channel=3, dataFormat="COMPACT",
                         eventFields={"Quote": ["eventType", "eventSymbol"]})
CHANGED_QUOTE_MAP = dict(type="FEED_CONFIG", channel=3, dataFormat="COMPACT",
                         eventFields={"Quote": list(reversed(FIELDS["Quote"]))})


def recovered_after(second, *, fail=(1,), symbols=("SPY",)):
    """First connection fails after one Trade; the second runs ``second`` then ends."""
    return report(Attempts([[compact("Trade", trade_row())], second], fail=set(fail)), symbols=symbols, seconds=8)


def row(out, symbol="SPY"):
    return next(r for r in out["stocks"]["checks"] if r["symbol"] == symbol)


# ---- a withdrawal after the last data message ------------------------------------------------------
@pytest.mark.parametrize("config,reason", [(INVALID_QUOTE_MAP, "FEED_SCHEMA_UNSUPPORTED"),
                                           (CHANGED_QUOTE_MAP, "FEED_SCHEMA_CHANGED")])
def test_quote_map_change_after_the_last_data_is_reported(config, reason):
    out = recovered_after(BOTH + [config])
    assert out["status"] == "OBSERVATIONS_ONLY" and out["capture"]["stop_reason"] == "CAPTURE_COMPLETE"
    assert row(out)["quote"] == dict(status="UNAVAILABLE", reason=reason)
    assert row(out)["trade"]["status"] == "AVAILABLE"  # the healthy Trade stays visible
    final = out["final_attempt"]
    assert final["components"]["SPY"] == {"Quote": True, "Trade": True, "Profile": False}  # receipt
    assert final["usable_at_end"]["SPY"] == {"Quote": False, "Trade": True, "Profile": False}
    assert final["health"] == "SOME_COMPONENTS_UNAVAILABLE_AT_END"
    recovery = out["capture"]["recoveries"][0]
    assert recovery["recovered"] is False and recovery["outcome"] == "RECEIVED_BUT_NOT_USABLE_AT_ATTEMPT_END"
    # The last data message is kept as history only.
    history = final["last_data_view"]
    assert history["eligible"] is False
    assert history["view"]["checks"][0]["quote"]["status"] == "AVAILABLE"


def test_profile_status_lost_after_the_last_data_is_reported():
    server = Server(on_subscribe=[compact("Trade", trade_row())],
                    profile=[PROFILE_MAP, profile_row("ACTIVE"),
                             dict(type="ERROR", channel=PROFILE_CHANNEL, error="UNKNOWN", message="x")])
    result, _ = run(server, profile=True)
    final = result["final_attempt"]
    status = final["terminal_view"]["checks"][0]["trading_status"]
    assert status == dict(status="UNKNOWN", reason="PROFILE_CHANNEL_ERROR_UNKNOWN")
    assert final["usable_at_end"]["SPY"] == {"Quote": False, "Trade": True, "Profile": False}
    assert result["observations"][-1]["checks"][0]["trading_status"]["status"] == "ACTIVE"  # earlier data


def test_a_halt_retained_after_the_last_data_is_reported():
    server = Server(on_subscribe=[compact("Trade", trade_row())],
                    profile=[PROFILE_MAP, profile_row("HALTED"),
                             dict(type="CHANNEL_CLOSED", channel=PROFILE_CHANNEL)])
    result, _ = run(server, profile=True)
    check = result["final_attempt"]["terminal_view"]["checks"][0]
    assert check["trading_status"]["status"] == "HALTED"
    assert check["trading_status"]["reason"] == "HALT_RETAINED_NO_RESUMPTION_EVIDENCE"
    assert check["trade"] == {**check["trade"], "status": "UNAVAILABLE", "reason": "SECURITY_HALTED"}
    assert result["final_attempt"]["usable_at_end"]["SPY"]["Trade"] is False


def test_an_identity_failure_after_the_last_data_is_reported():
    server = Server(on_subscribe=BOTH)
    seen = []

    def withdraw(view):
        seen.append(view)
        if len(seen) == 2:  # after the final data message
            source.invalidate("SPY", "IDENTITY_REFRESH_FAILED")
    from tests.test_tastytrade_quotes import service
    source = service(("SPY",))
    result, _ = run(server, source=source, on_observation=withdraw)
    check = result["final_attempt"]["terminal_view"]["checks"][0]
    assert check["trade"]["reason"] == check["quote"]["reason"] == "IDENTITY_REFRESH_FAILED"
    assert result["final_attempt"]["health"] == "NO_QUOTE_OR_TRADE_USABLE_AT_END"
    assert seen[-1]["checks"][0]["trade"]["status"] == "AVAILABLE"  # the data view predates it


# ---- receipt is not usability -----------------------------------------------------------------------
@pytest.mark.parametrize("second,component,reason", [
    ([compact("Trade", trade_row()), compact("Quote", quote_row(bidPrice="800", askPrice="799"))],
     "quote", "QUOTE_CROSSED"),
    ([compact("Trade", trade_row(at=AT - timedelta(seconds=61))), compact("Quote", quote_row())],
     "trade", "TRADE_STALE")])
def test_crossed_or_stale_values_are_received_but_not_usable(second, component, reason):
    out = recovered_after(second)
    assert row(out)[component]["status"] == "UNAVAILABLE" and row(out)[component]["reason"] == reason
    recovery = out["capture"]["recoveries"][0]
    assert recovery["received_by_symbol"]["SPY"]["Quote"] and recovery["received_by_symbol"]["SPY"]["Trade"]
    assert recovery["usable_by_symbol"]["SPY"][component.capitalize()] is False
    assert recovery["recovered"] is False and recovery["outcome"] == "RECEIVED_BUT_NOT_USABLE_AT_ATTEMPT_END"


def test_one_healthy_peer_is_not_recovery():
    out = recovered_after(BOTH, symbols=("SPY", "QQQ"))
    recovery = out["capture"]["recoveries"][0]
    assert recovery["usable_by_symbol"]["SPY"] == {"Quote": True, "Trade": True, "Profile": False}
    assert recovery["usable_by_symbol"]["QQQ"] == {"Quote": False, "Trade": False, "Profile": False}
    assert recovery["recovered"] is False and recovery["outcome"] == "PARTIALLY_RECEIVED"
    assert out["final_attempt"]["health"] == "SOME_COMPONENTS_UNAVAILABLE_AT_END"


# ---- transport failure after final observations ------------------------------------------------------
def test_final_observations_then_a_transport_fault_is_a_failure_with_history():
    out = report(Attempts([[compact("Trade", trade_row())], BOTH], fail={1, 2}), seconds=8)
    assert out["capture"]["stop_reason"] == "DXLINK_TRANSPORT_FAILURE"
    assert out["status"] == "FINAL_ATTEMPT_FAILED_AFTER_OBSERVATIONS"
    final = out["final_attempt"]
    assert final["deliberate_end"] is False and final["health"] == "NO_QUOTE_OR_TRADE_USABLE_AT_END"
    assert row(out)["trade"] == dict(status="UNAVAILABLE", reason="DXLINK_TRANSPORT_FAILURE", lag_status=
                                     "LAG_EVIDENCE_UNAVAILABLE")  # no cleared value is revived
    assert out["lag_evidence"] == "UNAVAILABLE"
    history = final["last_data_view"]
    assert history["eligible"] is False and history["lag_evidence_then"] == "AVAILABLE_WITHIN_QUOTE_POLICY"
    assert history["view"]["checks"][0]["trade"]["status"] == "AVAILABLE"  # kept, not erased
    assert out["capture"]["recoveries"][0]["recovered"] is False


def test_denial_after_final_observations_keeps_its_own_stop_reason():
    out = report(Attempts([[compact("Trade", trade_row())], BOTH + [dict(type="CHANNEL_CLOSED", channel=3)]],
                          fail={1}), seconds=8)
    assert out["capture"]["stop_reason"] == "DXLINK_DENIED_OR_CLOSED"
    assert out["status"] == "FINAL_ATTEMPT_FAILED_AFTER_OBSERVATIONS"
    assert row(out)["trade"]["reason"] == "DXLINK_DENIED_OR_CLOSED"


# ---- controls -----------------------------------------------------------------------------------------
def test_healthy_recovery_and_a_deliberate_end_keep_valid_values():
    out = recovered_after(BOTH)
    assert out["status"] == "OBSERVATIONS_ONLY" and out["lag_evidence"] == "AVAILABLE_WITHIN_QUOTE_POLICY"
    assert row(out)["trade"]["status"] == row(out)["quote"]["status"] == "AVAILABLE"
    assert out["capture"]["connected_after_capture"] is False  # closed afterwards, values kept
    recovery = out["capture"]["recoveries"][0]
    assert recovery["recovered"] is True and recovery["outcome"] == "USABLE_AT_ATTEMPT_END"
    assert out["final_attempt"]["health"] == "ALL_REQUESTED_QUOTE_AND_TRADE_USABLE_AT_END"
    assert out["final_attempt"]["deliberate_end"] is True
    assert len(out["capture"]["attempt_log"]) == out["capture"]["attempts"] == 2  # one terminal view each
    assert "PASS" not in json.dumps(out)


def test_quiet_time_at_the_deadline_uses_event_time_not_receipt_time():
    """A Trade 30 s old on receipt is still within policy; 40 quiet seconds later its
    event-time age is 70 s and the existing rule refuses it. Receipt time is not used."""
    server = Server(on_subscribe=[compact("Trade", trade_row(at=AT - timedelta(seconds=30)))])
    from tests.test_tastytrade_quotes import service
    source = service(("SPY",))
    ticks = [0.0]
    result = capture(client_fixture()[0], source, seconds=40, reconnects=0, connect=server.factory(ticks),
                     clock=lambda: AT + timedelta(seconds=ticks[0]), monotonic=lambda: ticks[0],
                     sleep=lambda s: ticks.__setitem__(0, ticks[0] + s))
    assert result["stop_reason"] == "CAPTURE_COMPLETE"
    assert result["observations"][-1]["checks"][0]["trade"]["status"] == "AVAILABLE"
    trade = result["final_attempt"]["terminal_view"]["checks"][0]["trade"]
    assert trade["status"] == "UNAVAILABLE" and trade["reason"] == "TRADE_STALE"
    assert 69 <= trade["age_seconds"] <= 71


def test_closed_market_terminal_views_never_claim_live_timing():
    client, _ = client_fixture()
    closed = AT - timedelta(days=1)  # Sunday
    client.resolve = lambda symbol, kind, svc: (svc.register(instrument(symbol, stock(symbol), closed)) or
                                                svc.identities[symbol])
    ticks = [0.0]
    server = Attempts([[compact("Trade", trade_row(at=closed)), compact("Quote", quote_row(at=closed))]])

    def capture_fn(cl, svc, **kwargs):
        return capture(cl, svc, connect=server.factory(ticks), clock=lambda: closed, monotonic=lambda: ticks[0],
                       sleep=lambda s: ticks.__setitem__(0, ticks[0] + s), **kwargs)
    out = diagnostic(client, QuoteService(), ["SPY"], seconds=6, reconnects=0, capture_fn=capture_fn,
                     clock=lambda: closed)
    assert out["LIVE_TIMING"] == "NOT_TESTED_MARKET_CLOSED"
    assert out["stocks"]["timing_status"] == "NOT_TESTED_MARKET_CLOSED"
    assert out["lag_evidence"] == "CLOSED_SESSION_AGES_ONLY_NOT_LIVE_EVIDENCE"
    assert "PASS" not in json.dumps(out)


def test_a_socket_that_fails_to_close_after_the_deadline_reports_the_fault_state():
    """A deliberate end whose close then fails is a transport fault: the view taken just
    before closing is discarded and the withdrawn state is reported instead."""
    server = Server(on_subscribe=BOTH)
    ticks = [0.0]
    make = server.factory(ticks)

    def connect(url):
        socket = make(url)

        class Failing(type(socket)):
            def __exit__(self, *a):
                raise OSError("close failed")
        socket.__class__ = Failing
        return socket
    from tests.test_tastytrade_quotes import service
    result = capture(client_fixture()[0], service(("SPY",)), seconds=6, reconnects=0, connect=connect,
                     clock=lambda: AT, monotonic=lambda: ticks[0], sleep=lambda s: ticks.__setitem__(0, ticks[0] + s))
    assert result["stop_reason"] == "DXLINK_TRANSPORT_FAILURE"
    final = result["final_attempt"]
    assert final["deliberate_end"] is False and final["health"] == "NO_QUOTE_OR_TRADE_USABLE_AT_END"
    assert final["terminal_view"]["checks"][0]["trade"]["reason"] == "DXLINK_TRANSPORT_FAILURE"
    assert result["observations"][-1]["checks"][0]["trade"]["status"] == "AVAILABLE"  # history kept
