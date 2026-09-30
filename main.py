#!/usr/bin/env python3
"""Run one Dhan strategy cycle using ordinary environment variables."""

from __future__ import annotations

import json
import logging
import os
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import certifi
import pyotp
from dhanhq import DhanContext, dhanhq

import strategy


LOGGER = logging.getLogger("dhan_runner")
TOKEN_URL = "https://auth.dhan.co/app/generateAccessToken"


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


def generate_access_token(client_id: str, pin: str, totp_secret: str, timeout_seconds: float) -> str:
    """Generate a fresh 24-hour Dhan token using the account's TOTP setup."""
    if len(pin) != 6 or not pin.isdigit():
        raise ValueError("DHAN_PIN must be the 6-digit numeric Dhan PIN.")

    try:
        totp = pyotp.TOTP(totp_secret.replace(" ", "")).now()
    except Exception as error:
        raise ValueError("DHAN_TOTP_SECRET is not a valid TOTP secret.") from error

    query = urlencode({"dhanClientId": client_id, "pin": pin, "totp": totp})
    request = Request(f"{TOKEN_URL}?{query}", method="POST")
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urlopen(request, timeout=timeout_seconds, context=context) as response:
            payload: Any = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        # Do not include error.url: it contains the PIN and one-time password.
        body = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Dhan token generation failed with HTTP {error.code}: {body}") from None
    except URLError as error:
        raise RuntimeError(f"Could not reach Dhan authentication service: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("Dhan authentication returned an invalid JSON response.") from error

    token = payload.get("accessToken") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip():
        message = payload.get("message") if isinstance(payload, dict) else None
        suffix = f": {message}" if isinstance(message, str) else ""
        raise RuntimeError(f"Dhan authentication response did not contain an access token{suffix}.")

    LOGGER.info("Generated a fresh Dhan access token; expiry: %s", payload.get("expiryTime", "not supplied"))
    return token.strip()


def load_config() -> strategy.StrategyConfig:
    """Read credentials and settings without cloud template substitution."""
    client_id = env_value("CLIENT_ID")
    timeout_seconds = float(env_value("DHAN_REQUEST_TIMEOUT_SECONDS", "15"))
    access_token = generate_access_token(
        client_id=client_id,
        pin=env_value("DHAN_PIN"),
        totp_secret=env_value("DHAN_TOTP_SECRET"),
        timeout_seconds=timeout_seconds,
    )
    config = strategy.StrategyConfig(
        client_id=client_id,
        access_token=access_token,
        # Preserve the identifiers used by the existing Cloud scripts.
        nifty_security_id=int(env_value("NIFTY_SECURITY_ID", "15")),
        nifty_exchange_segment=env_value("NIFTY_EXCHANGE_SEGMENT", "IDX_I"),
        nifty_instrument=env_value("NIFTY_INSTRUMENT", "INDEX"),
        nifty_option_underlying_security_id=int(env_value("NIFTY_OPTION_UNDERLYING_SECURITY_ID", "13")),
        nifty_quantity=int(env_value("NIFTY_QUANTITY", "1")),
        supertrend_atr_period=int(env_value("SUPERTREND_ATR_PERIOD", "22")),
        supertrend_multiplier=float(env_value("SUPERTREND_MULTIPLIER", "4")),
        supertrend_lookback_days=int(env_value("SUPERTREND_LOOKBACK_DAYS", "60")),
        request_timeout_seconds=timeout_seconds,
        dry_run=env_bool("DRY_RUN", True),
        live_trading_enabled=env_bool("LIVE_TRADING_ENABLED", False),
    )
    config.validate()
    return config


def run_cycle(dhan: dhanhq, config: strategy.StrategyConfig) -> int:
    LOGGER.info("Cycle started: fetching current Dhan positions.")
    positions = strategy.dhan_positions(dhan)
    LOGGER.info("Dhan authentication/data check succeeded; positions returned: %s", len(positions))
    result = strategy.run_strategy(config, dhan, positions=positions)
    LOGGER.info("Cycle completed with status: %s", result)
    return result


def main() -> int:
    """Run once and return an exit status for the scheduler."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config()
        dhan = dhanhq(DhanContext(config.client_id, config.access_token))
        LOGGER.info(
            "Scheduled strategy run started: dry_run=%s live_trading_enabled=%s quantity=%s",
            config.dry_run, config.live_trading_enabled, config.nifty_quantity,
        )
        return run_cycle(dhan, config)
    except Exception:
        LOGGER.exception("Scheduled strategy run failed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
