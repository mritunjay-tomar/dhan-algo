from __future__ import annotations
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo
from dhan_algo.audit import log_event
from dhan_algo.market import MarketAnalysis


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
    """Return whether the already scoped Dhan positions contain an active F&O leg."""
    # The broker applies instrument ownership before these predicates run.
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
