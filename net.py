"""
Retry with exponential backoff for transient API failures (connection resets, timeouts,
429 rate limits, 5xx). Permanent errors (4xx validation/auth) are raised immediately.
"""
from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _is_transient(exc: Exception) -> bool:
    try:
        import httpx
        if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
            return True
    except ImportError:
        pass
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)  # google.genai uses .code
    return status in RETRYABLE_STATUS


def with_retries(fn: Callable[[], T], attempts: int = 5, base_delay: float = 2.0, label: str = "") -> T:
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — filtered by _is_transient
            if not _is_transient(exc) or i == attempts - 1:
                raise
            delay = base_delay * (2 ** i) + random.uniform(0, 1)
            print(f"  transient error{f' ({label})' if label else ''}: {type(exc).__name__} "
                  f"{getattr(exc, 'status_code', '')} — retry {i + 1}/{attempts - 1} in {delay:.0f}s")
            time.sleep(delay)
    raise RuntimeError("unreachable")
