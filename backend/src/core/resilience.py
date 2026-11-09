"""
Resilience Patterns for Scalable Systems

Provides retry with exponential backoff, bulkhead pattern, and timeout handling
to ensure system stability under load and during failures.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Callable, Optional, TypeVar, Union

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Retry with Exponential Backoff
# =============================================================================


class RetryStrategy(Enum):
    """Retry strategies for different failure scenarios."""

    EXPONENTIAL = "exponential"  # 2^n * base delay
    LINEAR = "linear"  # n * base delay
    CONSTANT = "constant"  # Fixed delay


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_attempts: int = 3
    base_delay: float = 1.0  # seconds
    max_delay: float = 60.0  # seconds
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL
    jitter: bool = True  # Add randomness to prevent thundering herd
    retryable_exceptions: tuple = (Exception,)
    non_retryable_exceptions: tuple = ()  # Never retry these


@dataclass
class RetryStats:
    """Statistics for retry operations."""

    total_attempts: int = 0
    successful_attempts: int = 0
    failed_attempts: int = 0
    total_delay: float = 0.0
    last_error: Optional[str] = None


def calculate_delay(
    attempt: int,
    config: RetryConfig,
) -> float:
    """Calculate delay before next retry attempt."""
    import random

    if config.strategy == RetryStrategy.EXPONENTIAL:
        delay = config.base_delay * (2**attempt)
    elif config.strategy == RetryStrategy.LINEAR:
        delay = config.base_delay * (attempt + 1)
    else:  # CONSTANT
        delay = config.base_delay

    # Apply max delay cap
    delay = min(delay, config.max_delay)

    # Add jitter (±25%)
    if config.jitter:
        jitter_range = delay * 0.25
        delay += random.uniform(-jitter_range, jitter_range)

    return max(0, delay)


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL,
    retryable_exceptions: tuple = (Exception,),
    non_retryable_exceptions: tuple = (),
    on_retry: Optional[Callable[[int, Exception], None]] = None,
):
    """
    Decorator for retry with exponential backoff.

    Usage:
        @retry(max_attempts=3, base_delay=1.0)
        async def call_external_api():
            ...

        @retry(retryable_exceptions=(ConnectionError, TimeoutError))
        async def fetch_data():
            ...
    """
    config = RetryConfig(
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
        strategy=strategy,
        retryable_exceptions=retryable_exceptions,
        non_retryable_exceptions=non_retryable_exceptions,
    )

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(config.max_attempts):
                try:
                    return await func(*args, **kwargs)
                except config.non_retryable_exceptions as e:
                    # Never retry these
                    logger.warning(f"Non-retryable error in {func.__name__}: {e}")
                    raise
                except config.retryable_exceptions as e:
                    last_exception = e
                    is_last_attempt = attempt == config.max_attempts - 1

                    if is_last_attempt:
                        logger.error(
                            f"All {config.max_attempts} retry attempts failed "
                            f"for {func.__name__}: {e}"
                        )
                        raise

                    delay = calculate_delay(attempt, config)
                    logger.warning(
                        f"Retry {attempt + 1}/{config.max_attempts} for "
                        f"{func.__name__} after {delay:.2f}s: {e}"
                    )

                    if on_retry:
                        on_retry(attempt + 1, e)

                    await asyncio.sleep(delay)

            raise last_exception

        @wraps(func)
        def sync_wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(config.max_attempts):
                try:
                    return func(*args, **kwargs)
                except config.non_retryable_exceptions as e:
                    raise
                except config.retryable_exceptions as e:
                    last_exception = e
                    is_last_attempt = attempt == config.max_attempts - 1

                    if is_last_attempt:
                        raise

                    delay = calculate_delay(attempt, config)
                    logger.warning(
                        f"Retry {attempt + 1}/{config.max_attempts} for "
                        f"{func.__name__} after {delay:.2f}s: {e}"
                    )

                    if on_retry:
                        on_retry(attempt + 1, e)

                    time.sleep(delay)

            raise last_exception

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


# =============================================================================
# Bulkhead Pattern (Resource Isolation)
# =============================================================================


class BulkheadFullError(Exception):
    """Raised when bulkhead has no available capacity."""

    def __init__(self, name: str, max_concurrent: int):
        self.name = name
        self.max_concurrent = max_concurrent
        super().__init__(f"Bulkhead '{name}' is full (max {max_concurrent} concurrent)")


@dataclass
class BulkheadStats:
    """Statistics for bulkhead monitoring."""

    name: str
    max_concurrent: int
    current_concurrent: int = 0
    total_acquired: int = 0
    total_rejected: int = 0
    queue_size: int = 0
    avg_wait_time: float = 0.0


class Bulkhead:
    """
    Bulkhead pattern for resource isolation.

    Limits concurrent access to a resource to prevent cascade failures
    and ensure fair resource allocation.

    Usage:
        search_bulkhead = Bulkhead("search", max_concurrent=100)
        upload_bulkhead = Bulkhead("upload", max_concurrent=10)

        async with search_bulkhead.acquire():
            result = await perform_search(query)
    """

    def __init__(
        self,
        name: str,
        max_concurrent: int,
        max_queue: int = 0,  # 0 = no queueing, reject immediately
        queue_timeout: float = 30.0,
    ):
        self.name = name
        self.max_concurrent = max_concurrent
        self.max_queue = max_queue
        self.queue_timeout = queue_timeout

        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._current = 0
        self._total_acquired = 0
        self._total_rejected = 0
        self._queue_size = 0
        self._wait_times: list[float] = []
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, timeout: Optional[float] = None):
        """
        Acquire a slot in the bulkhead.

        Args:
            timeout: Optional timeout for waiting. Uses queue_timeout if None.

        Raises:
            BulkheadFullError: If bulkhead is full and cannot queue
            asyncio.TimeoutError: If timeout expires while waiting
        """
        effective_timeout = timeout or self.queue_timeout
        start_time = time.time()

        # Reserve capacity atomically: running + waiting may not exceed
        # max_concurrent + max_queue. Checking _current alone let a burst all
        # pass before any of them incremented it (it only rose after the
        # semaphore was acquired), so the queue limit was never enforced and
        # excess callers waited until timeout instead of failing fast.
        async with self._lock:
            if self._current + self._queue_size >= self.max_concurrent + self.max_queue:
                self._total_rejected += 1
                raise BulkheadFullError(self.name, self.max_concurrent)
            self._queue_size += 1

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                self._queue_size -= 1
                self._total_rejected += 1
            raise
        except BaseException:
            # Cancelled while waiting: give the reservation back.
            async with self._lock:
                self._queue_size -= 1
            raise

        async with self._lock:
            self._queue_size -= 1
            self._current += 1
            self._total_acquired += 1
            self._wait_times.append(time.time() - start_time)
            # Keep only last 100 wait times
            if len(self._wait_times) > 100:
                self._wait_times = self._wait_times[-100:]

        # The guarded body runs outside the reservation handlers above, so a
        # TimeoutError raised by the caller's own code is not miscounted as a
        # bulkhead rejection.
        try:
            yield
        finally:
            self._semaphore.release()
            async with self._lock:
                self._current -= 1

    def get_stats(self) -> BulkheadStats:
        """Get current bulkhead statistics."""
        avg_wait = (
            sum(self._wait_times) / len(self._wait_times) if self._wait_times else 0.0
        )
        return BulkheadStats(
            name=self.name,
            max_concurrent=self.max_concurrent,
            current_concurrent=self._current,
            total_acquired=self._total_acquired,
            total_rejected=self._total_rejected,
            queue_size=self._queue_size,
            avg_wait_time=avg_wait,
        )


# Global bulkheads for different resource types
bulkheads: dict[str, Bulkhead] = {}


def get_or_create_bulkhead(
    name: str,
    max_concurrent: int = 100,
    max_queue: int = 50,
) -> Bulkhead:
    """Get existing bulkhead or create new one."""
    if name not in bulkheads:
        bulkheads[name] = Bulkhead(
            name=name,
            max_concurrent=max_concurrent,
            max_queue=max_queue,
        )
    return bulkheads[name]


def with_bulkhead(
    bulkhead_name: str,
    max_concurrent: int = 100,
    timeout: Optional[float] = None,
):
    """
    Decorator to wrap function with bulkhead protection.

    Usage:
        @with_bulkhead("search", max_concurrent=100)
        async def perform_search(query):
            ...
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            bulkhead = get_or_create_bulkhead(bulkhead_name, max_concurrent)
            async with bulkhead.acquire(timeout=timeout):
                return await func(*args, **kwargs)

        return wrapper

    return decorator


