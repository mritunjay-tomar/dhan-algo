"""API-driven rule tests: only SDK responses and HTTP transport are mocked."""

import copy
import itertools
import json
from dataclasses import replace
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pytest
import strategy as cloud
from dhan_algo import http, market
from dhan_algo import positions as position_rules
from dhan_algo.broker import DhanBroker
from dhan_algo.strategies import credit_spread

NOW = datetime(2026, 10, 12, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
REASONS = (
    "RUNNING_PROFIT_AT_OR_ABOVE_50_PERCENT_OF_MAXIMUM_PROFIT",
    "OPPOSITE_30_MINUTE_SUPERTREND_CROSS",
    "LONG_OPTION_EXPIRY_WITHIN_TWO_DAYS",
)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def historical_response(pattern):
    data = {key: [] for key in ("open", "high", "low", "close", "timestamp")}
    dates = [6, 7, 8, 9, 12]
    for i in range(120):
        timestamp = NOW.replace(day=dates[i // 24], hour=9, minute=15) + timedelta(
            minutes=15 * (i % 24)
        )
        price = 100 + i * 0.8 if pattern in ("up", "cross_down") else 200 - i * 0.8
        if i >= 118 and pattern.startswith("cross_"):
            price = 80 - (i - 118) if pattern == "cross_down" else 250 + (i - 118)
        for key, value in {
            "open": price,
            "high": price + 2,
            "low": price - 2,
            "close": price,
            "timestamp": int(timestamp.timestamp()),
        }.items():
            data[key].append(value)
    return data


class MockDhanAPI:
    def __init__(
        self,
        module,
        monkeypatch,
        config,
        spread,
        chain,
        pattern="up",
        short_ltp=80,
        long_ltp=52,
        wrapped=True,
        live_enabled=False,
    ):
        self.module = module
        self.config = replace(config, dry_run=True, live_trading_enabled=live_enabled)
        self.spread = copy.deepcopy(spread)
        self.calls = []
        self.sdk = Mock(
            spec=["get_positions", "ticker_data", "place_order", "get_order_by_id"]
        )
        self.sdk.get_positions.return_value = (
            {"status": "success", "data": self.spread} if wrapped else self.spread
        )
        self.sdk.ticker_data.return_value = {
            "status": "success",
            "data": {
                "data": {
                    "NSE_FNO": {
                        "101": {"last_price": short_ltp},
                        "102": {"last_price": long_ltp},
                    }
                }
            },
        }
        self.sdk.place_order.side_effect = AssertionError(
            "Dry-run must never submit orders"
        )
        self.sdk.get_order_by_id.side_effect = AssertionError(
            "Dry-run must never poll nonexistent orders"
        )
        self.responses = {
            "/charts/intraday": historical_response(pattern),
            "/optionchain/expirylist": {"data": ["2026-10-13", "2026-10-20"]},
            "/optionchain": copy.deepcopy(chain),
        }
        self.opener = Mock(side_effect=self.open)
        monkeypatch.setattr(http, "urlopen", self.opener)
        monkeypatch.setattr(cloud, "urlopen", self.opener)
        # TLS construction belongs to the mocked transport, not the rule logic.
        monkeypatch.setattr(http.ssl, "create_default_context", lambda **_: None)
        for component in (market, position_rules, cloud):
            monkeypatch.setattr(component, "datetime", FixedDatetime)

    def open(self, request, **kwargs):
        path = urlparse(request.full_url).path.removeprefix("/v2")
        self.calls.append(
            {
                "path": path,
                "body": json.loads(request.data),
                "method": request.method,
                "token": request.get_header("Access-token"),
                "timeout": kwargs["timeout"],
            }
        )
        payload = self.responses[path]
        if isinstance(payload, Exception):
            raise payload
        result = MagicMock()
        result.__enter__.return_value.read.return_value = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )
        return result

    def run(self):
        broker = (
            DhanBroker(self.sdk, self.config)
            if self.module is credit_spread
            else self.sdk
        )
        return self.module.run_strategy(self.config, broker)

    def assert_read_only(self):
        self.sdk.place_order.assert_not_called()
        self.sdk.get_order_by_id.assert_not_called()


@pytest.fixture(params=[credit_spread, cloud], ids=["runner", "cloud"])
def implementation(request):
    return request.param


def events(caplog, name):
    records = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("event") == name:
            records.append(payload)
    return records


def spread_positions(positions, option="PUT", days=3):
    spread = copy.deepcopy(positions)
    for leg in spread:
        leg["drvOptionType"] = option
        leg["drvExpiryDate"] = (NOW.date() + timedelta(days=days)).isoformat()
    return spread


@pytest.mark.parametrize(
    "target,cross,expiry", list(itertools.product([False, True], repeat=3))
)
@pytest.mark.parametrize("option", ["PUT", "CALL"])
@pytest.mark.parametrize(
    "wrapped", [True, False], ids=["sdk-wrapped", "raw-position-list"]
)
@pytest.mark.parametrize(
    "live_enabled", [True, False], ids=["live-flag-on", "live-flag-off"]
)
def test_api_dry_run_exit_and_reentry_matrix(
    implementation,
    config,
    positions,
    chain,
    monkeypatch,
    caplog,
    target,
    cross,
    expiry,
    option,
    wrapped,
    live_enabled,
):
    caplog.set_level("INFO")
    pattern = (
        ("cross_down" if option == "PUT" else "cross_up")
        if cross
        else ("up" if option == "PUT" else "down")
    )
    spread = spread_positions(positions, option, 2 if expiry else 3)
    api = MockDhanAPI(
        implementation,
        monkeypatch,
        config,
        spread,
        chain,
        pattern=pattern,
        short_ltp=76 if target else 80,
        wrapped=wrapped,
        live_enabled=live_enabled,
    )
    assert api.run() == 0
    api.sdk.get_positions.assert_called_once_with()
    api.sdk.ticker_data.assert_called_once_with({"NSE_FNO": [101, 102]})
    checks = events(caplog, "EXIT_CONDITIONS_EVALUATED")
    assert len(checks) == 1
    check = checks[0]
    expected = [
        reason for flag, reason in zip((target, cross, expiry), REASONS) if flag
    ]
    assert check["reasons"] == expected
    assert check["profit_check"]["maximum_profit"] == 1200
    assert check["profit_check"]["profit_booking_amount"] == 600
    assert check["profit_check"]["running_profit"] == (600 if target else 500)
    evaluation = events(caplog, "SUPERTREND_EVALUATED")[0]
    if cross:
        assert evaluation["crossover_signal"] == (
            "BEARISH" if option == "PUT" else "BULLISH"
        )
    exit_events = events(caplog, "DRY_RUN_EXIT_SIGNAL")
    reentries = events(caplog, "DRY_RUN_REENTRY_SIGNAL")
    assert len(exit_events) == len(reentries) == int(bool(expected))
    if expected:
        assert exit_events[0]["current_positions"] == spread
        assert reentries[0]["signal"] == evaluation["current_direction"]
        assert reentries[0]["quantity"] == 25
        assert reentries[0]["sell"]["security_id"] == (
            "101" if evaluation["current_direction"] == "BULLISH" else "201"
        )
        assert [call["path"] for call in api.calls] == [
            "/charts/intraday",
            "/optionchain/expirylist",
            "/optionchain",
        ]
        assert api.calls[-1]["body"]["Expiry"] == "2026-10-20"
    else:
        assert len(events(caplog, "NO_EXIT_SIGNAL")) == 1
        assert [call["path"] for call in api.calls] == ["/charts/intraday"]
    assert all(
        call["method"] == "POST" and call["token"] == "test-token" for call in api.calls
    )
    assert api.calls[0]["body"]["interval"] == 15
    assert api.spread == spread  # A hypothetical exit does not mutate broker positions.
    api.assert_read_only()


@pytest.mark.parametrize(
    "short_ltp,long_ltp,exit_required",
    [
        (76, 52, True),
        (76.0001, 52, False),
        (75.9999, 52, True),
        (70, 48, True),
        (70, 30, False),
        (80, 52, False),
    ],
)
def test_api_profit_target_boundaries_include_both_legs(
    implementation,
    config,
    positions,
    chain,
    monkeypatch,
    caplog,
    short_ltp,
    long_ltp,
    exit_required,
):
    caplog.set_level("INFO")
    api = MockDhanAPI(
        implementation,
        monkeypatch,
        config,
        spread_positions(positions),
        chain,
        short_ltp=short_ltp,
        long_ltp=long_ltp,
    )
    assert api.run() == 0
    assert bool(events(caplog, "DRY_RUN_EXIT_SIGNAL")) == exit_required
    assert bool(events(caplog, "DRY_RUN_REENTRY_SIGNAL")) == exit_required
    api.assert_read_only()


@pytest.mark.parametrize(
    "days,exit_required", [(-1, False), (0, True), (1, True), (2, True), (3, False)]
)
def test_api_expiry_day_boundaries(
    implementation, config, positions, chain, monkeypatch, caplog, days, exit_required
):
    caplog.set_level("INFO")
    api = MockDhanAPI(
        implementation,
        monkeypatch,
        config,
        spread_positions(positions, days=days),
        chain,
    )
    assert api.run() == 0
    assert bool(events(caplog, "DRY_RUN_EXIT_SIGNAL")) == exit_required
    assert bool(events(caplog, "DRY_RUN_REENTRY_SIGNAL")) == exit_required
    api.assert_read_only()


@pytest.mark.parametrize(
    "payload", [None, {"status": "failure", "data": []}, {"data": {}}, {"data": [None]}]
)
def test_api_bad_position_responses_cannot_trigger_entry(
    implementation, config, positions, chain, monkeypatch, payload
):
    api = MockDhanAPI(
        implementation, monkeypatch, config, spread_positions(positions), chain
    )
    api.sdk.get_positions.return_value = payload
    with pytest.raises(ValueError):
        api.run()
    assert not api.calls
    api.assert_read_only()


@pytest.mark.parametrize(
    "payload",
    [None, {"status": "failure"}, {}, {"data": {}}, {"data": {"NSE_FNO": []}}],
)
def test_api_bad_quote_response_fails_without_reentry(
    implementation, config, positions, chain, monkeypatch, payload
):
    api = MockDhanAPI(
        implementation, monkeypatch, config, spread_positions(positions), chain
    )
    api.sdk.ticker_data.return_value = payload
    with pytest.raises(ValueError):
        api.run()
    assert [call["path"] for call in api.calls] == ["/charts/intraday"]
    api.assert_read_only()


@pytest.mark.parametrize(
    "quote",
    [
        None,
        {},
        {"last_price": None},
        {"last_price": 0},
        {"last_price": -1},
        {"last_price": "bad"},
    ],
)
def test_api_unavailable_quote_does_not_fabricate_profit_exit(
    implementation, config, positions, chain, monkeypatch, caplog, quote
):
    caplog.set_level("INFO")
    api = MockDhanAPI(
        implementation, monkeypatch, config, spread_positions(positions), chain
    )
    api.sdk.ticker_data.return_value["data"]["data"]["NSE_FNO"]["101"] = quote
    assert api.run() == 0
    check = events(caplog, "EXIT_CONDITIONS_EVALUATED")[0]["profit_check"]
    assert not check["calculable"] and not check["target_reached"]
    assert not events(caplog, "DRY_RUN_REENTRY_SIGNAL")
    api.assert_read_only()


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/charts/intraday", b"not json"),
        ("/charts/intraday", {}),
        ("/charts/intraday", {"open": [], "high": [], "low": [], "close": []}),
        ("/charts/intraday", None),
        ("/optionchain/expirylist", None),
        ("/optionchain/expirylist", {"data": None}),
        ("/optionchain/expirylist", {"data": [None, "2026-10-20"]}),
        ("/optionchain/expirylist", {"status": "failure", "data": []}),
        ("/optionchain/expirylist", {"data": []}),
        ("/optionchain/expirylist", {"data": ["2026-10-13"]}),
        ("/optionchain/expirylist", b"not json"),
        ("/optionchain", {}),
        ("/optionchain", {"data": {"oc": {}}}),
        ("/optionchain", None),
        ("/optionchain", {"data": None}),
        ("/optionchain", {"data": {"oc": []}}),
        ("/optionchain", b"not json"),
    ],
)
def test_api_bad_http_payloads_produce_failure(
    implementation, config, positions, chain, monkeypatch, caplog, path, payload
):
    caplog.set_level("INFO")
    api = MockDhanAPI(
        implementation,
        monkeypatch,
        config,
        spread_positions(positions),
        chain,
        short_ltp=76,
    )
    api.responses[path] = payload
    if path == "/charts/intraday":
        with pytest.raises(ValueError):
            api.run()
    else:
        assert api.run() == 1
    assert not events(caplog, "DRY_RUN_REENTRY_SIGNAL")
    api.assert_read_only()


