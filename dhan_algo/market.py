from __future__ import annotations
import json
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo
from dhan_algo.audit import log_event
from dataclasses import dataclass
import pandas as pd
import pandas_ta as ta
from dhan_algo.config import StrategyConfig
from dhan_algo.http import dhan_request


@dataclass(frozen=True)
class MarketAnalysis:
    """One completed-candle Supertrend calculation shared by a strategy cycle."""

    candles: list[dict[str, float]]
    supertrend_values: list[float | None]
    current_direction: str
    crossover_signal: str | None
    candle_close_time: datetime


def candle_rows(response: dict[str, Any]) -> list[dict[str, float]]:
    """Convert Dhan's column-oriented historical response into candle rows."""
    data = response.get("data", response)
    required = ("open", "high", "low", "close")
    if not isinstance(data, dict) or any(not isinstance(data.get(k), list) for k in required):
        raise ValueError(f"Unexpected historical-data response: {json.dumps(response)[:500]}")
    count = len(data["close"])
    if any(len(data[key]) != count for key in (*required, "timestamp") if key in data) or "timestamp" not in data:
        raise ValueError("Historical candle columns must have matching lengths and timestamps.")
    return [{**{key: float(data[key][i]) for key in required}, "timestamp": float(data.get("timestamp", [0] * len(data["close"]))[i])} for i in range(len(data["close"]))]


def completed_candles(candles: list[dict[str, float]], interval_minutes: int) -> list[dict[str, float]]:
    """Exclude the current unfinished native-interval candle from Dhan data."""
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return [
        candle for candle in candles
        if datetime.fromtimestamp(candle["timestamp"], tz=ZoneInfo("Asia/Kolkata")) + timedelta(minutes=interval_minutes) <= now
    ]


def resample_15m_to_30m(candles: list[dict[str, float]]) -> list[dict[str, float]]:
    """Combine completed 15-minute Dhan candles into NSE-aligned 30-minute candles."""
    ist = ZoneInfo("Asia/Kolkata")
    buckets: dict[tuple[date, int], list[dict[str, float]]] = {}
    for candle in candles:
        candle_time = datetime.fromtimestamp(candle["timestamp"], tz=ist)
        elapsed = candle_time.hour * 60 + candle_time.minute - (9 * 60 + 15)
        if elapsed >= 0:
            buckets.setdefault((candle_time.date(), elapsed // 30), []).append(candle)
    now = datetime.now(ist)
    result: list[dict[str, float]] = []
    for group in buckets.values():
        group.sort(key=lambda candle: candle["timestamp"])
        if len(group) != 2:
            continue
        first, second = group
        close_time = datetime.fromtimestamp(second["timestamp"], tz=ist) + timedelta(minutes=15)
        if second["timestamp"] - first["timestamp"] == 15 * 60 and close_time <= now:
            result.append({"open": first["open"], "high": max(first["high"], second["high"]), "low": min(first["low"], second["low"]), "close": second["close"], "timestamp": first["timestamp"]})
    return sorted(result, key=lambda candle: candle["timestamp"])


def last_market_date(today: date) -> date:
    """Return the latest weekday; weekend sessions have no NSE intraday candles."""
    market_date = today
    while market_date.weekday() >= 5:
        market_date -= timedelta(days=1)
    return market_date


def supertrend(candles: list[dict[str, float]], period: int, multiplier: float) -> list[float | None]:
    """Calculate pandas-ta Supertrend, preserving candle alignment and missing values."""
    if period < 1 or multiplier <= 0:
        raise ValueError("Supertrend period and multiplier must be positive.")
    if len(candles) < period + 2:
        raise ValueError(f"Need at least {period + 2} candles; received {len(candles)}.")

    frame = pd.DataFrame(candles)
    indicator = ta.supertrend(
        high=frame["high"],
        low=frame["low"],
        close=frame["close"],
        length=period,
        multiplier=float(multiplier),
    )
    if indicator is None or indicator.empty:
        raise ValueError("Supertrend calculation returned no values.")
    values = indicator[f"SUPERT_{period}_{float(multiplier)}"]
    return [None if pd.isna(value) else float(value) for value in values]


def cross_signal(candles: list[dict[str, float]], values: list[float | None]) -> str | None:
    prior, latest = len(candles) - 2, len(candles) - 1
    if values[prior] is None or values[latest] is None:
        return None
    if candles[prior]["close"] <= values[prior] and candles[latest]["close"] > values[latest]:
        return "BULLISH"  # put credit spread
    if candles[prior]["close"] >= values[prior] and candles[latest]["close"] < values[latest]:
        return "BEARISH"  # call credit spread
    return None


def current_direction(candles: list[dict[str, float]], values: list[float | None]) -> str:
    """Direction for re-entry, based strictly on the latest closed 30m candle."""
    latest = len(candles) - 1
    if values[latest] is None:
        raise ValueError("Latest Supertrend value is unavailable.")
    return "BULLISH" if candles[latest]["close"] > values[latest] else "BEARISH"


def analyse_market(config: StrategyConfig) -> MarketAnalysis:
    """Fetch and analyse completed NIFTY candles once for one strategy cycle."""
    config.validate()
    market_date = last_market_date(datetime.now(ZoneInfo("Asia/Kolkata")).date())
    query_end = market_date + timedelta(days=1)
    historical = dhan_request(
        "/charts/intraday",
        config.client_id,
        config.access_token,
        {
            "securityId": config.nifty_security_id,
            "exchangeSegment": config.nifty_exchange_segment,
            "instrument": config.nifty_instrument,
            "interval": 15,
            "oi": False,
            "fromDate": str(market_date - timedelta(days=config.supertrend_lookback_days)),
            "toDate": str(query_end),
        },
        config.request_timeout_seconds,
    )
    log_event(
        "INTRADAY_WINDOW_SELECTED",
        timeframe="30_MINUTE",
        latest_market_date=str(market_date),
        from_date=str(market_date - timedelta(days=config.supertrend_lookback_days)),
        to_date=str(query_end),
    )
    candles = resample_15m_to_30m(candle_rows(historical))
    values = supertrend(candles, config.supertrend_atr_period, config.supertrend_multiplier)
    latest = len(candles) - 1
    candle_close_time = datetime.fromtimestamp(candles[latest]["timestamp"], tz=ZoneInfo("Asia/Kolkata")) + timedelta(minutes=30)
    analysis = MarketAnalysis(
        candles=candles,
        supertrend_values=values,
        current_direction=current_direction(candles, values),
        crossover_signal=cross_signal(candles, values),
        candle_close_time=candle_close_time,
    )
    log_event(
        "SUPERTREND_EVALUATED",
        timeframe="30_MINUTE",
        close=candles[latest]["close"],
        supertrend=values[latest],
        current_direction=analysis.current_direction,
        crossover_signal=analysis.crossover_signal,
        candle_close_ist=candle_close_time.isoformat(),
    )
    return analysis
