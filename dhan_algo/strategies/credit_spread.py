from __future__ import annotations
import json
import logging
from typing import Any
from dhan_algo.audit import log_event
from urllib.error import HTTPError, URLError
from dhan_algo.config import StrategyConfig
from dhan_algo.http import dhan_request
from dhan_algo.market import MarketAnalysis, analyse_market
from dhan_algo.options import next_expiry, option_legs, spread_payoff
from dhan_algo.positions import (dhan_positions, active_strategy_position, spread_profit_target_reached,
                                 opposite_supertrend_cross, bought_option_expiring_within_two_days)
from dhan_algo.execution import place_credit_spread_entry, close_open_fno_positions
from dhan_algo.strategies.base import Strategy


LOGGER = logging.getLogger(__name__)


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
            return 0

        expiry_request = {"UnderlyingScrip": config.nifty_option_underlying_security_id, "UnderlyingSeg": segment}
        expiry = next_expiry(dhan_request("/optionchain/expirylist", client_id, token, expiry_request, config.request_timeout_seconds))
        chain = dhan_request("/optionchain", client_id, token, {**expiry_request, "Expiry": expiry}, config.request_timeout_seconds)
        if config.security_ids is not None:
            chain = {"data": {"oc": {
                strike: {side: leg for side, leg in legs.items()
                         if isinstance(leg, dict) and str(leg.get("security_id")) in config.security_ids}
                for strike, legs in chain.get("data", {}).get("oc", {}).items()
            }}}
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
        log_event("ENTRY_ORDERS_SUBMITTED", signal=signal, expiry=expiry, **output)
        return 0
    except HTTPError as error:
        LOGGER.error("DhanHQ HTTP %s: %s", error.code, error.read().decode("utf-8", errors="replace"))
        log_event("ENTRY_CHECK_FAILED", error_type="HTTPError", status_code=error.code)
    except (URLError, ValueError, json.JSONDecodeError) as error:
        LOGGER.error("Entry scan failed: %s", error)
        log_event("ENTRY_CHECK_FAILED", error_type=type(error).__name__, error=str(error))
    return 1


def exit_check(
    config: StrategyConfig,
    dhan: Any,
    positions: list[dict[str, Any]],
    *,
    analysis: MarketAnalysis | None = None,
) -> int:
    config.validate()
    analysis = analysis or analyse_market(config)
    log_event("EXIT_CHECK_STARTED", position_count=len(positions), dry_run=config.dry_run, live_trading_enabled=config.live_trading_enabled)
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
        output["note"] = "Exit condition met, but live trading is disabled or dry run is enabled."
        print(json.dumps(output, indent=2))
        log_event("DRY_RUN_EXIT_SIGNAL", **output)
        return 0
    try:
        output["orders"] = close_open_fno_positions(dhan, positions)
    except Exception as error:
        output["order_error"] = str(error)
        print(json.dumps(output, indent=2, default=str))
        log_event("EXIT_ORDER_FAILURE", **output)
        return 1
    remaining_positions = dhan_positions(dhan)
    if active_strategy_position(remaining_positions):
        output["remaining_positions"] = remaining_positions
        output["note"] = "Exit market orders submitted; positions not yet flat, re-entry deferred to next cycle."
        print(json.dumps(output, indent=2, default=str))
        log_event("EXIT_SUBMITTED_REENTRY_DEFERRED", **output)
        return 0

    output["note"] = "Exit market orders submitted and positions are flat; starting immediate re-entry."
    print(json.dumps(output, indent=2, default=str))
    log_event("EXIT_CONFIRMED_REENTRY_STARTED", **output)
    return entry_check(config, dhan, analysis=analysis, reentry=True)


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


class CreditSpreadStrategy(Strategy):
    """NIFTY Supertrend credit spread, evaluated once per scheduled cycle."""

    def run_cycle(self, broker: Any) -> int:
        return run_strategy(self.config, broker)
