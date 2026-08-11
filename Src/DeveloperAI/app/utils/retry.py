"""Async retry-with-exponential-backoff helper for outbound MCP calls."""

import logging
from typing import Any, Callable, Coroutine, TypeVar

from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

T = TypeVar("T")

_INITIAL_WAIT_SECONDS = 1.0
_MAX_WAIT_SECONDS = 10.0


def _log_before_retry(logger: logging.Logger, operation: str) -> Callable[[RetryCallState], None]:
    def _callback(state: RetryCallState) -> None:
        exception = state.outcome.exception() if state.outcome else None
        logger.warning(
            "Retrying %s (attempt %d) after error: %s",
            operation,
            state.attempt_number,
            exception,
            extra={"extra_fields": {"operation": operation, "attempt": state.attempt_number}},
        )

    return _callback


async def call_with_retry(
    func: Callable[[], Coroutine[Any, Any, T]],
    *,
    operation: str,
    max_attempts: int,
    retry_on: tuple[type[BaseException], ...],
    logger: logging.Logger,
) -> T:
    """Invoke ``func`` with exponential backoff retry on the given exception types."""

    retrying = AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=_INITIAL_WAIT_SECONDS, max=_MAX_WAIT_SECONDS),
        retry=retry_if_exception_type(retry_on),
        before_sleep=_log_before_retry(logger, operation),
        reraise=True,
    )
    return await retrying(func)
