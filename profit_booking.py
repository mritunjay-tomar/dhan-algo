#!/usr/bin/env python3
"""Check the fixed 50% credit-profit target and close the spread, without entry."""

import json
import logging
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import utils


def in_trading_session(now: datetime | None = None) -> bool:
    now = (now or datetime.now(ZoneInfo("Asia/Kolkata"))).astimezone(ZoneInfo("Asia/Kolkata"))
    return now.weekday() < 5 and time(9, 15) <= now.time().replace(tzinfo=None) <= time(15, 30)


def run_profit_booking(config: utils.StrategyConfig, dhan: Any) -> int:
    """Perform one profit-only cycle; partial exits are errors, never new entries."""
    config.validate()
    positions = utils.dhan_positions(dhan)
    if not utils.active_strategy_position(positions):
        utils.log_event("PROFIT_NO_ACTIVE_POSITION")
        return 0
    target_reached, check = utils.spread_profit_target_reached(
        dhan, positions, config.nifty_quantity, config.nifty_lot_size,
    )
    utils.log_event("PROFIT_TARGET_CHECKED", **check)
    if not check["calculable"]:
        utils.log_event("PROFIT_CHECK_UNAVAILABLE", **check)
        return 1
    if not target_reached:
        return 0
    if config.dry_run or not config.live_trading_enabled:
        utils.log_event("DRY_RUN_PROFIT_EXIT", profit_check=check)
        return 0
    # Once the target triggers, complete the exit even if prices subsequently move.
    # The shared executor closes and confirms the short before selling the hedge.
    orders = utils.close_open_fno_positions(dhan, positions)
    flat, remaining = utils.wait_for_strategy_flat(dhan)
    output = {"profit_check": check, "orders": orders, "remaining_positions": remaining}
    print(json.dumps(output, indent=2, default=str))
    utils.log_event("PROFIT_EXIT_CONFIRMED" if flat else "PROFIT_EXIT_INCOMPLETE", **output)
    return 0 if flat else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not in_trading_session():
        utils.log_event("PROFIT_OUTSIDE_TRADING_SESSION")
        return 0
    try:
        from dhanhq import DhanContext, dhanhq

        config = utils.load_config()
        with utils.trading_lock(config.client_id):
            return run_profit_booking(config, dhanhq(DhanContext(config.client_id, config.access_token)))
    except Exception:
        utils.LOGGER.exception("Profit-booking cycle failed; inspect order and position logs before retrying.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
