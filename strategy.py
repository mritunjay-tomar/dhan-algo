#!/usr/bin/env python3
"""Self-contained NIFTY credit-spread strategy for Dhan Cloud.

One scheduled invocation checks Dhan positions, then evaluates an active spread
or a new entry. Defaults remain read-only/dry-run.
"""

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


LOGGER = logging.getLogger("dhan_strategy")


def log_event(event: str, **details: Any) -> None:
    """Emit one structured, credential-free audit record to Cloud logs."""
    record = {"timestamp_ist": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(), "event": event, **details}
    LOGGER.info("%s", json.dumps(record, separators=(",", ":"), default=str))

import ssl
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import certifi
import pandas as pd
import pandas_ta as ta
import pyotp


API_BASE_URL = "https://api.dhan.co/v2"
TOKEN_URL = "https://auth.dhan.co/app/generateAccessToken"
EXIT_ORDER_POLL_INTERVAL_SECONDS = 1.0
EXIT_ORDER_POLL_ATTEMPTS = 5
EXIT_ORDER_POLL_TIMEOUT_SECONDS = 30.0
EXIT_FLAT_CONFIRMATIONS_REQUIRED = 2


@dataclass(frozen=True)
class StrategyConfig:
    """All runtime inputs needed for one strategy cycle."""

    client_id: str
    access_token: str = field(repr=False)
    nifty_security_id: int
    nifty_exchange_segment: str
    nifty_instrument: str
    nifty_option_underlying_security_id: int = 13
    supertrend_atr_period: int = 22
    supertrend_multiplier: float = 4.0
    supertrend_lookback_days: int = 60
    nifty_quantity: int = 1  # Number of lots.
    nifty_lot_size: int = 1  # Units per lot, supplied by the deployment configuration.
    request_timeout_seconds: float = 15.0
    dry_run: bool = True
    live_trading_enabled: bool = False

    def validate(self) -> None:
        required_values = (
            ("client_id", self.client_id),
            ("access_token", self.access_token),
            ("nifty_exchange_segment", self.nifty_exchange_segment),
            ("nifty_instrument", self.nifty_instrument),
        )
        for name, value in required_values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required.")
        for name in ("supertrend_atr_period", "supertrend_lookback_days", "nifty_security_id", "nifty_option_underlying_security_id"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if type(self.dry_run) is not bool or type(self.live_trading_enabled) is not bool:
            raise ValueError("Trading flags must be booleans.")
        if not math.isfinite(self.supertrend_multiplier) or self.supertrend_multiplier <= 0:
            raise ValueError("Supertrend multiplier must be positive and finite.")
        if (
            type(self.nifty_quantity) is not int
            or self.nifty_quantity < 1
            or type(self.nifty_lot_size) is not int
            or self.nifty_lot_size < 1
            or not math.isfinite(self.request_timeout_seconds)
            or self.request_timeout_seconds <= 0
        ):
            raise ValueError("Lot count and lot size must be positive integers and request timeout must be positive.")


def env_value(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required; configure it in the environment.")
    value = value.strip()
    if "{{" in value or "}}" in value:
        raise ValueError(f"{name} must contain a value, not a template placeholder.")
    return value


def env_bool(name: str, default: bool) -> bool:
    value = env_value(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false.")
    return value == "true"


def generate_access_token(client_id: str, pin: str, totp_secret: str, timeout_seconds: float) -> str:
    """Generate a fresh 24-hour Dhan token using the account's TOTP setup."""
    if len(pin) != 6 or not pin.isdigit():
        raise ValueError("DHAN_PIN must be the 6-digit numeric Dhan PIN.")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("DHAN_REQUEST_TIMEOUT_SECONDS must be positive and finite.")

    try:
        totp = pyotp.TOTP(totp_secret.replace(" ", "")).now()
    except Exception as error:
        raise ValueError("DHAN_TOTP_SECRET is not a valid TOTP secret.") from error

    query = urlencode({"dhanClientId": client_id, "pin": pin, "totp": totp})
    request = Request(f"{TOKEN_URL}?{query}", method="POST")
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urlopen(request, timeout=timeout_seconds, context=context) as response:
            payload: Any = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        # Do not include error.url because it contains the PIN and one-time password.
        raise RuntimeError(f"Dhan token generation failed with HTTP {error.code}.") from None
    except URLError as error:
        raise RuntimeError(f"Could not reach Dhan authentication service: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("Dhan authentication returned an invalid JSON response.") from error

    token = payload.get("accessToken") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip():
        message = payload.get("message") if isinstance(payload, dict) else None
        suffix = f": {message}" if isinstance(message, str) else ""
        raise RuntimeError(f"Dhan authentication response did not contain an access token{suffix}.")

    LOGGER.info("Generated a fresh Dhan access token; expiry: %s", payload.get("expiryTime", "not supplied"))
    return token.strip()


def load_config() -> StrategyConfig:
    """Build strategy settings from the process environment."""
    timeout_seconds = float(env_value("DHAN_REQUEST_TIMEOUT_SECONDS", "15"))
    config = StrategyConfig(
        client_id=env_value("CLIENT_ID"),
        access_token=env_value("DHAN_ACCESS_TOKEN"),
        nifty_security_id=int(env_value("NIFTY_SECURITY_ID", "15")),
        nifty_exchange_segment=env_value("NIFTY_EXCHANGE_SEGMENT", "IDX_I"),
        nifty_instrument=env_value("NIFTY_INSTRUMENT", "INDEX"),
        nifty_option_underlying_security_id=int(env_value("NIFTY_OPTION_UNDERLYING_SECURITY_ID", "13")),
        nifty_quantity=int(env_value("NIFTY_QUANTITY", "1")),
        nifty_lot_size=int(env_value("NIFTY_LOT_SIZE")),
        supertrend_atr_period=int(env_value("SUPERTREND_ATR_PERIOD", "22")),
        supertrend_multiplier=float(env_value("SUPERTREND_MULTIPLIER", "4")),
        supertrend_lookback_days=int(env_value("SUPERTREND_LOOKBACK_DAYS", "60")),
        request_timeout_seconds=timeout_seconds,
        dry_run=env_bool("DRY_RUN", True),
        live_trading_enabled=env_bool("LIVE_TRADING_ENABLED", False),
    )
    config.validate()
    return config


@dataclass(frozen=True)
class MarketAnalysis:
    """One completed-candle Supertrend calculation shared by a strategy cycle."""

    candles: list[dict[str, float]]
    supertrend_values: list[float | None]
    current_direction: str
    crossover_signal: str | None
    candle_close_time: datetime


def dhan_request(path: str, client_id: str, token: str, body: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    log_event("DHAN_HTTP_REQUEST", path=path, body=body, timeout_seconds=timeout_seconds, header_names=["Accept", "Content-Type", "access-token", "client-id"])
    request = Request(
        f"{API_BASE_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json", "access-token": token, "client-id": client_id},
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(request, timeout=timeout_seconds, context=context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    candle_count = len(data.get("close", [])) if path in {"/charts/intraday", "/charts/historical"} and isinstance(data, dict) and isinstance(data.get("close"), list) else None
    response_details = {key: value for key, value in payload.items() if key in {"status", "message", "errorCode", "errorMessage"}} if isinstance(payload, dict) else {}
    log_event("DHAN_HTTP_RESPONSE", path=path, response_type=type(payload).__name__, response_keys=sorted(payload) if isinstance(payload, dict) else [], candle_count=candle_count, response_details=response_details)
    return payload


def candle_rows(response: dict[str, Any]) -> list[dict[str, float]]:
    """Convert Dhan's column-oriented historical response into candle rows."""
    if not isinstance(response, dict):
        raise ValueError("Historical API response must be a JSON object.")
    data = response.get("data", response)
    required = ("open", "high", "low", "close")
    if not isinstance(data, dict) or any(not isinstance(data.get(k), list) for k in required):
        raise ValueError(f"Unexpected historical-data response: {json.dumps(response)[:500]}")
    count = len(data["close"])
    if "timestamp" not in data or any(len(data[key]) != count for key in (*required, "timestamp")):
        raise ValueError("Historical candle columns must have matching lengths and timestamps.")
    return [{**{key: float(data[key][i]) for key in required}, "timestamp": float(data.get("timestamp", [0] * len(data["close"]))[i])} for i in range(len(data["close"]))]


def completed_candles(candles: list[dict[str, float]], interval_minutes: int) -> list[dict[str, float]]:
    """Exclude the current unfinished native-interval candle from Dhan data."""
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return [
        candle for candle in candles
        if datetime.fromtimestamp(candle["timestamp"], tz=ZoneInfo("Asia/Kolkata")) + timedelta(minutes=interval_minutes) <= now
    ]


def resample_15m_to_30m(candles: list[dict[str, float]]) -> list[dict[str, float]]:
    """Combine completed 15-minute Dhan candles into NSE-aligned 30-minute candles."""
    ist = ZoneInfo("Asia/Kolkata")
    buckets: dict[tuple[date, int], list[dict[str, float]]] = {}
    for candle in candles:
        candle_time = datetime.fromtimestamp(candle["timestamp"], tz=ist)
        elapsed = candle_time.hour * 60 + candle_time.minute - (9 * 60 + 15)
        if elapsed >= 0:
            buckets.setdefault((candle_time.date(), elapsed // 30), []).append(candle)
    now = datetime.now(ist)
    result: list[dict[str, float]] = []
    for group in buckets.values():
        group.sort(key=lambda candle: candle["timestamp"])
        if len(group) != 2:
            continue
        first, second = group
        close_time = datetime.fromtimestamp(second["timestamp"], tz=ist) + timedelta(minutes=15)
        if second["timestamp"] - first["timestamp"] == 15 * 60 and close_time <= now:
            result.append({"open": first["open"], "high": max(first["high"], second["high"]), "low": min(first["low"], second["low"]), "close": second["close"], "timestamp": first["timestamp"]})
    return sorted(result, key=lambda candle: candle["timestamp"])


def last_market_date(today: date) -> date:
    """Return the latest weekday; weekend sessions have no NSE intraday candles."""
    market_date = today
    while market_date.weekday() >= 5:
        market_date -= timedelta(days=1)
    return market_date


def supertrend(candles: list[dict[str, float]], period: int, multiplier: float) -> list[float | None]:
    """Calculate pandas-ta Supertrend, preserving candle alignment and missing values."""
    if period < 1 or multiplier <= 0:
        raise ValueError("Supertrend period and multiplier must be positive.")
    if len(candles) < period + 2:
        raise ValueError(f"Need at least {period + 2} candles; received {len(candles)}.")

    frame = pd.DataFrame(candles)
    indicator = ta.supertrend(
        high=frame["high"],
        low=frame["low"],
        close=frame["close"],
        length=period,
        multiplier=float(multiplier),
    )
    if indicator is None or indicator.empty:
        raise ValueError("Supertrend calculation returned no values.")
    values = indicator[f"SUPERT_{period}_{float(multiplier)}"]
    return [None if pd.isna(value) else float(value) for value in values]


def cross_signal(candles: list[dict[str, float]], values: list[float | None]) -> str | None:
    prior, latest = len(candles) - 2, len(candles) - 1
    if values[prior] is None or values[latest] is None:
        return None
    if candles[prior]["close"] <= values[prior] and candles[latest]["close"] > values[latest]:
        return "BULLISH"  # put credit spread
    if candles[prior]["close"] >= values[prior] and candles[latest]["close"] < values[latest]:
        return "BEARISH"  # call credit spread
    return None


def current_direction(candles: list[dict[str, float]], values: list[float | None]) -> str:
    """Direction for re-entry, based strictly on the latest closed 30m candle."""
    latest = len(candles) - 1
    if values[latest] is None:
        raise ValueError("Latest Supertrend value is unavailable.")
    return "BULLISH" if candles[latest]["close"] > values[latest] else "BEARISH"


def analyse_market(config: StrategyConfig) -> MarketAnalysis:
    """Fetch and analyse completed NIFTY candles once for one strategy cycle."""
    config.validate()
    market_date = last_market_date(date.today())
    query_end = market_date + timedelta(days=1)
    historical = dhan_request(
        "/charts/intraday",
        config.client_id,
        config.access_token,
        {
            "securityId": config.nifty_security_id,
            "exchangeSegment": config.nifty_exchange_segment,
            "instrument": config.nifty_instrument,
            "interval": 15,
            "oi": False,
            "fromDate": str(market_date - timedelta(days=config.supertrend_lookback_days)),
            "toDate": str(query_end),
        },
        config.request_timeout_seconds,
    )
    log_event(
        "INTRADAY_WINDOW_SELECTED",
        timeframe="30_MINUTE",
        latest_market_date=str(market_date),
        from_date=str(market_date - timedelta(days=config.supertrend_lookback_days)),
        to_date=str(query_end),
    )
    candles = resample_15m_to_30m(candle_rows(historical))
    values = supertrend(candles, config.supertrend_atr_period, config.supertrend_multiplier)
    latest = len(candles) - 1
    candle_close_time = datetime.fromtimestamp(candles[latest]["timestamp"], tz=ZoneInfo("Asia/Kolkata")) + timedelta(minutes=30)
    analysis = MarketAnalysis(
        candles=candles,
        supertrend_values=values,
        current_direction=current_direction(candles, values),
        crossover_signal=cross_signal(candles, values),
        candle_close_time=candle_close_time,
    )
    log_event(
        "SUPERTREND_EVALUATED",
        timeframe="30_MINUTE",
        close=candles[latest]["close"],
        supertrend=values[latest],
        current_direction=analysis.current_direction,
        crossover_signal=analysis.crossover_signal,
        candle_close_ist=candle_close_time.isoformat(),
    )
    return analysis


def next_expiry(expiry_response: dict[str, Any]) -> str:
    if not isinstance(expiry_response, dict) or expiry_response.get("status") == "failure":
        raise ValueError("Invalid Dhan expiry-list response.")
    values = expiry_response.get("data")
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("Dhan expiry list must contain date strings.")
    expiries = sorted(expiry_response.get("data", []))
    if len(expiries) < 2:
        raise ValueError(f"Need at least two active expiries to select next week; received {expiries!r}")
    return expiries[1]


def option_legs(
    chain_response: dict[str, Any],
    signal: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return sell Δ0.50 and protective buy whose premium is ~50% of sell premium."""

    if signal not in {"BULLISH", "BEARISH"}:
        raise ValueError("Unknown Supertrend direction.")
    if not isinstance(chain_response, dict) or chain_response.get("status") == "failure":
        raise ValueError("Invalid Dhan option-chain response.")
    data = chain_response.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("oc"), dict):
        raise ValueError("Dhan option chain must contain a strike dictionary.")
    option_chain = data["oc"]

    side, target_sell = (
        ("pe", -0.50)
        if signal == "BULLISH"
        else ("ce", 0.50)
    )

    candidates: list[dict[str, Any]] = []

    for strike, legs in option_chain.items():
        leg = legs.get(side) if isinstance(legs, dict) else None
        greeks = leg.get("greeks", {}) if isinstance(leg, dict) else {}
        delta = greeks.get("delta")
        last_price = leg.get("last_price") if isinstance(leg, dict) else None

        if (
            isinstance(delta, (int, float))
            and isinstance(last_price, (int, float))
            and last_price > 0
            and leg.get("security_id")
        ):
            candidates.append({
                "strike": float(strike),
                "delta": float(delta),
                "security_id": str(leg["security_id"]),
                "last_price": float(last_price),
                "top_bid_price": leg.get("top_bid_price"),
                "top_ask_price": leg.get("top_ask_price"),
            })

    if not candidates:
        raise ValueError(
            "No option contracts with delta/security_id/last_price were returned by Dhan."
        )

    # Sell leg: closest to delta 0.50
    sell = min(
        candidates,
        key=lambda item: abs(item["delta"] - target_sell)
    )

    # Buy leg target premium = 50% of sell premium
    target_buy_price = sell["last_price"] * 0.50

    # Buy leg: premium closest to half of sell premium
    protective = [leg for leg in candidates if
                  (signal == "BULLISH" and leg["strike"] < sell["strike"])
                  or (signal == "BEARISH" and leg["strike"] > sell["strike"])]
    if not protective:
        raise ValueError("No out-of-the-money protective contract is available.")
    buy = min(
        protective,
        key=lambda item: abs(item["last_price"] - target_buy_price)
    )

    if sell["security_id"] == buy["security_id"]:
        raise ValueError(
            "Sell and buy selections resolved to the same contract; no spread proposed."
        )

    return sell, buy


def spread_payoff(sell: dict[str, Any], buy: dict[str, Any], quantity: int) -> dict[str, float]:
    """Conservative credit-spread economics using sell bid and buy ask."""
    sell_bid, buy_ask = sell.get("top_bid_price"), buy.get("top_ask_price")
    if not isinstance(sell_bid, (int, float)) or not isinstance(buy_ask, (int, float)):
        raise ValueError("Option-chain response is missing executable bid/ask prices.")
    credit = float(sell_bid) - float(buy_ask)
    width = abs(float(sell["strike"]) - float(buy["strike"]))
    if credit <= 0:
        raise ValueError(f"Selected legs do not form a credit spread at current bid/ask (credit={credit:.2f}).")
    if credit >= width:
        raise ValueError("Credit is not smaller than spread width; refusing invalid payoff calculation.")
    return {"net_credit_per_unit": credit, "maximum_profit": credit * quantity, "maximum_loss": (width - credit) * quantity, "profit_booking_debit_per_unit": credit / 2, "profit_booking_amount": (credit / 2) * quantity, "spread_width": width}


def place_credit_spread_entry(dhan: Any, sell: dict[str, Any], buy: dict[str, Any], quantity: int) -> list[dict[str, Any]]:
    """Submit the protective buy, then immediately submit the short sell.

    Dhan can acknowledge an order before its status endpoint reports a fill, so
    entry checks submission success without waiting on the status endpoint.
    """
    orders: list[dict[str, Any]] = []
    for leg_name, leg, transaction_type in (
        ("BUY_PROTECTION", buy, "BUY"),
        ("SELL_SHORT", sell, "SELL"),
    ):
        security_id = str(leg["security_id"])
        log_event("ENTRY_ORDER_INTENT", leg=leg_name, security_id=security_id, transaction_type=transaction_type, quantity=quantity, order_type="MARKET", product_type="MARGIN")
        response = dhan.place_order(
            security_id=security_id, exchange_segment="NSE_FNO",
            transaction_type=transaction_type, quantity=quantity,
            order_type="MARKET", product_type="MARGIN", price=0,
        )
        log_event("ENTRY_ORDER_RESPONSE", leg=leg_name, security_id=security_id, response=response)
        order = {"leg": leg_name, "security_id": security_id, "quantity": quantity, "response": response}
        orders.append(order)
        if not isinstance(response, dict) or str(response.get("status", "")).lower() == "failure":
            raise ValueError(f"{leg_name} order submission failed: {response}")
    return orders



def entry_check(
    config: StrategyConfig,
    dhan: Any,
    *,
    analysis: MarketAnalysis | None = None,
    reentry: bool = False,
) -> int:
    try:
        config.validate()
        log_event("ENTRY_CHECK_STARTED", reentry=reentry, lots=config.nifty_quantity, dry_run=config.dry_run, live_trading_enabled=config.live_trading_enabled)
        analysis = analysis or analyse_market(config)
        client_id, token = config.client_id, config.access_token
        segment = config.nifty_exchange_segment
        signal = analysis.current_direction
        print(f"Completed NIFTY 30-minute candle ending {analysis.candle_close_time:%Y-%m-%d %H:%M IST}: close={analysis.candles[-1]['close']:.2f}; Supertrend={analysis.supertrend_values[-1]:.2f}; ATR period={config.supertrend_atr_period}; multiplier={config.supertrend_multiplier}")
        if not signal:
            print("No Supertrend direction is available. No spread proposed.")
            log_event("NO_ENTRY_SIGNAL", close=analysis.candles[-1]["close"], supertrend=analysis.supertrend_values[-1], candle_close_ist=analysis.candle_close_time.isoformat())
            return 0 if config.dry_run or not config.live_trading_enabled else 1

        expiry_request = {"UnderlyingScrip": config.nifty_option_underlying_security_id, "UnderlyingSeg": segment}
        expiry = next_expiry(dhan_request("/optionchain/expirylist", client_id, token, expiry_request, config.request_timeout_seconds))
        chain = dhan_request("/optionchain", client_id, token, {**expiry_request, "Expiry": expiry}, config.request_timeout_seconds)
        sell, buy = option_legs(chain, signal)
        lots = config.nifty_quantity
        lot_size = config.nifty_lot_size
        quantity = lots * lot_size
        log_event("ENTRY_QUANTITY_RESOLVED", lots=lots, lot_size=lot_size, quantity=quantity)
        payoff = spread_payoff(sell, buy, quantity)
        log_event("ENTRY_LEGS_SELECTED", signal=signal, expiry=expiry, sell=sell, buy=buy, payoff=payoff, quantity=quantity)
        option_type = "PUT" if signal == "BULLISH" else "CALL"
        trigger = "current 30-minute Supertrend direction"
        print(f"{trigger}: propose next-expiry ({expiry}) {option_type} credit spread:")
        output: dict[str, Any] = {"SELL": sell, "BUY": buy, "payoff": payoff, "lots": lots, "lot_size": lot_size, "quantity": quantity, "dry_run": config.dry_run}
        if not config.live_trading_enabled or config.dry_run:
            print(json.dumps(output, indent=2))
            log_event("DRY_RUN_REENTRY_SIGNAL" if reentry else "DRY_RUN_ENTRY_SIGNAL", signal=signal, expiry=expiry, sell=sell, buy=buy, payoff=payoff, quantity=quantity)
            return 0
        try:
            output["orders"] = place_credit_spread_entry(dhan, sell, buy, quantity)
        except Exception as error:
            output["order_error"] = str(error)
            print(json.dumps(output, indent=2, default=str))
            log_event("ENTRY_ORDER_FAILURE", signal=signal, expiry=expiry, **output)
            return 1
        print(json.dumps(output, indent=2, default=str))
        log_event(
            "REENTRY_ORDERS_SUBMITTED" if reentry else "ENTRY_ORDERS_SUBMITTED",
            signal=signal,
            expiry=expiry,
            **output,
        )
        return 0
    except HTTPError as error:
        LOGGER.error("DhanHQ HTTP %s: %s", error.code, error.read().decode("utf-8", errors="replace"))
        log_event("ENTRY_CHECK_FAILED", error_type="HTTPError", status_code=error.code)
    except (URLError, ValueError, json.JSONDecodeError) as error:
        LOGGER.error("Entry scan failed: %s", error)
        log_event("ENTRY_CHECK_FAILED", error_type=type(error).__name__, error=str(error))
    return 1




def dhan_positions(dhan: Any) -> list[dict[str, Any]]:
    """Fetch the current account positions through the authenticated Dhan SDK."""
    payload = dhan.get_positions()
    # Dhan API versions may return either a list directly or wrap it in a data
    # field. Reject any other response shape instead of iterating dictionary
    # keys and accidentally treating a malformed response as a position list.
    if isinstance(payload, dict) and str(payload.get("status", "")).lower() == "failure":
        raise ValueError("Dhan positions request failed.")
    positions = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(positions, list):
        raise ValueError("Unexpected positions response from Dhan.")
    if any(not isinstance(position, dict) for position in positions):
        raise ValueError("Malformed position in Dhan response.")
    result = positions
    log_event("POSITIONS_FETCHED", total_positions=len(result), active_fno_positions=sum(1 for position in result if str(position.get("exchangeSegment")) == "NSE_FNO" and float(position.get("netQty", position.get("netQuantity", 0)) or 0) != 0))
    return result


def active_strategy_position(positions: list[dict[str, Any]]) -> bool:
    """Return whether the live Dhan position payload has an active NIFTY F&O leg."""
    # Account is dedicated to this strategy: any non-zero NIFTY F&O position is active.
    return any(
        str(position.get('exchangeSegment')) == 'NSE_FNO'
        and float(position.get('netQty', position.get('netQuantity', 0)) or 0) != 0
        for position in positions
    )


def short_option_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select live Dhan F&O short option legs with their original sell average."""
    short_legs: list[dict[str, Any]] = []
    for position in positions:
        net_quantity = float(position.get("netQty", position.get("netQuantity", 0)) or 0)
        sell_average = position.get("sellAvg")
        if (
            str(position.get("exchangeSegment")) == "NSE_FNO"
            and net_quantity < 0
            and str(position.get("drvOptionType", "")).upper() in {"CALL", "PUT"}
            and position.get("securityId")
            and isinstance(sell_average, (int, float))
            and float(sell_average) > 0
        ):
            short_legs.append(position)
    return short_legs


def ltp_by_security_id(dhan: Any, positions: list[dict[str, Any]]) -> dict[str, float]:
    """Fetch current LTPs through the Dhan SDK's ticker-data method."""
    security_ids = [int(position["securityId"]) for position in positions]
    log_event("LTP_REQUEST", exchange_segment="NSE_FNO", security_ids=security_ids)
    payload = dhan.ticker_data({"NSE_FNO": security_ids})
    if not isinstance(payload, dict) or str(payload.get("status", "")).lower() == "failure":
        raise ValueError("Dhan ticker request failed.")
    data = payload.get("data", {})
    data = data.get("data", data) if isinstance(data, dict) else {}
    segment_data = data.get("NSE_FNO") if isinstance(data, dict) else None
    if not isinstance(segment_data, dict):
        raise ValueError("Unexpected Dhan ticker response.")
    prices: dict[str, float] = {}
    if isinstance(segment_data, dict):
        for security_id, quote in segment_data.items():
            if isinstance(quote, dict):
                price = quote.get("last_price")
                if isinstance(price, (int, float)):
                    prices[str(security_id)] = float(price)
            elif isinstance(quote, (int, float)):
                prices[str(security_id)] = float(quote)
    log_event("LTP_RESPONSE", response_keys=sorted(payload) if isinstance(payload, dict) else [], prices_found=list(prices))
    return prices


def spread_profit_target_reached(
    dhan: Any,
    positions: list[dict[str, Any]],
    lots: int,
    lot_size: int,
) -> tuple[bool, dict[str, Any]]:
    """Return whether running spread profit has reached 50% of maximum credit."""
    live_options = [
        position for position in positions
        if str(position.get("exchangeSegment")) == "NSE_FNO"
        and str(position.get("drvOptionType", "")).upper() in {"CALL", "PUT"}
        and float(position.get("netQty", position.get("netQuantity", 0)) or 0) != 0
    ]
    short_legs = [position for position in live_options if float(position.get("netQty", position.get("netQuantity", 0)) or 0) < 0]
    long_legs = [position for position in live_options if float(position.get("netQty", position.get("netQuantity", 0)) or 0) > 0]
    check: dict[str, Any] = {
        "lots": lots,
        "lot_size": lot_size,
        "quantity": lots * lot_size,
        "target_fraction": 0.5,
        "calculable": False,
        "target_reached": False,
    }
    if len(short_legs) != 1 or len(long_legs) != 1:
        check["reason"] = "Expected exactly one live short option and one live long option."
        return False, check

    short_leg, long_leg = short_legs[0], long_legs[0]
    sell_average = short_leg.get("sellAvg")
    buy_average = long_leg.get("buyAvg")
    if not isinstance(sell_average, (int, float)) or not isinstance(buy_average, (int, float)):
        check["reason"] = "Dhan positions are missing the sell or buy entry average."
        return False, check

    sell_average, buy_average = float(sell_average), float(buy_average)
    quantity = lots * lot_size
    maximum_profit = (sell_average - buy_average) * quantity
    if sell_average <= 0 or buy_average < 0 or maximum_profit <= 0:
        check["reason"] = "Position averages do not describe a positive-credit spread."
        return False, check

    prices = ltp_by_security_id(dhan, [short_leg, long_leg])
    short_ltp = prices.get(str(short_leg["securityId"]))
    long_ltp = prices.get(str(long_leg["securityId"]))
    check.update({
        "short_security_id": str(short_leg["securityId"]),
        "long_security_id": str(long_leg["securityId"]),
        "short_sell_average": sell_average,
        "long_buy_average": buy_average,
        "short_ltp": short_ltp,
        "long_ltp": long_ltp,
        "maximum_profit": maximum_profit,
        "profit_booking_amount": maximum_profit * 0.5,
    })
    if short_ltp is None or long_ltp is None or short_ltp <= 0 or long_ltp <= 0:
        check["reason"] = "A valid current LTP is unavailable for one or both spread legs."
        return False, check

    running_profit = ((sell_average - short_ltp) + (long_ltp - buy_average)) * quantity
    check.update({
        "running_profit": running_profit,
        "calculable": True,
        "target_reached": running_profit >= check["profit_booking_amount"],
    })
    return bool(check["target_reached"]), check

def opposite_supertrend_cross(analysis: MarketAnalysis, positions: list[dict[str, Any]]) -> bool:
    """Exit when the latest completed NIFTY candle is against the supertrend signal."""
    short_legs = short_option_positions(positions)
    option_types = {str(position["drvOptionType"]).upper() for position in short_legs}
    if option_types not in ({"PUT"}, {"CALL"}):
        return False
    latest = len(analysis.candles) - 1
    latest_supertrend = analysis.supertrend_values[latest]
    if latest_supertrend is None:
        return False

    latest_close = analysis.candles[latest]["close"]
    should_exit = (
        (option_types == {"CALL"} and latest_close > latest_supertrend)
        or (option_types == {"PUT"} and latest_close < latest_supertrend)
    )
    log_event(
        "SUPERTREND_POSITION_CHECKED",
        short_option_type=next(iter(option_types)),
        latest_close=latest_close,
        latest_supertrend=latest_supertrend,
        exit_required=should_exit,
    )
    return should_exit


def bought_option_expiring_within_two_days(positions: list[dict[str, Any]]) -> bool:
    """Return whether a live long option expires today, tomorrow, or in two days."""
    for position in positions:
        if float(position.get("netQty", position.get("netQuantity", 0)) or 0) <= 0:
            continue
        if str(position.get("exchangeSegment")) != "NSE_FNO" or not position.get("drvOptionType"):
            continue
        raw_expiry = str(position.get("drvExpiryDate", "")).split("T", 1)[0]
        try:
            days_remaining = (date.fromisoformat(raw_expiry) - datetime.now(ZoneInfo("Asia/Kolkata")).date()).days
        except ValueError:
            continue
        if 0 <= days_remaining <= 2:
            return True
    return False


def close_open_fno_positions(dhan: Any, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Submit market orders: buy to close shorts, then sell to close long hedges."""
    live_legs = [
        position for position in positions
        if str(position.get("exchangeSegment")) == "NSE_FNO"
        and float(position.get("netQty", position.get("netQuantity", 0)) or 0) != 0
        and position.get("securityId")
    ]
    short_legs = [position for position in live_legs if float(position.get("netQty", position.get("netQuantity", 0)) or 0) < 0]
    long_legs = [position for position in live_legs if float(position.get("netQty", position.get("netQuantity", 0)) or 0) > 0]
    responses: list[dict[str, Any]] = []
    for position in short_legs + long_legs:
        quantity = abs(int(float(position.get("netQty", position.get("netQuantity", 0)) or 0)))
        transaction_type = "BUY" if position in short_legs else "SELL"
        log_event("EXIT_ORDER_INTENT", security_id=str(position["securityId"]), transaction_type=transaction_type, quantity=quantity, order_type="MARKET", product_type=str(position.get("productType") or "MARGIN"))
        response = dhan.place_order(
            security_id=str(position["securityId"]),
            exchange_segment="NSE_FNO",
            transaction_type=transaction_type,
            quantity=quantity,
            order_type="MARKET",
            product_type=str(position.get("productType") or "MARGIN"),
            price=0,
        )
        log_event("EXIT_ORDER_RESPONSE", security_id=str(position["securityId"]), response=response)
        responses.append({
            "security_id": str(position["securityId"]),
            "quantity": quantity,
            "response": response,
        })
        if not isinstance(response, dict) or str(response.get("status", "")).lower() == "failure":
            raise ValueError(f"Exit order submission failed for {position['securityId']}: {response}")
    return responses


def order_details(response: dict[str, Any], order_id: str | None = None) -> dict[str, Any]:
    """Return order details from direct, SDK-wrapped, or single-item list responses."""
    value: Any = response
    for _ in range(3):
        if isinstance(value, dict):
            if any(key in value for key in ("orderStatus", "order_status", "filledQty", "filled_qty")):
                return value
            if "data" not in value:
                return value
            value = value["data"]
            continue
        if isinstance(value, list):
            candidates = [item for item in value if isinstance(item, dict)]
            if order_id is not None:
                match = next((item for item in candidates if str(item.get("orderId", item.get("order_id", ""))) == order_id), None)
                if match is not None:
                    value = match
                    continue
            if len(candidates) == 1:
                value = candidates[0]
                continue
        return {}
    return value if isinstance(value, dict) else {}


def wait_for_orders_traded(dhan: Any, orders: list[dict[str, Any]], *, phase: str) -> tuple[bool, list[dict[str, Any]]]:
    """Confirm submitted orders are fully filled before the next trading step."""
    if not orders:
        return False, []
    tracked: list[dict[str, Any]] = []
    for order in orders:
        response = order.get("response")
        payload = order_details(response) if isinstance(response, dict) else {}
        order_id = payload.get("orderId", payload.get("order_id"))
        if (not isinstance(response, dict) or str(response.get("status", "")).lower() == "failure"
                or not isinstance(order_id, (str, int)) or not str(order_id)):
            log_event(f"{phase}_ORDER_CONFIRMATION_UNAVAILABLE", security_id=order.get("security_id"), response=response)
            return False, tracked
        tracked.append({**order, "order_id": str(order_id)})

    latest_statuses: list[dict[str, Any]] = []
    for attempt in range(1, EXIT_ORDER_POLL_ATTEMPTS + 1):
        latest_statuses = []
        all_traded = True
        for order in tracked:
            try:
                status_response = dhan.get_order_by_id(order["order_id"])
            except Exception as error:
                status_response = {"status": "failure", "remarks": str(error)}
            payload = order_details(status_response, order["order_id"]) if isinstance(status_response, dict) else {}
            status = str(payload.get("orderStatus", payload.get("order_status", ""))).upper()
            filled_quantity = payload.get("filledQty", payload.get("filled_qty"))
            try:
                fully_filled = float(filled_quantity) >= float(order["quantity"])
            except (TypeError, ValueError):
                fully_filled = False
            record = {
                "security_id": order["security_id"],
                "order_id": order["order_id"],
                "order_status": status,
                "filled_quantity": filled_quantity,
                "expected_quantity": order["quantity"],
                "fully_filled": fully_filled,
                "response_status": status_response.get("status") if isinstance(status_response, dict) else None,
                "response_data_type": type(status_response.get("data")).__name__ if isinstance(status_response, dict) and "data" in status_response else type(status_response).__name__,
                "response_keys": sorted(str(key) for key in status_response) if isinstance(status_response, dict) else [],
                "payload_keys": sorted(str(key) for key in payload),
                "response_remarks": status_response.get("remarks") if isinstance(status_response, dict) else None,
            }
            latest_statuses.append(record)
            if status in {"REJECTED", "CANCELLED", "EXPIRED"}:
                log_event(f"{phase}_ORDER_NOT_FULLY_TRADED", order_statuses=latest_statuses)
                return False, latest_statuses
            if status != "TRADED" or not fully_filled:
                all_traded = False
        log_event(f"{phase}_ORDER_POLL_ATTEMPT", attempt=attempt, maximum_attempts=EXIT_ORDER_POLL_ATTEMPTS, order_statuses=latest_statuses)
        if all_traded:
            log_event(f"{phase}_ORDERS_CONFIRMED_TRADED", order_statuses=latest_statuses)
            return True, latest_statuses
        if attempt < EXIT_ORDER_POLL_ATTEMPTS:
            time.sleep(EXIT_ORDER_POLL_INTERVAL_SECONDS)
    log_event(f"{phase}_ORDER_CONFIRMATION_EXHAUSTED", attempts=EXIT_ORDER_POLL_ATTEMPTS, order_statuses=latest_statuses)
    return False, latest_statuses

def wait_for_exit_orders_traded(dhan: Any, orders: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]]]:
    """Confirm all exit orders before re-entry."""
    return wait_for_orders_traded(dhan, orders, phase="EXIT")


def wait_for_strategy_flat(dhan: Any) -> tuple[bool, list[dict[str, Any]]]:
    """Require consecutive position reads showing the account is flat."""
    deadline = time.monotonic() + EXIT_ORDER_POLL_TIMEOUT_SECONDS
    consecutive_flat = 0
    while True:
        positions = dhan_positions(dhan)
        if not active_strategy_position(positions):
            consecutive_flat += 1
            log_event("EXIT_FLAT_POSITION_OBSERVED", consecutive_confirmations=consecutive_flat, required_confirmations=EXIT_FLAT_CONFIRMATIONS_REQUIRED)
            if consecutive_flat >= EXIT_FLAT_CONFIRMATIONS_REQUIRED:
                log_event("EXIT_POSITIONS_CONFIRMED_FLAT")
                return True, positions
        else:
            consecutive_flat = 0
        # Once the first flat snapshot arrives, always allow the confirming
        # read even if it lands on the timeout boundary.
        if time.monotonic() >= deadline and consecutive_flat == 0:
            log_event("EXIT_POSITION_CONFIRMATION_TIMED_OUT", remaining_positions=positions)
            return False, positions
        log_event("EXIT_WAITING_FOR_POSITION_UPDATE")
        time.sleep(EXIT_ORDER_POLL_INTERVAL_SECONDS)


def reenter_after_exit(
    config: StrategyConfig,
    dhan: Any,
    analysis: MarketAnalysis,
    *,
    simulated_exit: bool = False,
) -> int:
    """Immediately open a fresh spread in the current Supertrend direction."""
    log_event(
        "REENTRY_STARTED",
        simulated_exit=simulated_exit,
        direction=analysis.current_direction,
    )
    if simulated_exit:
        log_event("DRY_RUN_REENTRY_STARTED", simulated_exit=True)
    return entry_check(config, dhan, analysis=analysis, reentry=True)


def exit_check(
    config: StrategyConfig,
    dhan: Any,
    positions: list[dict[str, Any]],
    *,
    analysis: MarketAnalysis | None = None,
) -> int:
    config.validate()
    analysis = analysis or analyse_market(config)
    log_event(
        "EXIT_CHECK_STARTED",
        position_count=len(positions),
        dry_run=config.dry_run,
        live_trading_enabled=config.live_trading_enabled,
    )
    target_reached, profit_check = spread_profit_target_reached(
        dhan, positions, config.nifty_quantity, config.nifty_lot_size
    )
    opposite_cross = opposite_supertrend_cross(analysis, positions)
    expiry_within_two_days = bought_option_expiring_within_two_days(positions)
    reasons: list[str] = []
    if target_reached:
        reasons.append("RUNNING_PROFIT_AT_OR_ABOVE_50_PERCENT_OF_MAXIMUM_PROFIT")
    if opposite_cross:
        reasons.append("OPPOSITE_30_MINUTE_SUPERTREND_CROSS")
    if expiry_within_two_days:
        reasons.append("LONG_OPTION_EXPIRY_WITHIN_TWO_DAYS")
    log_event("EXIT_CONDITIONS_EVALUATED", target_reached=target_reached, opposite_supertrend_cross=opposite_cross, long_option_expiry_within_two_days=expiry_within_two_days, reasons=reasons, profit_check=profit_check)
    output: dict[str, Any] = {
        "exit_required": bool(reasons),
        "reasons": reasons,
        "profit_check": profit_check,
        "current_positions": positions,
    }
    if not reasons:
        output["note"] = "No exit condition is currently met."
        print(json.dumps(output, indent=2))
        log_event("NO_EXIT_SIGNAL", **output)
        return 0
    if not config.live_trading_enabled or config.dry_run:
        output["note"] = "Dry-run exit proposed; evaluating a hypothetical re-entry without changing broker positions."
        print(json.dumps(output, indent=2))
        log_event("DRY_RUN_EXIT_SIGNAL", **output)
        return reenter_after_exit(config, dhan, analysis, simulated_exit=True)
    try:
        output["orders"] = close_open_fno_positions(dhan, positions)
        exit_confirmed, order_statuses = wait_for_exit_orders_traded(dhan, output["orders"])
        output["exit_order_statuses"] = order_statuses
        if not exit_confirmed:
            log_event("EXIT_ORDER_STATUS_INCONCLUSIVE", order_statuses=order_statuses)
        flat, remaining_positions = wait_for_strategy_flat(dhan)
    except Exception as error:
        output["order_error"] = str(error)
        print(json.dumps(output, indent=2, default=str))
        log_event("EXIT_ORDER_FAILURE", **output)
        return 1
    if not flat:
        output["remaining_positions"] = remaining_positions
        output["note"] = "Exit could not be confirmed flat after order-status and position polling; re-entry was skipped."
        print(json.dumps(output, indent=2, default=str))
        log_event("EXIT_REENTRY_SKIPPED", **output)
        return 1

    if exit_confirmed:
        output["note"] = "Exit orders filled and positions confirmed flat twice; starting immediate re-entry."
    else:
        output["note"] = "Order-status responses remained inconclusive, but positions were confirmed flat twice; starting immediate re-entry safely."
    print(json.dumps(output, indent=2, default=str))
    log_event("EXIT_CONFIRMED_REENTRY_STARTED", confirmation_source="orders_and_positions" if exit_confirmed else "positions_fallback", **output)
    return reenter_after_exit(config, dhan, analysis)




def run_strategy(config: StrategyConfig, dhan: Any, *, positions: list[dict[str, Any]] | None = None) -> int:
    """Run one cycle with one shared Supertrend analysis."""
    config.validate()
    positions = positions if positions is not None else dhan_positions(dhan)
    analysis = analyse_market(config)
    if active_strategy_position(positions):
        log_event("STRATEGY_CYCLE", action="CHECK_ACTIVE_SPREAD", active_position_count=len(positions))
        return exit_check(config, dhan, positions, analysis=analysis)
    log_event("STRATEGY_CYCLE", action="CHECK_NEW_ENTRY", active_position_count=0)
    return entry_check(config, dhan, analysis=analysis)


def main() -> int:
    """Run one scheduled strategy cycle and return a process exit status."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        from dhanhq import DhanContext, dhanhq

        config = load_config()
        dhan = dhanhq(DhanContext(config.client_id, config.access_token))
        LOGGER.info(
            "Scheduled strategy run started: dry_run=%s live_trading_enabled=%s lots=%s",
            config.dry_run,
            config.live_trading_enabled,
            config.nifty_quantity,
        )
        positions = dhan_positions(dhan)
        LOGGER.info("Dhan positions returned: %s", len(positions))
        result = run_strategy(config, dhan, positions=positions)
        LOGGER.info("Strategy cycle completed with status: %s", result)
        return result
    except Exception:
        LOGGER.exception("Scheduled strategy run failed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
