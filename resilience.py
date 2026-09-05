"""Bounded resilience controls for calls to third-party services.

Retries are deliberately opt-in for operations that are safe to repeat.  A
caller must identify a read operation or preserve an idempotency key before it
can receive an automatic retry; this prevents a timeout from becoming a double
payment, duplicate ticket, or duplicate social post.
"""

from __future__ import annotations

import email.utils
import random
import socket
import threading
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TypeVar


T = TypeVar("T")
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class CircuitOpenError(RuntimeError):
    """The provider is temporarily isolated after repeated transient failures."""


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 0.25
    max_delay: float = 5.0
    jitter_ratio: float = 0.20

    def validate(self) -> None:
        if not 0 <= self.max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        if not 0 <= self.base_delay <= self.max_delay <= 60:
            raise ValueError("retry delays must be between 0 and 60 seconds")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")


class CircuitBreaker:
    """Small thread-safe circuit breaker scoped to one provider/tenant client."""

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0,
                 *, clock: Callable[[], float] = time.monotonic) -> None:
        if not 1 <= failure_threshold <= 100:
            raise ValueError("failure_threshold must be between 1 and 100")
        if not 0 < recovery_timeout <= 3600:
            raise ValueError("recovery_timeout must be between 0 and 3600 seconds")
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def before_call(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            if self._clock() - self._opened_at >= self.recovery_timeout:
                self._opened_at = None
                self._failures = 0
                return
            raise CircuitOpenError("third-party circuit is open")

    def success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def transient_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = self._clock()

    @property
    def state(self) -> str:
        with self._lock:
            return "open" if self._opened_at is not None else "closed"


def _retry_after_seconds(error: urllib.error.HTTPError, now: datetime | None = None) -> float:
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = email.utils.parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - (now or datetime.now(timezone.utc))).total_seconds())


def _is_transient(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRYABLE_HTTP_STATUSES
    return isinstance(error, (urllib.error.URLError, socket.timeout, TimeoutError))


def execute(operation: Callable[[], T], *, retry_safe: bool,
            policy: RetryPolicy | None = None, breaker: CircuitBreaker | None = None,
            sleeper: Callable[[float], None] | None = None,
            random_value: Callable[[], float] | None = None) -> T:
    """Execute a third-party call with bounded retry and circuit isolation."""
    selected = policy or RetryPolicy()
    selected.validate()
    pause = sleeper or time.sleep
    entropy = random_value or random.random
    if breaker is not None:
        breaker.before_call()

    attempts = selected.max_retries + 1 if retry_safe else 1
    for attempt in range(attempts):
        try:
            result = operation()
        except Exception as error:
            transient = _is_transient(error)
            final = attempt + 1 >= attempts
            if not transient or final:
                if transient and breaker is not None:
                    breaker.transient_failure()
                raise
            exponential = selected.base_delay * (2 ** attempt)
            requested = _retry_after_seconds(error) if isinstance(
                error, urllib.error.HTTPError) else 0.0
            delay = max(exponential, requested)
            delay += exponential * selected.jitter_ratio * max(0.0, min(entropy(), 1.0))
            pause(min(selected.max_delay, delay))
        else:
            if breaker is not None:
                breaker.success()
            return result
    raise AssertionError("retry loop exited without a result")
