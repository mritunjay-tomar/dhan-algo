import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger(__name__)

def log_event(event: str, **details: Any) -> None:
    """Emit one structured, credential-free audit record to Cloud logs."""
    record = {"timestamp_ist": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(), "event": event, **details}
    LOGGER.info("%s", json.dumps(record, separators=(",", ":"), default=str))
