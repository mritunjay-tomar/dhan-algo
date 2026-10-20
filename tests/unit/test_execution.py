from unittest.mock import Mock
import pytest
import strategy as execution


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
    monkeypatch.setattr(execution,"EXIT_ORDER_POLL_ATTEMPTS",1)
    client.get_order_by_id.return_value=payload
    assert not execution.wait_for_orders_traded(client,[{"response":{"data":{"orderId":"1"}},"quantity":25,"security_id":"101"}],phase="TEST")[0]


def test_pending_eventually_fills(client,monkeypatch):
    monkeypatch.setattr(execution.time,"sleep",lambda _:None)
    client.get_order_by_id.side_effect=[{"data":{"orderStatus":"PENDING","filledQty":0}},
                                       {"data":{"orderStatus":"TRADED","filledQty":25}}]
    assert execution.wait_for_orders_traded(client,[{"response":{"data":{"orderId":"1"}},"quantity":25,"security_id":"101"}],phase="TEST")[0]


def test_transient_order_responses_are_retried_until_fifth_attempt(client,monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(execution.time,"sleep",sleep)
    client.get_order_by_id.side_effect = [
        {"status":"failure","remarks":"temporary"},
        {"status":"success","data":{}},
        {"status":"success","data":[]},
        None,
        {"status":"success","data":{"orderStatus":"TRADED","filledQty":25}},
    ]
    order = {"response":{"data":{"orderId":"1"}},"quantity":25,"security_id":"101"}
    confirmed, statuses = execution.wait_for_orders_traded(client,[order],phase="TEST")
    assert confirmed
    assert statuses[0]["fully_filled"] is True
    assert client.get_order_by_id.call_count == 5
    assert sleep.call_count == 4
    assert all(call.args == (1.0,) for call in sleep.call_args_list)


def test_persistent_unusable_order_responses_get_five_attempts(client,monkeypatch,caplog):
    sleep = Mock()
    monkeypatch.setattr(execution.time,"sleep",sleep)
    client.get_order_by_id.return_value = {"status":"success","data":{}}
    order = {"response":{"data":{"orderId":"1"}},"quantity":25,"security_id":"101"}
    with caplog.at_level("INFO"):
        confirmed, statuses = execution.wait_for_orders_traded(client,[order],phase="TEST")
    assert not confirmed
    assert statuses[0]["order_status"] == ""
    assert client.get_order_by_id.call_count == 5
    assert sleep.call_count == 4
    assert "TEST_ORDER_CONFIRMATION_EXHAUSTED" in caplog.text


def test_terminal_order_status_fails_without_retries(client,monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(execution.time,"sleep",sleep)
    client.get_order_by_id.return_value = {"data":{"orderStatus":"REJECTED","filledQty":0}}
    order = {"response":{"data":{"orderId":"1"}},"quantity":25,"security_id":"101"}
    assert not execution.wait_for_orders_traded(client,[order],phase="TEST")[0]
    client.get_order_by_id.assert_called_once_with("1")
    sleep.assert_not_called()


def test_order_details_accepts_direct_wrapped_and_list_shapes():
    expected = {"orderId":"1","orderStatus":"TRADED","filledQty":25}
    assert execution.order_details(expected,"1") == expected
    assert execution.order_details({"status":"success","data":expected},"1") == expected
    assert execution.order_details({"data":{"data":[expected]}},"1") == expected