@pytest.mark.parametrize("trigger", ["profit", "expiry", "crossover"])
def test_api_each_repeated_exit_has_one_reentry(
    implementation, config, positions, chain, monkeypatch, caplog, trigger
):
    caplog.set_level("INFO")
    api = MockDhanAPI(
        implementation,
        monkeypatch,
        config,
        spread_positions(positions, days=2 if trigger == "expiry" else 3),
        chain,
        short_ltp=76 if trigger == "profit" else 80,
        pattern="cross_down" if trigger == "crossover" else "up",
    )
    for _ in range(3):
        assert api.run() == 0
    assert len(events(caplog, "DRY_RUN_EXIT_SIGNAL")) == 3
    assert len(events(caplog, "DRY_RUN_REENTRY_STARTED")) == 3
    assert len(events(caplog, "DRY_RUN_REENTRY_SIGNAL")) == 3
    assert api.sdk.get_positions.call_count == api.sdk.ticker_data.call_count == 3
    api.assert_read_only()


@pytest.mark.parametrize(
    "path", ["/charts/intraday", "/optionchain/expirylist", "/optionchain"]
)
@pytest.mark.parametrize(
    "failure", ["unauthorized", "rate-limited", "server-error", "offline"]
)
def test_api_http_failures_are_not_successful_trade_cycles(
    implementation, config, positions, chain, monkeypatch, path, failure
):
    api = MockDhanAPI(
        implementation,
        monkeypatch,
        config,
        spread_positions(positions),
        chain,
        short_ltp=76,
    )
    api.responses[path] = (
        URLError("mock network unavailable")
        if failure == "offline"
        else HTTPError(
            "https://api.dhan.co/v2" + path,
            {"unauthorized": 401, "rate-limited": 429, "server-error": 503}[failure],
            "mock API failure",
            {},
            BytesIO(b"mock error"),
        )
    )
    if path == "/charts/intraday":
        with pytest.raises((HTTPError, URLError)):
            api.run()
    else:
        assert api.run() == 1
    api.assert_read_only()


