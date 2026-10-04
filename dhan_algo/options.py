from __future__ import annotations
from typing import Any


def next_expiry(expiry_response: dict[str, Any]) -> str:
    expiries = sorted(expiry_response.get("data", []))
    if len(expiries) < 2:
        raise ValueError(f"Need at least two active expiries to select next week; received {expiries!r}")
    return expiries[1]


def option_legs(
    chain_response: dict[str, Any],
    signal: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return sell Δ0.50 and protective buy whose premium is ~50% of sell premium."""

    if signal not in {"BULLISH", "BEARISH"}:
        raise ValueError("Unknown Supertrend direction.")
    option_chain = chain_response.get("data", {}).get("oc", {})

    side, target_sell = (
        ("pe", -0.50)
        if signal == "BULLISH"
        else ("ce", 0.50)
    )

    candidates: list[dict[str, Any]] = []

    for strike, legs in option_chain.items():
        leg = legs.get(side) if isinstance(legs, dict) else None
        greeks = leg.get("greeks", {}) if isinstance(leg, dict) else {}
        delta = greeks.get("delta")
        last_price = leg.get("last_price") if isinstance(leg, dict) else None

        if (
            isinstance(delta, (int, float))
            and isinstance(last_price, (int, float))
            and last_price > 0
            and leg.get("security_id")
        ):
            candidates.append({
                "strike": float(strike),
                "delta": float(delta),
                "security_id": str(leg["security_id"]),
                "last_price": float(last_price),
                "top_bid_price": leg.get("top_bid_price"),
                "top_ask_price": leg.get("top_ask_price"),
            })

    if not candidates:
        raise ValueError(
            "No option contracts with delta/security_id/last_price were returned by Dhan."
        )

    # Sell leg: closest to delta 0.50
    sell = min(
        candidates,
        key=lambda item: abs(item["delta"] - target_sell)
    )

    # Buy leg target premium = 50% of sell premium
    target_buy_price = sell["last_price"] * 0.50

    # Buy leg: premium closest to half of sell premium
    protective = [leg for leg in candidates if
                  (signal == "BULLISH" and leg["strike"] < sell["strike"])
                  or (signal == "BEARISH" and leg["strike"] > sell["strike"])]
    if not protective:
        raise ValueError("No out-of-the-money protective contract is available.")
    buy = min(
        protective,
        key=lambda item: abs(item["last_price"] - target_buy_price)
    )

    if sell["security_id"] == buy["security_id"]:
        raise ValueError(
            "Sell and buy selections resolved to the same contract; no spread proposed."
        )

    return sell, buy


def spread_payoff(sell: dict[str, Any], buy: dict[str, Any], quantity: int) -> dict[str, float]:
    """Conservative credit-spread economics using sell bid and buy ask."""
    sell_bid, buy_ask = sell.get("top_bid_price"), buy.get("top_ask_price")
    if not isinstance(sell_bid, (int, float)) or not isinstance(buy_ask, (int, float)):
        raise ValueError("Option-chain response is missing executable bid/ask prices.")
    credit = float(sell_bid) - float(buy_ask)
    width = abs(float(sell["strike"]) - float(buy["strike"]))
    if credit <= 0:
        raise ValueError(f"Selected legs do not form a credit spread at current bid/ask (credit={credit:.2f}).")
    if credit >= width:
        raise ValueError("Credit is not smaller than spread width; refusing invalid payoff calculation.")
    return {"net_credit_per_unit": credit, "maximum_profit": credit * quantity, "maximum_loss": (width - credit) * quantity, "profit_booking_debit_per_unit": credit / 2, "profit_booking_amount": (credit / 2) * quantity, "spread_width": width}