# =============================================================================
# Timeout Handling
# =============================================================================


class TimeoutError(Exception):
    """Raised when operation exceeds timeout."""

    def __init__(self, operation: str, timeout: float):
        self.operation = operation
        self.timeout = timeout
        super().__init__(f"Operation '{operation}' timed out after {timeout}s")


def with_timeout(timeout: float, operation_name: Optional[str] = None):
    """
    Decorator to add timeout to async functions.

    Usage:
        @with_timeout(30.0, "external_api_call")
        async def call_api():
            ...
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            name = operation_name or func.__name__
            try:
                return await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.error(f"Timeout after {timeout}s in {name}")
                raise TimeoutError(name, timeout)

        return wrapper

    return decorator


# =============================================================================
# Combined Resilience Decorator
# =============================================================================


def resilient(
    retry_attempts: int = 3,
    retry_delay: float = 1.0,
    timeout: float = 30.0,
    bulkhead: Optional[str] = None,
    bulkhead_limit: int = 100,
    circuit_breaker: Optional[str] = None,
):
    """
    Combined resilience decorator with retry, timeout, and bulkhead.

    Usage:
        @resilient(
            retry_attempts=3,
            timeout=30.0,
            bulkhead="search",
            circuit_breaker="external_api"
        )
        async def search_documents(query):
            ...
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            # Import here to avoid circular imports
            from src.core.circuit_breaker import (
                ServiceUnavailableError,
                circuit_breakers,
            )

            # Check circuit breaker first
            if circuit_breaker:
                breaker = circuit_breakers.get(circuit_breaker)
                if breaker and not breaker.can_execute():
                    raise ServiceUnavailableError(circuit_breaker)

            async def execute():
                result = await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=timeout,
                )
                return result

            # Apply bulkhead if specified
            if bulkhead:
                bh = get_or_create_bulkhead(bulkhead, bulkhead_limit)

                async def execute_with_bulkhead():
                    async with bh.acquire():
                        return await execute()

                target = execute_with_bulkhead
            else:
                target = execute

            # Apply retry
            last_exception = None
            for attempt in range(retry_attempts):
                try:
                    result = await target()

                    # Record success in circuit breaker
                    if circuit_breaker:
                        breaker = circuit_breakers.get(circuit_breaker)
                        if breaker:
                            breaker.record_success()

                    return result
                except Exception as e:
                    last_exception = e

                    # Record failure in circuit breaker
                    if circuit_breaker:
                        breaker = circuit_breakers.get(circuit_breaker)
                        if breaker:
                            breaker.record_failure(e)

                    if attempt < retry_attempts - 1:
                        delay = calculate_delay(
                            attempt,
                            RetryConfig(base_delay=retry_delay),
                        )
                        logger.warning(
                            f"Retry {attempt + 1}/{retry_attempts} "
                            f"for {func.__name__}: {e}"
                        )
                        await asyncio.sleep(delay)
                    else:
                        raise

            raise last_exception

        return wrapper

    return decorator


# =============================================================================
# Utility Functions
# =============================================================================


def get_all_bulkhead_stats() -> dict[str, BulkheadStats]:
    """Get statistics for all bulkheads."""
    return {name: bh.get_stats() for name, bh in bulkheads.items()}


def reset_all_bulkheads() -> None:
    """Reset all bulkheads (useful for testing)."""
    bulkheads.clear()


# Pre-configured bulkheads for common use cases
def setup_default_bulkheads():
    """Setup default bulkheads for the RAG system."""
    # Search operations - high concurrency
    get_or_create_bulkhead("search", max_concurrent=100, max_queue=50)

    # Document processing - limited concurrency
    get_or_create_bulkhead("document_processing", max_concurrent=10, max_queue=100)

    # External API calls - moderate concurrency
    get_or_create_bulkhead("external_api", max_concurrent=20, max_queue=20)

    # LLM calls - limited due to rate limits
    get_or_create_bulkhead("llm", max_concurrent=5, max_queue=50)

    # Database writes - controlled for consistency
    get_or_create_bulkhead("db_write", max_concurrent=50, max_queue=100)

    logger.info("Default bulkheads configured")
