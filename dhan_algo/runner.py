"""Load a portfolio and evaluate every strategy, isolating failures and positions."""
import json
import logging
from dataclasses import replace
from pathlib import Path
from dhan_algo.broker import DhanBroker
from dhan_algo.config import StrategyConfig
from dhan_algo.strategies.base import Strategy
from dhan_algo.strategies.credit_spread import CreditSpreadStrategy


LOGGER = logging.getLogger(__name__)
STRATEGIES: dict[str, type[Strategy]] = {"credit_spread": CreditSpreadStrategy}
# Credentials, trading flags and scopes cannot be overridden in a strategy's parameters.
PARAMETERS = {"nifty_security_id", "nifty_exchange_segment", "nifty_instrument",
              "nifty_option_underlying_security_id", "supertrend_atr_period",
              "supertrend_multiplier", "supertrend_lookback_days", "nifty_quantity", "nifty_lot_size"}


def validate_portfolio(strategies: list[Strategy]) -> None:
    if not strategies:
        raise ValueError("Configure at least one strategy.")
    names, owned = set(), set()
    for strategy in strategies:
        strategy.config.validate()
        if strategy.name in names:
            raise ValueError(f"Duplicate strategy name: {strategy.name}")
        names.add(strategy.name)
        scope = strategy.config.security_ids
        if scope is None and len(strategies) > 1:
            raise ValueError("Multiple strategies require explicit, disjoint security_ids.")
        if scope is not None:
            if not scope or owned.intersection(scope):
                raise ValueError("Strategy security_ids must be nonempty and disjoint.")
            owned.update(scope)


def load_strategies(path: Path, config: StrategyConfig) -> list[Strategy]:
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError("Strategy configuration must be a JSON list.")
    strategies = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"name", "type", "parameters", "security_ids"}:
            raise ValueError("Invalid strategy definition.")
        kind = entry.get("type")
        if kind not in STRATEGIES:
            raise ValueError(f"Unknown strategy type: {kind}")
        params = entry.get("parameters", {})
        if not isinstance(params, dict) or set(params) - PARAMETERS:
            raise ValueError("Unsupported strategy parameters.")
        ids = entry.get("security_ids")
        if ids is not None and (not isinstance(ids, list) or not ids or any(
            not isinstance(i, (str, int)) or isinstance(i, bool) or not str(i).isdigit() for i in ids
        )):
            raise ValueError("security_ids must be a nonempty list of numeric instrument IDs.")
        instance_config = replace(config, **params, security_ids=None if ids is None else frozenset(map(str, ids)))
        strategies.append(STRATEGIES[kind](entry["name"], instance_config))
    validate_portfolio(strategies)
    return strategies


def run_strategies(client, strategies: list[Strategy]) -> int:
    validate_portfolio(strategies)
    failed = False
    # Sequential execution avoids API bursts and allows fresh positions after each cycle.
    for strategy in strategies:
        try:
            status = strategy.run_cycle(DhanBroker(client, strategy.config))
            LOGGER.info("Strategy %s completed with status %s", strategy.name, status)
            failed |= status != 0
        except Exception:
            LOGGER.exception("Strategy %s failed", strategy.name)
            failed = True
    return int(failed)
