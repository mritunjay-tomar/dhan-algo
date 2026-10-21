import pytest
import strategy
import utils


def traded(quantity=25):
    return {"data": {"orderStatus": "TRADED", "filledQty": quantity}}


def rejected(reason, filled=0):
    return {"status": "success", "data": {"orderStatus": "REJECTED", "filledQty": filled,
            "omsErrorCode": "broker-code", "omsErrorDescription": reason}}


def test_hedge_rejection_retries_only_hedge_after_short_closes(client, positions, monkeypatch, caplog):
    monkeypatch.setattr(utils.time, "sleep", lambda _: None)
    hedge_only = {"data": [positions[1]]}
    client.get_positions.side_effect = [hedge_only, hedge_only, hedge_only, {"data": []}]
    client.get_order_by_id.side_effect = [traded(), rejected("Part of a hedge and cannot be exited solely"), traded()]
    with caplog.at_level("INFO"):
        orders = utils.close_open_fno_positions(client, positions)
    assert len(orders) == 3
    assert [(c.kwargs["security_id"], c.kwargs["transaction_type"], c.kwargs["quantity"])
            for c in client.place_order.call_args_list] == [("101", "BUY", 25), ("102", "SELL", 25), ("102", "SELL", 25)]
    assert '"oms_error_code":"broker-code"' in caplog.text
    assert "Part of a hedge and cannot be exited solely" in caplog.text
    assert "EXIT_ORDER_RETRY" in caplog.text


def test_retry_after_terminal_partial_fill_uses_only_remaining_quantity(client, positions, monkeypatch):
    monkeypatch.setattr(utils.time, "sleep", lambda _: None)
    remaining_short = {**positions[0], "netQty": -15}
    client.get_positions.side_effect = [{"data": [remaining_short, positions[1]]},
                                       {"data": [positions[1]]}, {"data": [positions[1]]}, {"data": []}]
    client.get_order_by_id.side_effect = [rejected("Temporary service issue; try again", 10), traded(15), traded()]
    utils.close_open_fno_positions(client, positions)
    assert [c.kwargs["quantity"] for c in client.place_order.call_args_list] == [25, 15, 25]
    assert [c.kwargs["transaction_type"] for c in client.place_order.call_args_list] == ["BUY", "BUY", "SELL"]


@pytest.mark.parametrize("status", ["PENDING", "TRANSIT", "PART_TRADED", ""])
def test_unconfirmed_short_never_releases_hedge_or_duplicates(client, positions, monkeypatch, status):
    monkeypatch.setattr(utils, "EXIT_ORDER_POLL_ATTEMPTS", 1)
    client.get_order_by_id.return_value = {"data": {"orderStatus": status, "filledQty": 10}}
    with pytest.raises(ValueError, match="not confirmed"):
        utils.close_open_fno_positions(client, positions)
    assert client.place_order.call_count == 1
    client.get_positions.assert_not_called()


def test_permanent_rejection_does_not_resubmit_or_sell_hedge(client, positions):
    client.get_order_by_id.return_value = rejected("Insufficient funds")
    with pytest.raises(ValueError, match="Insufficient funds"):
        utils.close_open_fno_positions(client, positions)
    assert client.place_order.call_count == 1


def test_stale_short_position_keeps_hedge(client, positions, monkeypatch):
    monkeypatch.setattr(utils, "EXIT_ORDER_POLL_ATTEMPTS", 1)
    client.get_positions.return_value = {"data": positions}
    with pytest.raises(ValueError, match="Positions have not confirmed"):
        utils.close_open_fno_positions(client, positions)
    assert client.place_order.call_count == 1


def test_stale_partial_position_never_causes_oversized_retry(client, positions, monkeypatch):
    monkeypatch.setattr(utils, "EXIT_ORDER_POLL_ATTEMPTS", 1)
    client.get_order_by_id.return_value = rejected("Temporary error", 10)
    client.get_positions.return_value = {"data": positions}
    with pytest.raises(ValueError, match="Positions have not confirmed"):
        utils.close_open_fno_positions(client, positions)
    assert client.place_order.call_count == 1


def test_retries_are_bounded(client, positions, monkeypatch):
    monkeypatch.setattr(utils.time, "sleep", lambda _: None)
    client.get_order_by_id.return_value = rejected("Temporary error")
    client.get_positions.return_value = {"data": positions}
    with pytest.raises(ValueError, match="not confirmed"):
        utils.close_open_fno_positions(client, positions)
    assert client.place_order.call_count == 3
    assert all(c.kwargs["transaction_type"] == "BUY" for c in client.place_order.call_args_list)


def test_submission_timeout_is_not_blindly_retried(client, positions):
    client.place_order.side_effect = TimeoutError("unknown submission outcome")
    with pytest.raises(TimeoutError):
        utils.close_open_fno_positions(client, positions)
    assert client.place_order.call_count == 1


@pytest.mark.parametrize("entry", [True, False])
def test_pending_broker_order_blocks_new_submissions(client, positions, entry):
    client.get_order_list.return_value = {"data": [{"exchangeSegment": "NSE_FNO", "orderId": "pending-1", "orderStatus": "PENDING"}]}
    with pytest.raises(ValueError, match="Outstanding F&O order"):
        if entry:
            strategy.place_credit_spread_entry(client, {"security_id": "101"}, {"security_id": "102"}, 25)
        else:
            utils.close_open_fno_positions(client, positions)
    client.place_order.assert_not_called()


def test_entry_hedge_rejection_blocks_short(client):
    client.get_order_by_id.return_value = rejected("Order rejected")
    with pytest.raises(ValueError, match="BUY_PROTECTION"):
        strategy.place_credit_spread_entry(client, {"security_id": "101"}, {"security_id": "102"}, 25)
    assert client.place_order.call_count == 1


def test_shared_lock_prevents_overlap():
    with utils.trading_lock("unit-test-lock"):
        with pytest.raises(OSError):
            with utils.trading_lock("unit-test-lock"):
                pytest.fail("overlapping execution acquired the lock")
    with utils.trading_lock("unit-test-lock"):
        pass
