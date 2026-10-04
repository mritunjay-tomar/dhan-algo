"""Contract for independently configured strategies."""
from abc import ABC, abstractmethod
from typing import Any
from dhan_algo.config import StrategyConfig


class Strategy(ABC):
    def __init__(self, name: str, config: StrategyConfig):
        if not name.strip():
            raise ValueError("A strategy instance needs a unique name.")
        config.validate()
        self.name = name
        self.config = config

    @abstractmethod
    def run_cycle(self, broker: Any) -> int:
        """Evaluate entry/exit once. Return zero on success, nonzero on failure."""
