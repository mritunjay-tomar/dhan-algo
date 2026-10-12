from dataclasses import replace
from unittest.mock import Mock

import pytest

import strategy as standalone
from dhan_algo import execution
from dhan_algo.broker import DhanBroker
from dhan_algo.strategies import credit_spread


@pytest.mark.parametrize("module", [credit_spread, standalone])
def test_waits_for_stale_positions_then_reenters_same_cycle(module, config, client, positions,
                                                          analysis, monkeypatch):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    if module is credit_spread:
        monkeypatch.setattr(module, "spread_profit_target_reached", lambda *_: (True, {}))
    else:
        monkeypatch.setattr(module, "short_legs_at_half_price", lambda *_: (True, []))
    clock_module = execution if module is credit_spread else standalone
    monkeypatch.setattr(clock_module.time, "sleep", lambda _: None)
    client.get_positions.side_effect = [{"data": positions}, {"data": positions}, {"data": []}]
    entry = Mock(return_value=0)
    monkeypatch.setattr(module, "entry_check", entry)

    assert module.exit_check(cfg, client, positions, analysis=analysis) == 0
    assert client.get_positions.call_count == 3
    assert client.place_order.call_count == 2
    assert client.get_order_by_id.call_count == 2
    entry.assert_called_once_with(cfg, client, analysis=analysis, reentry=True)


@pytest.mark.parametrize("direction,protective_id,short_id", [
    ("BULLISH", "102", "101"), ("BEARISH", "203", "201"),
])
def test_exit_followed_by_both_new_entry_orders(config, client, positions, analysis,
                                              chain, monkeypatch, direction, protective_id, short_id):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    analysis = replace(analysis, current_direction=direction, crossover_signal=None)
    monkeypatch.setattr(execution.time, "sleep", lambda _: None)
    monkeypatch.setattr(credit_spread, "spread_profit_target_reached", lambda *_: (True, {}))
    monkeypatch.setattr(credit_spread, "dhan_request", Mock(side_effect=[
        {"data": ["2026-10-06", "2026-10-13"]}, chain,
    ]))
    client.get_positions.side_effect = [{"data": positions}, {"data": []}]
    assert credit_spread.exit_check(cfg, DhanBroker(client, cfg), positions, analysis=analysis) == 0
    assert [(c.kwargs["security_id"], c.kwargs["transaction_type"])
            for c in client.place_order.call_args_list] == [
        ("101", "BUY"), ("102", "SELL"),
        (protective_id, "BUY"), (short_id, "SELL"),
    ]


@pytest.mark.parametrize("module", [execution, standalone])
def test_partial_exit_eventually_fills_instead_of_aborting(module, client, monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    client.get_order_by_id.side_effect = [
        {"data": {"orderStatus": "PART_TRADED", "filledQty": 10}},
        {"data": {"orderStatus": "TRADED", "filledQty": 25}},
    ]
    orders = [{"security_id": "101", "quantity": 25,
               "response": {"data": {"orderId": "exit-1"}}}]
    confirmed, statuses = module.wait_for_exit_orders_traded(client, orders)
    assert confirmed and statuses[0]["filled_quantity"] == 25
    assert client.get_order_by_id.call_count == 2


@pytest.mark.parametrize("status", ["REJECTED", "CANCELLED", "EXPIRED", "PENDING", "PART_TRADED"])
def test_unfinished_or_failed_exit_is_an_error_and_does_not_duplicate_orders(status, config, client,
                                                                          positions, analysis, monkeypatch):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    monkeypatch.setattr(execution, "EXIT_ORDER_POLL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(credit_spread, "spread_profit_target_reached", lambda *_: (True, {}))
    client.get_order_by_id.return_value = {"data": {"orderStatus": status, "filledQty": 10}}
    entry = Mock()
    monkeypatch.setattr(credit_spread, "entry_check", entry)
    assert credit_spread.exit_check(cfg, client, positions, analysis=analysis) == 1
    entry.assert_not_called()
    assert client.place_order.call_count == 2
    client.get_positions.assert_not_called()


def test_flat_cycle_enters_without_waiting_for_a_new_crossover(config, client, analysis, chain, monkeypatch):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    monkeypatch.setattr(credit_spread, "analyse_market", lambda _: analysis)
    monkeypatch.setattr(credit_spread, "dhan_request", Mock(side_effect=[
        {"data": ["2026-10-06", "2026-10-13"]}, chain,
    ]))
    assert analysis.crossover_signal is None
    assert credit_spread.run_strategy(cfg, client) == 0
    assert client.place_order.call_count == 2


def test_missing_direction_is_not_reported_as_a_successful_live_cycle(config, client, analysis):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    assert credit_spread.entry_check(cfg, client, analysis=replace(analysis, current_direction=None)) == 1
    client.place_order.assert_not_called()


def test_other_strategys_positions_do_not_delay_reentry(config, client, positions, analysis, monkeypatch):
    cfg = replace(config, dry_run=False, live_trading_enabled=True, security_ids=frozenset({"101", "102"}))
    monkeypatch.setattr(credit_spread, "spread_profit_target_reached", lambda *_: (True, {}))
    client.get_positions.return_value = {"data": [{"securityId": "999", "exchangeSegment": "NSE_FNO",
                                                 "netQty": 25}]}
    entry = Mock(return_value=0)
    monkeypatch.setattr(credit_spread, "entry_check", entry)
    broker = DhanBroker(client, cfg)
    assert credit_spread.exit_check(cfg, broker, positions, analysis=analysis) == 0
    entry.assert_called_once_with(cfg, broker, analysis=analysis, reentry=True)
