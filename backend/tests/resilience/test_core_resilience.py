"""Resilience primitives: retry, circuit breaker, bulkhead and timeout.

Exercises the real src.core.resilience and src.core.circuit_breaker (used by
the knowledge-graph service, Cohere embed/rerank and LLM entity extraction).
Everything under tests/resilience/ is auto-marked ``resilience`` by
conftest.py, which is what the CI "Resilience Tests" job selects; until this
suite existed that job collected zero tests and still reported green.

Started from tests/templates/test_resilience_template.py.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.circuit_breaker import (
    CircuitState,
    ServiceCircuitBreaker,
    ServiceUnavailableError,
    with_circuit_breaker,
)
from src.core.resilience import (
    Bulkhead,
    BulkheadFullError,
    RetryConfig,
    RetryStrategy,
    TimeoutError,
    get_or_create_bulkhead,
    reset_all_bulkheads,
    retry,
    with_bulkhead,
    with_timeout,
)

# =============================================================================
# Test Configuration
# =============================================================================


class ResilienceTestConfig:
    """Configuration for resilience tests."""

    # Retry settings
    MAX_RETRY_ATTEMPTS = 3
    RETRY_BASE_DELAY = 0.1  # Fast for testing
    RETRY_MAX_DELAY = 1.0

    # Circuit breaker settings
    CB_FAILURE_THRESHOLD = 3
    CB_RECOVERY_TIMEOUT = 1.0  # Fast for testing

    # Bulkhead settings
    BH_MAX_CONCURRENT = 5
    BH_MAX_QUEUE = 3

    # Timeout settings
    OPERATION_TIMEOUT = 1.0


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def circuit_breaker() -> Any:
    """Create a circuit breaker for testing."""
    cb = ServiceCircuitBreaker(
        "test_service",
        failure_threshold=ResilienceTestConfig.CB_FAILURE_THRESHOLD,
        recovery_timeout=ResilienceTestConfig.CB_RECOVERY_TIMEOUT,
    )
    yield cb
    cb.reset()


@pytest.fixture
def bulkhead() -> Any:
    """Create a bulkhead for testing."""
    reset_all_bulkheads()
    bh = get_or_create_bulkhead(
        "test_bulkhead",
        max_concurrent=ResilienceTestConfig.BH_MAX_CONCURRENT,
        max_queue=ResilienceTestConfig.BH_MAX_QUEUE,
    )
    yield bh
    reset_all_bulkheads()


@pytest.fixture
def failing_service() -> Any:
    """Mock service that fails N times then succeeds."""

    def create(fail_count: int = 2, error_type: type = ConnectionError) -> Any:
        call_count = 0

        async def mock_call(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count <= fail_count:
                raise error_type(f"Simulated failure {call_count}")
            return {"success": True, "attempt": call_count}

        return mock_call, lambda: call_count

    return create


@pytest.fixture
def slow_service() -> Any:
    """Mock service that takes a configurable time."""

    def create(delay: float = 2.0) -> Any:
        async def mock_call(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(delay)
            return {"success": True}

        return mock_call

    return create


# =============================================================================
# Retry Pattern Tests
# =============================================================================


@pytest.mark.resilience
class TestRetryPattern:
    """Tests for retry with exponential backoff."""

    @pytest.mark.asyncio
    async def test_retry_succeeds_after_transient_failures(
        self, failing_service: Any
    ) -> None:
        """Retry succeeds when failures are transient."""
        mock_call, get_count = failing_service(fail_count=2)

        @retry(
            max_attempts=ResilienceTestConfig.MAX_RETRY_ATTEMPTS,
            base_delay=ResilienceTestConfig.RETRY_BASE_DELAY,
        )
        async def operation() -> Any:
            return await mock_call()

        result = await operation()

        assert result["success"] is True
        assert get_count() == 3  # 2 failures + 1 success

    @pytest.mark.asyncio
    async def test_retry_exhausts_attempts(self, failing_service: Any) -> None:
        """Retry gives up after max attempts."""
        mock_call, get_count = failing_service(fail_count=10)  # More than max

        @retry(
            max_attempts=ResilienceTestConfig.MAX_RETRY_ATTEMPTS,
            base_delay=ResilienceTestConfig.RETRY_BASE_DELAY,
        )
        async def operation() -> Any:
            return await mock_call()

        with pytest.raises(ConnectionError):
            await operation()

        assert get_count() == ResilienceTestConfig.MAX_RETRY_ATTEMPTS

    @pytest.mark.asyncio
    async def test_retry_respects_non_retryable_exceptions(
        self, failing_service: Any
    ) -> None:
        """Non-retryable exceptions are not retried."""
        mock_call, get_count = failing_service(fail_count=1, error_type=ValueError)

        @retry(
            max_attempts=3,
            non_retryable_exceptions=(ValueError,),
        )
        async def operation() -> Any:
            return await mock_call()

        with pytest.raises(ValueError):
            await operation()

        assert get_count() == 1  # No retry

    @pytest.mark.asyncio
    async def test_retry_exponential_backoff(self) -> None:
        """Verify exponential backoff timing."""
        delays = []

        @retry(max_attempts=4, base_delay=0.1, strategy=RetryStrategy.EXPONENTIAL)
        async def operation() -> None:
            delays.append(datetime.utcnow())
            raise ConnectionError("fail")

        with pytest.raises(ConnectionError):
            await operation()

        # Check delay progression (approximately exponential)
        if len(delays) >= 3:
            delay1 = (delays[1] - delays[0]).total_seconds()
            delay2 = (delays[2] - delays[1]).total_seconds()
            # Second delay should be roughly 2x first (with jitter)
            assert delay2 > delay1 * 1.5  # Allow for jitter

    @pytest.mark.asyncio
    async def test_retry_callback_invoked(self, failing_service: Any) -> None:
        """Retry callback is invoked on each retry."""
        mock_call, _ = failing_service(fail_count=2)
        retry_events = []

        def on_retry(attempt: Any, error: Any) -> None:
            retry_events.append((attempt, str(error)))

        @retry(
            max_attempts=3,
            base_delay=0.01,
            on_retry=on_retry,
        )
        async def operation() -> Any:
            return await mock_call()

        await operation()

        assert len(retry_events) == 2
        assert retry_events[0][0] == 1
        assert retry_events[1][0] == 2


# =============================================================================
# Circuit Breaker Tests
# =============================================================================


@pytest.mark.resilience
class TestCircuitBreakerPattern:
    """Tests for circuit breaker pattern."""

    def test_circuit_starts_closed(self, circuit_breaker: Any) -> None:
        """Circuit breaker starts in CLOSED state."""
        assert circuit_breaker.state == CircuitState.CLOSED
        assert circuit_breaker.can_execute() is True

    def test_circuit_opens_after_threshold(self, circuit_breaker: Any) -> None:
        """Circuit opens after failure threshold."""
        for _ in range(ResilienceTestConfig.CB_FAILURE_THRESHOLD):
            circuit_breaker.record_failure(ConnectionError("test"))

        assert circuit_breaker.state == CircuitState.OPEN
        assert circuit_breaker.can_execute() is False

    def test_circuit_transitions_to_half_open(self, circuit_breaker: Any) -> None:
        """Circuit transitions to HALF_OPEN after recovery timeout."""
        import time

        # Open the circuit
        for _ in range(ResilienceTestConfig.CB_FAILURE_THRESHOLD):
            circuit_breaker.record_failure(ConnectionError("test"))

        assert circuit_breaker.state == CircuitState.OPEN

        # Wait for recovery timeout
        time.sleep(ResilienceTestConfig.CB_RECOVERY_TIMEOUT + 0.1)

        # Should transition to HALF_OPEN
        assert circuit_breaker.can_execute() is True
        assert circuit_breaker.state == CircuitState.HALF_OPEN

    def test_circuit_closes_after_successful_calls(self, circuit_breaker: Any) -> None:
        """Circuit closes after successful calls in HALF_OPEN."""
        import time

        # Open the circuit
        for _ in range(ResilienceTestConfig.CB_FAILURE_THRESHOLD):
            circuit_breaker.record_failure(ConnectionError("test"))

        time.sleep(ResilienceTestConfig.CB_RECOVERY_TIMEOUT + 0.1)
        circuit_breaker.can_execute()  # Trigger HALF_OPEN

        # Record successful calls
        for _ in range(circuit_breaker.half_open_max_calls):
            circuit_breaker.record_success()

        assert circuit_breaker.state == CircuitState.CLOSED

    def test_circuit_reopens_on_half_open_failure(self, circuit_breaker: Any) -> None:
        """Circuit reopens on failure in HALF_OPEN state."""
        import time

        # Open the circuit
        for _ in range(ResilienceTestConfig.CB_FAILURE_THRESHOLD):
            circuit_breaker.record_failure(ConnectionError("test"))

        time.sleep(ResilienceTestConfig.CB_RECOVERY_TIMEOUT + 0.1)
        circuit_breaker.can_execute()  # Trigger HALF_OPEN

        # Fail in HALF_OPEN
        circuit_breaker.record_failure(ConnectionError("test"))

        assert circuit_breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_decorator_respects_circuit(self) -> None:
        """Circuit breaker decorator respects circuit state."""
        from src.core.circuit_breaker import circuit_breakers

        # Create a test circuit breaker
        test_cb = ServiceCircuitBreaker("test_decorator", failure_threshold=2)
        circuit_breakers["test_decorator"] = test_cb

        try:
            call_count = 0

            @with_circuit_breaker("test_decorator")
            async def operation() -> None:
                nonlocal call_count
                call_count += 1
                raise ConnectionError("fail")

            # Fail until circuit opens
            for _ in range(3):
                try:
                    await operation()
                except (ConnectionError, ServiceUnavailableError):
                    pass

            # Circuit should be open now
            with pytest.raises(ServiceUnavailableError):
                await operation()

        finally:
            del circuit_breakers["test_decorator"]


# =============================================================================
# Bulkhead Tests
# =============================================================================


@pytest.mark.resilience
class TestBulkheadPattern:
    """Tests for bulkhead pattern."""

    @pytest.mark.asyncio
    async def test_bulkhead_allows_within_limit(self, bulkhead: Any) -> None:
        """Bulkhead allows requests within concurrency limit."""
        results = []

        async def operation(n: Any) -> Any:
            async with bulkhead.acquire():
                await asyncio.sleep(0.1)
                results.append(n)
                return n

        tasks = [operation(i) for i in range(ResilienceTestConfig.BH_MAX_CONCURRENT)]
        await asyncio.gather(*tasks)

        assert len(results) == ResilienceTestConfig.BH_MAX_CONCURRENT

    @pytest.mark.asyncio
    async def test_bulkhead_rejects_over_limit(self, bulkhead: Any) -> None:
        """Bulkhead rejects requests over limit when queue is full."""

        # Fill up the bulkhead
        async def slow_operation() -> None:
            async with bulkhead.acquire():
                await asyncio.sleep(1.0)

        # Start max + queue tasks
        max_tasks = (
            ResilienceTestConfig.BH_MAX_CONCURRENT + ResilienceTestConfig.BH_MAX_QUEUE
        )
        tasks = [asyncio.create_task(slow_operation()) for _ in range(max_tasks)]

        # Give time for tasks to acquire/queue
        await asyncio.sleep(0.1)

        # Next request should fail
        with pytest.raises(BulkheadFullError):
            async with bulkhead.acquire(timeout=0.1):
                pass

        # Cancel background tasks
        for task in tasks:
            task.cancel()

    @pytest.mark.asyncio
    async def test_bulkhead_queues_when_full(self, bulkhead: Any) -> None:
        """Bulkhead queues requests when at capacity."""
        order = []

        async def operation(n: Any, delay: Any = 0.1) -> None:
            async with bulkhead.acquire():
                await asyncio.sleep(delay)
                order.append(n)

        # Start more than max_concurrent tasks
        tasks = [
            asyncio.create_task(operation(i))
            for i in range(ResilienceTestConfig.BH_MAX_CONCURRENT + 2)
        ]

        await asyncio.gather(*tasks)

        # All should complete
        assert len(order) == ResilienceTestConfig.BH_MAX_CONCURRENT + 2

    @pytest.mark.asyncio
    async def test_bulkhead_stats(self, bulkhead: Any) -> None:
        """Bulkhead tracks statistics correctly."""

        async def operation() -> None:
            async with bulkhead.acquire():
                await asyncio.sleep(0.01)

        # Run some operations
        await asyncio.gather(*[operation() for _ in range(5)])

        stats = bulkhead.get_stats()
        assert stats.total_acquired >= 5
        assert stats.current_concurrent == 0


# =============================================================================
# Timeout Tests
# =============================================================================


@pytest.mark.resilience
class TestTimeoutPattern:
    """Tests for timeout pattern."""

    @pytest.mark.asyncio
    async def test_timeout_allows_fast_operations(self, slow_service: Any) -> None:
        """Timeout allows operations that complete in time."""
        fast_call = slow_service(delay=0.1)

        @with_timeout(ResilienceTestConfig.OPERATION_TIMEOUT)
        async def operation() -> Any:
            return await fast_call()

        result = await operation()
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_timeout_cancels_slow_operations(self, slow_service: Any) -> None:
        """Timeout cancels operations that exceed limit."""
        slow_call = slow_service(delay=5.0)

        @with_timeout(ResilienceTestConfig.OPERATION_TIMEOUT)
        async def operation() -> Any:
            return await slow_call()

        with pytest.raises(TimeoutError):
            await operation()

    @pytest.mark.asyncio
    async def test_timeout_error_contains_context(self, slow_service: Any) -> None:
        """Timeout error contains operation name."""
        slow_call = slow_service(delay=5.0)

        @with_timeout(0.1, operation_name="my_slow_operation")
        async def operation() -> Any:
            return await slow_call()

        try:
            await operation()
        except TimeoutError as e:
            assert e.operation == "my_slow_operation"
            assert e.timeout == 0.1


# =============================================================================
# Combined Resilience Tests
# =============================================================================


@pytest.mark.resilience
class TestCombinedResilience:
    """Tests for combined resilience patterns."""

    @pytest.mark.asyncio
    async def test_retry_with_circuit_breaker(self, failing_service: Any) -> None:
        """Retry and circuit breaker work together."""
        from src.core.circuit_breaker import circuit_breakers
        from src.core.resilience import resilient

        # Create test circuit breaker
        test_cb = ServiceCircuitBreaker("combined_test", failure_threshold=5)
        circuit_breakers["combined_test"] = test_cb

        try:
            mock_call, get_count = failing_service(fail_count=2)

            @resilient(
                retry_attempts=3,
                retry_delay=0.01,
                circuit_breaker="combined_test",
            )
            async def operation() -> Any:
                return await mock_call()

            result = await operation()
            assert result["success"] is True

        finally:
            del circuit_breakers["combined_test"]

    @pytest.mark.asyncio
    async def test_bulkhead_with_timeout(
        self, bulkhead: Any, slow_service: Any
    ) -> None:
        """Bulkhead and timeout work together."""
        slow_call = slow_service(delay=5.0)

        async def operation() -> Any:
            async with bulkhead.acquire(timeout=0.5):
                try:
                    return await asyncio.wait_for(slow_call(), timeout=0.2)
                except asyncio.TimeoutError:
                    raise TimeoutError("operation", 0.2)

        with pytest.raises(TimeoutError):
            await operation()


# =============================================================================
# Load Testing
# =============================================================================


@pytest.mark.slow
@pytest.mark.resilience
class TestResilienceUnderLoad:
    """Tests for resilience patterns under load."""

    @pytest.mark.asyncio
    async def test_bulkhead_under_burst(self, bulkhead: Any) -> None:
        """Bulkhead handles burst traffic."""
        results = {"success": 0, "rejected": 0}

        async def operation() -> None:
            try:
                async with bulkhead.acquire(timeout=0.5):
                    await asyncio.sleep(0.1)
                    results["success"] += 1
            except BulkheadFullError:
                results["rejected"] += 1

        # Simulate burst
        burst_size = 50
        await asyncio.gather(*[operation() for _ in range(burst_size)])

        # Some should succeed, some should be rejected
        assert results["success"] > 0
        assert results["success"] + results["rejected"] == burst_size

    @pytest.mark.asyncio
    async def test_circuit_breaker_under_failure_storm(self) -> None:
        """Circuit breaker protects under failure storm."""
        from src.core.circuit_breaker import circuit_breakers

        test_cb = ServiceCircuitBreaker("storm_test", failure_threshold=5)
        circuit_breakers["storm_test"] = test_cb

        try:
            call_count = 0
            rejected_count = 0

            @with_circuit_breaker("storm_test")
            async def failing_operation() -> None:
                nonlocal call_count
                call_count += 1
                raise ConnectionError("fail")

            # Simulate failure storm
            for _ in range(100):
                try:
                    await failing_operation()
                except ConnectionError:
                    pass
                except ServiceUnavailableError:
                    rejected_count += 1

            # Circuit should have opened and rejected many calls
            assert rejected_count > 90  # Most should be rejected
            assert call_count <= 10  # Few actual calls

        finally:
            del circuit_breakers["storm_test"]


@pytest.mark.asyncio
async def test_bulkhead_body_timeout_is_not_counted_as_rejection() -> None:
    """A TimeoutError raised by the guarded body is the caller's error: it
    must propagate unchanged and must not count as a bulkhead rejection or
    disturb the queue accounting."""
    bulkhead = Bulkhead("body-timeout", max_concurrent=1, max_queue=0)

    with pytest.raises(asyncio.TimeoutError):
        async with bulkhead.acquire(timeout=1.0):
            raise asyncio.TimeoutError("caller's own timeout")

    stats = bulkhead.get_stats()
    assert stats.total_rejected == 0
    assert stats.queue_size == 0
    assert stats.current_concurrent == 0
    # The slot was released, so the next caller gets in.
    async with bulkhead.acquire(timeout=1.0):
        pass
