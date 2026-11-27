"""
QDE V1 - utils/retry.py

Exponential backoff retry decorator (Document 02 SS10, Document 07 SS15).
Used by data.exchange.ExchangeClient to make individual endpoint calls
resilient to transient failures without ever crashing a full scan
(Document 02 SS12).

Dependency level: 0 (no internal imports).
"""

from __future__ import annotations

import asyncio
import functools
import random
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


class RetryExhaustedError(Exception):
    """Raised when all retry attempts for a call have been exhausted.

    Callers (typically data.collector) are expected to catch this,
    log it, and skip the symbol rather than let it propagate
    (Document 02 SS12: one bad symbol must never stop scanning).
    """

    def __init__(self, attempts: int, last_exception: Exception) -> None:
        self.attempts = attempts
        self.last_exception = last_exception
        super().__init__(
            f"Retry exhausted after {attempts} attempts: {last_exception!r}"
        )


def async_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator applying exponential backoff to an async function.

    Delay sequence (without jitter): base_delay * 2^attempt, capped at
    max_delay. With jitter enabled, a random factor in [0.5, 1.5] is
    applied to avoid synchronized retry storms across symbols.

    Example:
        @async_retry(max_retries=3, base_delay=1.0)
        async def get_ohlcv(...): ...
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:  # noqa: BLE001 - intentional broad catch
                    last_exception = exc
                    if attempt == max_retries:
                        break
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    if jitter:
                        delay *= random.uniform(0.5, 1.5)
                    await asyncio.sleep(delay)
            assert last_exception is not None
            raise RetryExhaustedError(max_retries + 1, last_exception)

        return wrapper

    return decorator


def retry_config_from_dict(config: dict) -> dict:
    """Extract retry-relevant keys from the `rate_limit` config section
    (Document 09 SS8), so callers can do:
        @async_retry(**retry_config_from_dict(config["rate_limit"]))
    """
    return {"max_retries": config.get("max_retries", 3)}
