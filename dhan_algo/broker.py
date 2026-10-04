"""Restricted Dhan SDK facade: explicit reads, scoped positions and guarded orders."""
from typing import Any
from dhan_algo.config import StrategyConfig
from dhan_algo.positions import dhan_positions


class DhanBroker:
    def __init__(self, client: Any, config: StrategyConfig):
        self._client = client
        self._config = config

    def get_positions(self):
        positions = dhan_positions(self._client)
        scope = self._config.security_ids
        return {"status": "success", "data": [
            p for p in positions if p.get("exchangeSegment") == "NSE_FNO"
            and (scope is None or str(p.get("securityId")) in scope)
        ]}

    def ticker_data(self, securities):
        return self._client.ticker_data(securities)

    def get_order_by_id(self, order_id):
        return self._client.get_order_by_id(order_id)

    def place_order(self, **kwargs):
        if self._config.dry_run or not self._config.live_trading_enabled:
            raise RuntimeError("Order submission is blocked in dry-run/read-only mode.")
        scope = self._config.security_ids
        if kwargs.get("exchange_segment") != "NSE_FNO" or (
            scope is not None and str(kwargs.get("security_id")) not in scope
        ):
            raise ValueError("Order is outside this strategy's instrument scope.")
        return self._client.place_order(**kwargs)
