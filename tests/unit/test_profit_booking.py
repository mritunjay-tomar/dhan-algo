from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo
import sys

import pytest

import profit_booking
import utils


def test_no_position_does_not_request_prices_or_enter(config, client):
    assert profit_booking.run_profit_booking(config, client) == 0
    client.ticker_data.assert_not_called()
    client.place_order.assert_not_called()


@pytest.mark.parametrize("price,hit", [(80, False), (76, True), (75.99, True), (76.01, False)])
def test_fixed_fifty_percent_target(config, client, positions, caplog, price, hit):
    client.get_positions.return_value = {"data": positions}
    client.ticker_data.return_value["data"]["data"]["NSE_FNO"]["101"]["last_price"] = price
    with caplog.at_level("INFO"):
        assert profit_booking.run_profit_booking(config, client) == 0
    assert ("DRY_RUN_PROFIT_EXIT" in caplog.text) is hit
    assert '"target_fraction":0.5' in caplog.text
    client.place_order.assert_not_called()


def test_live_profit_exit_closes_both_legs_without_entry(config, client, positions, monkeypatch, caplog):
    monkeypatch.setattr(utils.time, "sleep", lambda _: None)
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    client.ticker_data.return_value["data"]["data"]["NSE_FNO"]["101"]["last_price"] = 76
    client.get_positions.side_effect = [{"data": positions}, {"data": [positions[1]]},
                                       {"data": [positions[1]]}, {"data": []}, {"data": []}, {"data": []}]
    with caplog.at_level("INFO"):
        assert profit_booking.run_profit_booking(cfg, client) == 0
    assert [c.kwargs["transaction_type"] for c in client.place_order.call_args_list] == ["BUY", "SELL"]
    assert "PROFIT_EXIT_CONFIRMED" in caplog.text
    assert "REENTRY" not in caplog.text


def test_unmatched_hedge_reports_incomplete_spread(config, client, positions, caplog):
    client.get_positions.return_value = {"data": [positions[1]]}
    with caplog.at_level("INFO"):
        assert profit_booking.run_profit_booking(config, client) == 1
    assert "PROFIT_CHECK_UNAVAILABLE" in caplog.text
    client.place_order.assert_not_called()


@pytest.mark.parametrize("dry,enabled", [(True, True), (True, False), (False, False)])
def test_profit_booking_respects_both_live_flags(config, client, positions, dry, enabled):
    client.get_positions.return_value = {"data": positions}
    client.ticker_data.return_value["data"]["data"]["NSE_FNO"]["101"]["last_price"] = 76
    assert profit_booking.run_profit_booking(replace(config, dry_run=dry, live_trading_enabled=enabled), client) == 0
    client.place_order.assert_not_called()


@pytest.mark.parametrize("day,hour,minute,allowed", [
    (21, 9, 14, False), (21, 9, 15, True), (21, 15, 30, True),
    (21, 15, 31, False), (24, 10, 0, False), (25, 10, 0, False),
])
def test_market_session_guard(day, hour, minute, allowed):
    now = datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert profit_booking.in_trading_session(now) is allowed
    assert profit_booking.in_trading_session(now.astimezone(ZoneInfo("UTC"))) is allowed


def test_main_outside_session_does_not_load_credentials(monkeypatch):
    monkeypatch.setattr(profit_booking, "in_trading_session", lambda: False)
    load = Mock(side_effect=AssertionError("must not load credentials"))
    monkeypatch.setattr(utils, "load_config", load)
    assert profit_booking.main() == 0
    load.assert_not_called()


def test_main_reports_order_failure(config, client, monkeypatch):
    monkeypatch.setattr(profit_booking, "in_trading_session", lambda: True)
    monkeypatch.setattr(utils, "load_config", lambda: config)
    monkeypatch.setitem(sys.modules, "dhanhq", SimpleNamespace(DhanContext=lambda *_: None, dhanhq=lambda *_: client))
    monkeypatch.setattr(profit_booking, "run_profit_booking", Mock(side_effect=ValueError("order rejected")))
    assert profit_booking.main() == 1


def test_profit_uses_actual_position_quantity(client, positions):
    client.ticker_data.return_value["data"]["data"]["NSE_FNO"]["101"]["last_price"] = 76
    reached, check = utils.spread_profit_target_reached(client, positions, 10, 65)
    assert reached
    assert check["quantity"] == 25
    assert check["profit_booking_amount"] == 600


@pytest.mark.parametrize("field,value", [("netQty", 10), ("drvOptionType", "CALL"), ("productType", "INTRADAY")])
def test_invalid_spread_cannot_trigger_profit_exit(client, positions, field, value):
    positions[1][field] = value
    reached, check = utils.spread_profit_target_reached(client, positions, 1, 25)
    assert not reached and not check["calculable"]
    client.place_order.assert_not_called()


def test_profit_exit_not_confirmed_flat_returns_failure(config, client, positions, monkeypatch):
    cfg = replace(config, dry_run=False, live_trading_enabled=True)
    client.get_positions.return_value = {"data": positions}
    client.ticker_data.return_value["data"]["data"]["NSE_FNO"]["101"]["last_price"] = 76
    monkeypatch.setattr(utils, "close_open_fno_positions", lambda *_: [])
    monkeypatch.setattr(utils, "EXIT_ORDER_POLL_TIMEOUT_SECONDS", 0)
    assert profit_booking.run_profit_booking(cfg, client) == 1
