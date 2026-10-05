from __future__ import annotations
from typing import Any
from dhan_algo.audit import log_event
import time


EXIT_ORDER_POLL_INTERVAL_SECONDS = 2.0
EXIT_ORDER_POLL_TIMEOUT_SECONDS = 30.0

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
        confirmed, _ = wait_for_orders_traded(dhan, [responses[-1]], phase="EXIT")
        if not confirmed:
            raise ValueError("Exit leg not fully filled; remaining legs and re-entry blocked.")
    return responses


def order_details(response: dict[str, Any]) -> dict[str, Any]:
    """Return the Dhan order payload from either SDK response shape."""
    data = response.get("data", response) if isinstance(response, dict) else {}
    return data if isinstance(data, dict) else {}


def wait_for_orders_traded(dhan: Any, orders: list[dict[str, Any]], *, phase: str) -> tuple[bool, list[dict[str, Any]]]:
    """Confirm submitted orders are fully filled before the next trading step."""
    if not orders:
        return False, []
    tracked: list[dict[str, Any]] = []
    for order in orders:
        response = order.get("response")
        payload = order_details(response) if isinstance(response, dict) else {}
        order_id = payload.get("orderId")
        if (not isinstance(response, dict) or str(response.get("status", "")).lower() == "failure"
                or not isinstance(order_id, (str, int)) or not str(order_id)):
            log_event(f"{phase}_ORDER_CONFIRMATION_UNAVAILABLE", security_id=order.get("security_id"), response=response)
            return False, tracked
        tracked.append({**order, "order_id": str(order_id)})

    deadline = time.monotonic() + EXIT_ORDER_POLL_TIMEOUT_SECONDS
    latest_statuses: list[dict[str, Any]] = []
    while True:
        latest_statuses = []
        all_traded = True
        for order in tracked:
            status_response = dhan.get_order_by_id(order["order_id"])
            if not isinstance(status_response, dict) or str(status_response.get("status", "")).lower() == "failure":
                log_event(f"{phase}_ORDER_CONFIRMATION_UNAVAILABLE", order_id=order["order_id"], response=status_response)
                return False, latest_statuses
            payload = order_details(status_response)
            status = str(payload.get("orderStatus", "")).upper()
            filled_quantity = payload.get("filledQty")
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
            }
            latest_statuses.append(record)
            if status in {"REJECTED", "CANCELLED", "EXPIRED", "PART_TRADED"}:
                log_event(f"{phase}_ORDER_NOT_FULLY_TRADED", order_statuses=latest_statuses)
                return False, latest_statuses
            if status != "TRADED" or not fully_filled:
                all_traded = False
        if all_traded:
            log_event(f"{phase}_ORDERS_CONFIRMED_TRADED", order_statuses=latest_statuses)
            return True, latest_statuses
        if time.monotonic() >= deadline:
            log_event(f"{phase}_ORDER_CONFIRMATION_TIMED_OUT", order_statuses=latest_statuses)
            return False, latest_statuses
        time.sleep(EXIT_ORDER_POLL_INTERVAL_SECONDS)


def wait_for_exit_orders_traded(dhan: Any, orders: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]]]:
    """Confirm all exit orders before re-entry."""
    return wait_for_orders_traded(dhan, orders, phase="EXIT")
