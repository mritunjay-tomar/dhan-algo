from __future__ import annotations
import json
from typing import Any
from dhan_algo.audit import log_event
import ssl
import certifi
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.dhan.co/v2"

def dhan_request(path: str, client_id: str, token: str, body: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    log_event("DHAN_HTTP_REQUEST", path=path, body=body, timeout_seconds=timeout_seconds, header_names=["Accept", "Content-Type", "access-token", "client-id"])
    request = Request(
        f"{API_BASE_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json", "access-token": token, "client-id": client_id},
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(request, timeout=timeout_seconds, context=context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    candle_count = len(data.get("close", [])) if path in {"/charts/intraday", "/charts/historical"} and isinstance(data, dict) and isinstance(data.get("close"), list) else None
    response_details = {key: value for key, value in payload.items() if key in {"status", "message", "errorCode", "errorMessage"}} if isinstance(payload, dict) else {}
    log_event("DHAN_HTTP_RESPONSE", path=path, response_type=type(payload).__name__, response_keys=sorted(payload) if isinstance(payload, dict) else [], candle_count=candle_count, response_details=response_details)
    return payload
