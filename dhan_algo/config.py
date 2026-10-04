from __future__ import annotations
import math
from dataclasses import dataclass, field
import os
from dhan_algo.auth import generate_access_token


@dataclass(frozen=True)
class StrategyConfig:
    """All runtime inputs needed for one strategy cycle.

    Authentication supplies a token at runtime. The strategy deliberately does not read or mutate the
    process environment.
    """

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
    security_ids: frozenset[str] | None = None

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
        raise ValueError(f"{name} must contain a value, not a Dhan Cloud placeholder.")
    return value

def env_bool(name: str, default: bool) -> bool:
    value = env_value(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false.")
    return value == "true"

def load_config() -> StrategyConfig:
    """Read credentials and settings without cloud template substitution."""
    client_id = env_value("CLIENT_ID")
    timeout_seconds = float(env_value("DHAN_REQUEST_TIMEOUT_SECONDS", "15"))
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("DHAN_REQUEST_TIMEOUT_SECONDS must be positive and finite.")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN") or generate_access_token(
        client_id=client_id,
        pin=env_value("DHAN_PIN"),
        totp_secret=env_value("DHAN_TOTP_SECRET"),
        timeout_seconds=timeout_seconds,
    )
    config = StrategyConfig(
        client_id=client_id,
        access_token=access_token,
        # Preserve the identifiers used by the existing Cloud scripts.
        nifty_security_id=int(env_value("NIFTY_SECURITY_ID", "15")),
        nifty_exchange_segment=env_value("NIFTY_EXCHANGE_SEGMENT", "IDX_I"),
        nifty_instrument=env_value("NIFTY_INSTRUMENT", "INDEX"),
        nifty_option_underlying_security_id=int(env_value("NIFTY_OPTION_UNDERLYING_SECURITY_ID", "13")),
        # Configured lot count and units per lot; the strategy multiplies them.
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
