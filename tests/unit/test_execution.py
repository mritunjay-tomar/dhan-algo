from dataclasses import replace
import pytest
from dhan_algo import execution
from dhan_algo.broker import DhanBroker


@pytest.mark.parametrize("dry,enabled", [(True,False),(True,True),(False,False)])
def test_broker_never_submits_read_only(config,client,dry,enabled):
    with pytest.raises(RuntimeError):
        DhanBroker(client,replace(config,dry_run=dry,live_trading_enabled=enabled)).place_order(security_id="101")
    client.place_order.assert_not_called()


def test_entry_buys_protection_before_short(client):
    execution.place_credit_spread_entry(client,{"security_id":"101"},{"security_id":"102"},25)
    assert [c.kwargs["transaction_type"] for c in client.place_order.call_args_list]==["BUY","SELL"]
    client.get_order_by_id.assert_not_called()


def test_failed_buy_submission_blocks_short(client):
    client.place_order.return_value={"status":"failure"}
    with pytest.raises(ValueError):
        execution.place_credit_spread_entry(client,{"security_id":"101"},{"security_id":"102"},25)
    assert client.place_order.call_count==1


def test_entry_submits_short_when_buy_fill_status_is_unavailable(client):
    client.get_order_by_id.side_effect=AssertionError("Entry must not poll for a fill")
    execution.place_credit_spread_entry(client,{"security_id":"101"},{"security_id":"102"},25)
    assert [call.kwargs["transaction_type"] for call in client.place_order.call_args_list] == ["BUY", "SELL"]
    client.get_order_by_id.assert_not_called()


def test_exit_closes_short_before_hedge_without_waiting(client,positions):
    execution.close_open_fno_positions(client,list(reversed(positions)))
    assert [c.kwargs["security_id"] for c in client.place_order.call_args_list]==["101","102"]
    assert [c.kwargs["transaction_type"] for c in client.place_order.call_args_list]==["BUY","SELL"]
    assert all(c.kwargs["order_type"]=="MARKET" and c.kwargs["price"]==0 for c in client.place_order.call_args_list)
    client.get_order_by_id.assert_not_called()


def test_rejected_short_exit_submission_keeps_hedge(client,positions):
    client.place_order.return_value={"status":"failure"}
    with pytest.raises(ValueError): execution.close_open_fno_positions(client,positions)
    assert client.place_order.call_count==1
    assert client.place_order.call_args.kwargs["transaction_type"]=="BUY"


@pytest.mark.parametrize("response", [None,{}, {"status":"failure"},{"data":{}},{"data":{"orderId":""}}])
def test_submission_error_blocks_confirmation(client,response):
    assert not execution.wait_for_orders_traded(client,[{"response":response}],phase="TEST")[0]
    client.get_order_by_id.assert_not_called()


def test_empty_orders_not_confirmed(client):
    assert execution.wait_for_orders_traded(client,[],phase="TEST")== (False,[])


@pytest.mark.parametrize("payload", [{"status":"failure"},None,{"data":{"orderStatus":"TRADED"}},
    {"data":{"orderStatus":"TRADED","filledQty":"bad"}}])
def test_bad_order_status_blocks_confirmation(client,monkeypatch,payload):
    monkeypatch.setattr(execution,"EXIT_ORDER_POLL_TIMEOUT_SECONDS",0)
    client.get_order_by_id.return_value=payload
    assert not execution.wait_for_orders_traded(client,[{"response":{"data":{"orderId":"1"}},"quantity":25,"security_id":"101"}],phase="TEST")[0]


def test_pending_eventually_fills(client,monkeypatch):
    monkeypatch.setattr(execution.time,"sleep",lambda _:None)
    client.get_order_by_id.side_effect=[{"data":{"orderStatus":"PENDING","filledQty":0}},
                                       {"data":{"orderStatus":"TRADED","filledQty":25}}]
    assert execution.wait_for_orders_traded(client,[{"response":{"data":{"orderId":"1"}},"quantity":25,"security_id":"101"}],phase="TEST")[0]
