from __future__ import annotations
from typing import Any
from dhan_algo.audit import log_event
from dhan_algo.positions import active_strategy_position, dhan_positions
import time


EXIT_ORDER_POLL_INTERVAL_SECONDS = 1.0
EXIT_ORDER_POLL_ATTEMPTS = 5
EXIT_ORDER_POLL_TIMEOUT_SECONDS = 30.0
EXIT_FLAT_CONFIRMATIONS_REQUIRED = 2

def place_credit_spread_entry(dhan: Any, sell: dict[str, Any], buy: dict[str, Any], quantity: int) -> list[dict[str, Any]]:
    """Submit the protective buy, then immediately submit the short sell.

    Dhan can acknowledge an order before its status endpoint reports a fill.
    Entry therefore checks only that the buy submission was not rejected; it
    deliberately does not poll for a fill before sending the sell order.
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
        # No fill polling: only stop if Dhan outright rejects the submission,
        # so a long hedge is never sold while its short leg failed to be sent.
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
    """Require consecutive scoped position reads showing the strategy is flat."""
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
