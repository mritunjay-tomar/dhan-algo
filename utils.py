"""Shared configuration, profit rules and confirmed Dhan order execution."""

import hashlib
import json
import logging
import math
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("dhan_strategy")
EXIT_ORDER_POLL_INTERVAL_SECONDS = 1.0
EXIT_ORDER_POLL_ATTEMPTS = 5
EXIT_ORDER_POLL_TIMEOUT_SECONDS = 30.0
EXIT_FLAT_CONFIRMATIONS_REQUIRED = 2
EXIT_ORDER_MAX_ATTEMPTS = 3


@contextmanager
def trading_lock(client_id: str):
    """Serialize both executables on the runner, including manual invocations."""
    suffix = hashlib.sha256(client_id.encode()).hexdigest()[:16]
    path = os.path.join(tempfile.gettempdir(), f"dhan-trading-{suffix}.lock")
    with open(path, "a+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def log_event(event: str, **details: Any) -> None:
    """Emit one structured, credential-free audit record to Cloud logs."""
    record = {"timestamp_ist": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(), "event": event, **details}
    LOGGER.info("%s", json.dumps(record, separators=(",", ":"), default=str))


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
    short_quantity = abs(float(short_leg.get("netQty", short_leg.get("netQuantity", 0)) or 0))
    long_quantity = float(long_leg.get("netQty", long_leg.get("netQuantity", 0)) or 0)
    if short_quantity != long_quantity or not short_quantity.is_integer():
        check["reason"] = "Expected matching whole-unit spread quantities."
        return False, check
    if any(short_leg.get(key) is not None and long_leg.get(key) is not None and short_leg[key] != long_leg[key] for key in ("drvExpiryDate", "drvOptionType", "productType")):
        check["reason"] = "Spread legs must have matching expiry, option type and product."
        return False, check
    quantity = int(short_quantity)
    check["quantity"] = quantity
    maximum_profit = (sell_average - buy_average) * quantity
    if not all(math.isfinite(value) for value in (sell_average, buy_average, maximum_profit)) or sell_average <= 0 or buy_average < 0 or maximum_profit <= 0:
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
    if short_ltp is None or long_ltp is None or not all(math.isfinite(value) for value in (short_ltp, long_ltp)) or short_ltp <= 0 or long_ltp <= 0:
        check["reason"] = "A valid current LTP is unavailable for one or both spread legs."
        return False, check

    running_profit = ((sell_average - short_ltp) + (long_ltp - buy_average)) * quantity
    check.update({
        "running_profit": running_profit,
        "calculable": True,
        "target_reached": running_profit >= check["profit_booking_amount"],
    })
    return bool(check["target_reached"]), check


def net_quantity(position: dict[str, Any]) -> int:
    value = float(position.get("netQty", position.get("netQuantity", 0)) or 0)
    if not math.isfinite(value) or not value.is_integer():
        raise ValueError("Position quantity must be a finite whole number.")
    return int(value)


def ensure_no_pending_orders(dhan: Any) -> None:
    """Prevent another cycle from duplicating an order whose result is unknown.

    This account is dedicated to the strategy. With no persistent recovery
    state, the broker order book is the source of truth between invocations.
    """
    response = dhan.get_order_list()
    if isinstance(response, dict) and str(response.get("status", "")).lower() == "failure":
        raise ValueError("Could not verify outstanding Dhan orders.")
    orders = response.get("data") if isinstance(response, dict) else response
    if not isinstance(orders, list) or any(not isinstance(order, dict) for order in orders):
        raise ValueError("Unexpected Dhan order-book response.")
    for order in orders:
        if order.get("exchangeSegment") != "NSE_FNO":
            continue
        if str(order.get("orderStatus", "")).upper() not in {"TRADED", "REJECTED", "CANCELLED", "EXPIRED"}:
            raise ValueError(f"Outstanding F&O order {order.get('orderId')} must be resolved before new orders.")


def wait_for_leg_quantity(dhan: Any, security_id: str, maximum_quantity: int) -> list[dict[str, Any]]:
    """Wait for fills to reach positions before making a dependent submission."""
    for attempt in range(EXIT_ORDER_POLL_ATTEMPTS):
        positions = dhan_positions(dhan)
        remaining = sum(abs(net_quantity(p)) for p in positions
                        if str(p.get("securityId")) == security_id
                        and p.get("exchangeSegment") == "NSE_FNO")
        if remaining <= maximum_quantity:
            return positions
        log_event("EXIT_WAITING_FOR_LEG_POSITION", security_id=security_id,
                  remaining_quantity=remaining, maximum_quantity=maximum_quantity)
        if attempt + 1 < EXIT_ORDER_POLL_ATTEMPTS:
            time.sleep(EXIT_ORDER_POLL_INTERVAL_SECONDS)
    raise ValueError(f"Positions have not confirmed the fill for {security_id}; no replacement submitted.")


def retryable_exit_rejection(record: dict[str, Any]) -> bool:
    """Retry known hedge-propagation or temporary failures, preserving order type."""
    reason = str(record.get("oms_error_description") or record.get("response_remarks") or "").lower()
    return record.get("order_status") == "REJECTED" and any(
        text in reason for text in ("hedge", "temporar", "try again", "rate limit", "too many requests", "service unavailable")
    )


def close_open_fno_positions(dhan: Any, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Close shorts completely before releasing their protective long options.

    Dhan rejects selling a hedge while its protected short is still open.
    Waiting for both the short fill and its position update avoids that rejection.
    """
    ensure_no_pending_orders(dhan)
    live_legs = [p for p in positions if p.get("exchangeSegment") == "NSE_FNO" and net_quantity(p)]
    short_legs = [p for p in live_legs if net_quantity(p) < 0]
    long_legs = [p for p in live_legs if net_quantity(p) > 0]
    orders: list[dict[str, Any]] = []
    for position in short_legs + long_legs:
        security_id = str(position["securityId"])
        transaction_type = "BUY" if net_quantity(position) < 0 else "SELL"
        quantity = abs(net_quantity(position))
        if transaction_type == "SELL":
            # Fill status can arrive before the broker's hedge restriction clears.
            snapshot = dhan_positions(dhan)
            if any(net_quantity(p) < 0 for p in snapshot if p.get("exchangeSegment") == "NSE_FNO"):
                raise ValueError("Short positions are still open; retaining the hedge.")
            quantity = sum(max(0, net_quantity(p)) for p in snapshot
                           if str(p.get("securityId")) == security_id and p.get("exchangeSegment") == "NSE_FNO")
        for attempt in range(1, EXIT_ORDER_MAX_ATTEMPTS + 1):
            if quantity == 0:
                break
            ensure_no_pending_orders(dhan)
            log_event("EXIT_ORDER_INTENT", security_id=security_id, transaction_type=transaction_type,
                      quantity=quantity, order_type="MARKET", attempt=attempt,
                      product_type=position.get("productType") or "MARGIN")
            response = dhan.place_order(
                security_id=security_id, exchange_segment="NSE_FNO",
                transaction_type=transaction_type, quantity=quantity, order_type="MARKET",
                product_type=position.get("productType") or "MARGIN", price=0,
            )
            log_event("EXIT_ORDER_RESPONSE", security_id=security_id, response=response)
            order = {"security_id": security_id, "quantity": quantity, "response": response}
            orders.append(order)
            confirmed, statuses = wait_for_exit_orders_traded(dhan, [order])
            if confirmed:
                wait_for_leg_quantity(dhan, security_id, 0)
                break
            record = statuses[-1] if statuses else {}
            log_event("EXIT_ORDER_UNRESOLVED", attempt=attempt, **record)
            if not retryable_exit_rejection(record) or attempt == EXIT_ORDER_MAX_ATTEMPTS:
                # Pending, partially filled and unknown results must not be duplicated.
                raise ValueError(f"Exit order not confirmed for {security_id}: {statuses}")
            filled = float(record.get("filled_quantity") or 0)
            if not math.isfinite(filled) or not filled.is_integer() or not 0 <= filled <= quantity:
                raise ValueError("Invalid filled quantity; refusing a replacement order.")
            snapshot = wait_for_leg_quantity(dhan, security_id, quantity - int(filled))
            remaining = [net_quantity(p) for p in snapshot if str(p.get("securityId")) == security_id
                         and p.get("exchangeSegment") == "NSE_FNO"]
            if any(q > 0 if transaction_type == "BUY" else q < 0 for q in remaining):
                raise ValueError("Position direction changed; refusing a replacement order.")
            if transaction_type == "SELL" and any(net_quantity(p) < 0 for p in snapshot if p.get("exchangeSegment") == "NSE_FNO"):
                raise ValueError("Short positions are still open; retaining the hedge.")
            quantity = sum(abs(q) for q in remaining)
            log_event("EXIT_ORDER_RETRY", security_id=security_id, remaining_quantity=quantity,
                      reason=record.get("oms_error_description"), next_attempt=attempt + 1)
            time.sleep(EXIT_ORDER_POLL_INTERVAL_SECONDS)
    return orders


def order_details(response: dict[str, Any], order_id: str | None = None) -> dict[str, Any]:
    """Return order details from direct, SDK-wrapped, or single-item list responses."""
    value: Any = response
    for _ in range(3):
        if isinstance(value, dict):
            if any(key in value for key in ("orderStatus", "order_status", "filledQty", "filled_qty")):
                returned_id = value.get("orderId", value.get("order_id"))
                if order_id is not None and returned_id is not None and str(returned_id) != order_id:
                    return {}
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
            if len(candidates) == 1 and order_id is None:
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
                filled = float(filled_quantity)
                fully_filled = math.isfinite(filled) and filled == float(order["quantity"])
            except (TypeError, ValueError):
                fully_filled = False
            if isinstance(status_response, dict) and str(status_response.get("status", "")).lower() == "failure":
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
                "oms_error_code": payload.get("omsErrorCode"),
                "oms_error_description": payload.get("omsErrorDescription"),
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

