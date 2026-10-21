from dataclasses import replace
from unittest.mock import Mock

import pytest

import strategy
import utils


def test_waits_for_stale_positions_then_reenters_same_cycle(config, client, positions,
                                                          analysis, monkeypatch):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    monkeypatch.setattr(strategy, "opposite_supertrend_cross", lambda *_: True)
    monkeypatch.setattr(utils.time, "sleep", lambda _: None)
    client.get_positions.side_effect = [{"data": positions}, {"data": [positions[1]]}, {"data": [positions[1]]}, {"data": []}, {"data": []}, {"data": []}]
    entry = Mock(return_value=0)
    monkeypatch.setattr(strategy, "entry_check", entry)

    assert strategy.exit_check(cfg, client, positions, analysis=analysis) == 0
    assert client.get_positions.call_count == 6
    assert client.place_order.call_count == 2
    assert client.get_order_by_id.call_count == 2
    entry.assert_called_once_with(cfg, client, analysis=analysis, reentry=True)


@pytest.mark.parametrize("direction,protective_id,short_id", [
    ("BULLISH", "102", "101"), ("BEARISH", "203", "201"),
])
@pytest.mark.parametrize("exit_reason", ["supertrend", "expiry"])
def test_exit_followed_by_both_new_entry_orders(config, client, positions, analysis,
                                              chain, monkeypatch, caplog, direction,
                                              protective_id, short_id, exit_reason):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    analysis = replace(analysis, current_direction=direction, crossover_signal=None)
    monkeypatch.setattr(utils.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        strategy,
        "opposite_supertrend_cross",
        lambda *_: exit_reason == "supertrend",
    )
    monkeypatch.setattr(
        strategy,
        "bought_option_expiring_within_two_days",
        lambda *_: exit_reason == "expiry",
    )
    monkeypatch.setattr(strategy, "dhan_request", Mock(side_effect=[
        {"data": ["2026-10-06", "2026-10-13"]}, chain,
    ]))
    client.get_positions.side_effect = [{"data": [positions[1]]}, {"data": [positions[1]]}, {"data": []}, {"data": []}, {"data": []}]
    with caplog.at_level("INFO"):
        assert strategy.exit_check(cfg, client, positions, analysis=analysis) == 0
    assert [(c.kwargs["security_id"], c.kwargs["transaction_type"])
            for c in client.place_order.call_args_list] == [
        ("101", "BUY"), ("102", "SELL"),
        (protective_id, "BUY"), (short_id, "SELL"),
    ]
    assert "REENTRY_STARTED" in caplog.text
    assert "REENTRY_ORDERS_SUBMITTED" in caplog.text


def test_partial_exit_eventually_fills_instead_of_aborting(client, monkeypatch):
    monkeypatch.setattr(utils.time, "sleep", lambda _: None)
    client.get_order_by_id.side_effect = [
        {"data": {"orderStatus": "PART_TRADED", "filledQty": 10}},
        {"data": {"orderStatus": "TRADED", "filledQty": 25}},
    ]
    orders = [{"security_id": "101", "quantity": 25,
               "response": {"data": {"orderId": "exit-1"}}}]
    confirmed, statuses = utils.wait_for_exit_orders_traded(client, orders)
    assert confirmed and statuses[0]["filled_quantity"] == 25
    assert client.get_order_by_id.call_count == 2


@pytest.mark.parametrize("status", ["REJECTED", "CANCELLED", "EXPIRED", "PENDING", "PART_TRADED"])
def test_unfinished_or_failed_exit_is_an_error_and_does_not_duplicate_orders(status, config, client,
                                                                          positions, analysis, monkeypatch):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    monkeypatch.setattr(utils, "EXIT_ORDER_POLL_ATTEMPTS", 1)
    monkeypatch.setattr(utils, "EXIT_ORDER_POLL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(strategy, "opposite_supertrend_cross", lambda *_: True)
    client.get_order_by_id.return_value = {"data": {"orderStatus": status, "filledQty": 10}}
    client.get_positions.return_value = {"data": positions}
    entry = Mock()
    monkeypatch.setattr(strategy, "entry_check", entry)
    assert strategy.exit_check(cfg, client, positions, analysis=analysis) == 1
    entry.assert_not_called()
    assert client.place_order.call_count == 1
    client.get_positions.assert_not_called()


def test_inconclusive_short_exit_keeps_hedge_and_blocks_reentry(config, client, positions,
                                                              analysis, monkeypatch):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    monkeypatch.setattr(utils.time, "sleep", lambda _: None)
    monkeypatch.setattr(strategy, "opposite_supertrend_cross", lambda *_: True)
    client.get_order_by_id.return_value = {"status": "success", "data": {}}
    entry = Mock(return_value=0)
    monkeypatch.setattr(strategy, "entry_check", entry)
    assert strategy.exit_check(cfg, client, positions, analysis=analysis) == 1
    assert client.get_order_by_id.call_count == 5
    assert client.place_order.call_count == 1
    entry.assert_not_called()


def test_flat_cycle_enters_without_waiting_for_a_new_crossover(config, client, analysis, chain, monkeypatch):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    monkeypatch.setattr(strategy, "analyse_market", lambda _: analysis)
    monkeypatch.setattr(strategy, "dhan_request", Mock(side_effect=[
        {"data": ["2026-10-06", "2026-10-13"]}, chain,
    ]))
    assert analysis.crossover_signal is None
    assert strategy.run_strategy(cfg, client) == 0
    assert client.place_order.call_count == 2


def test_missing_direction_is_not_reported_as_a_successful_live_cycle(config, client, analysis):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    assert strategy.entry_check(cfg, client, analysis=replace(analysis, current_direction=None)) == 1
    client.place_order.assert_not_called()
