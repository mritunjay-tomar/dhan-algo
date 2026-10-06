from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo
import socket
import pytest
from dhan_algo.config import StrategyConfig
from dhan_algo.market import MarketAnalysis


def pytest_addoption(parser):
    parser.addoption("--dhan-integration", action="store_true", help="Allow real, read-only Dhan API tests")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--dhan-integration"):
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="Pass --dhan-integration to enable real Dhan reads"))


@pytest.fixture(autouse=True)
def network_guard(request, monkeypatch):
    if "integration" not in request.keywords:
        def blocked(*args, **kwargs):
            raise AssertionError("Unit tests must not access the network")
        monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def config():
    return StrategyConfig("test-client", "test-token", 15, "IDX_I", "INDEX", nifty_lot_size=25)


@pytest.fixture
def analysis():
    return MarketAnalysis([{"close": 101.0}], [100.0], "BULLISH", None,
                          datetime(2026, 10, 1, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata")))


@pytest.fixture
def positions():
    return [dict(securityId="101", exchangeSegment="NSE_FNO", netQty=-25,
                 sellAvg=100, drvOptionType="PUT", productType="MARGIN"),
            dict(securityId="102", exchangeSegment="NSE_FNO", netQty=25,
                 buyAvg=52, drvOptionType="PUT", drvExpiryDate="2099-01-01", productType="MARGIN")]


@pytest.fixture
def client():
    client = Mock(spec=["get_positions", "ticker_data", "place_order", "get_order_by_id"])
    client.get_positions.return_value = {"status": "success", "data": []}
    client.ticker_data.return_value = {"status": "success", "data": {"data": {"NSE_FNO": {
        "101": {"last_price": 80}, "102": {"last_price": 52}
    }}}}
    client.place_order.return_value = {"status": "success", "data": {"orderId": "order-1"}}
    client.get_order_by_id.return_value = {"status": "success", "data": {"orderStatus": "TRADED", "filledQty": 25}}
    return client


@pytest.fixture
def chain():
    def leg(i, delta, premium):
        return {"security_id": i, "greeks": {"delta": delta}, "last_price": premium,
                "top_bid_price": premium - 1, "top_ask_price": premium + 1}
    return {"data": {"oc": {
        "24900": {"pe": leg("102", -.25, 50), "ce": leg("202", .75, 150)},
        "25000": {"pe": leg("101", -.50, 100), "ce": leg("201", .50, 100)},
        "25100": {"pe": leg("103", -.75, 150), "ce": leg("203", .25, 50)},
    }}}