def test_api_empty_account_proposes_entry_in_dry_run(
    implementation, config, chain, monkeypatch, caplog
):
    caplog.set_level("INFO")
    api = MockDhanAPI(implementation, monkeypatch, config, [], chain)
    assert api.run() == 0
    assert len(events(caplog, "DRY_RUN_ENTRY_SIGNAL")) == 1
    assert not events(caplog, "EXIT_CONDITIONS_EVALUATED")
    api.sdk.ticker_data.assert_not_called()
    api.assert_read_only()


def test_api_price_wrappers_and_quantity_alias(
    implementation, config, positions, chain, monkeypatch, caplog
):
    caplog.set_level("INFO")
    spread = spread_positions(positions)
    for leg in spread:
        leg["netQuantity"] = leg.pop("netQty")
    api = MockDhanAPI(implementation, monkeypatch, config, spread, chain)
    api.sdk.ticker_data.return_value = {
        "status": "success",
        "data": {"NSE_FNO": {"101": 76, "102": 52}},
    }
    api.responses["/charts/intraday"] = {"data": api.responses["/charts/intraday"]}
    assert api.run() == 0
    assert len(events(caplog, "DRY_RUN_REENTRY_SIGNAL")) == 1
    api.assert_read_only()


def test_api_closed_position_records_are_treated_as_flat(
    implementation, config, positions, chain, monkeypatch, caplog
):
    caplog.set_level("INFO")
    spread = spread_positions(positions)
    for leg in spread:
        leg["netQty"] = 0
    api = MockDhanAPI(implementation, monkeypatch, config, spread, chain)
    assert api.run() == 0
    assert len(events(caplog, "DRY_RUN_ENTRY_SIGNAL")) == 1
    assert not events(caplog, "EXIT_CONDITIONS_EVALUATED")
    api.sdk.ticker_data.assert_not_called()
    api.assert_read_only()


@pytest.mark.parametrize("scoped", [True, False])
def test_api_manual_position_respects_the_configured_scope(
    config, positions, chain, monkeypatch, caplog, scoped
):
    caplog.set_level("INFO")
    spread = spread_positions(positions)
    spread.append({**spread[1], "securityId": "999"})
    cfg = replace(config, security_ids=frozenset({"101", "102"}) if scoped else None)
    api = MockDhanAPI(credit_spread, monkeypatch, cfg, spread, chain, short_ltp=76)
    assert api.run() == 0
    assert len(events(caplog, "DRY_RUN_REENTRY_SIGNAL")) == int(scoped)
    if scoped:
        assert len(events(caplog, "DRY_RUN_EXIT_SIGNAL")[0]["current_positions"]) == 2
    else:
        assert not events(caplog, "EXIT_CONDITIONS_EVALUATED")[0]["profit_check"][
            "calculable"
        ]
    api.assert_read_only()
