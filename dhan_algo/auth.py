"""Runtime authentication; tokens expire and are never a one-time setup."""
import json
import logging
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import certifi
import pyotp


LOGGER = logging.getLogger(__name__)
TOKEN_URL = "https://auth.dhan.co/app/generateAccessToken"

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
        raise RuntimeError(f"Dhan token generation failed with HTTP {error.code}.") from None
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
