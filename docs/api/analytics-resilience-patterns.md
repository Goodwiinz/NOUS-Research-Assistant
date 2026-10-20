# Analytics Dashboard - Resilience Patterns and Error Handling

> **Stale / historical (2026-10-17):** It covers only part of the API and its paths and schemas may have drifted. The source of truth is the generated `backend/openapi.json` (also served at `/docs` when `DEBUG` is on). Current contracts: [`docs/engineering/`](../engineering/README.md).

## 1. Resilience Architecture Overview

### Multi-Layer Resilience Strategy

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Edge Resilience Layer                             │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   Load Balancer │  │   Rate Limiting │  │   API Gateway   │           │
│  │   (Health Checks│  │   (Per Client)  │  │   (Circuit      │           │
│  │   Failover)     │  │   (Burst Limit) │  │   Breaker)      │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Application Resilience                              │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   Circuit       │  │   Retry         │  │   Timeout       │           │
│  │   Breakers      │  │   Patterns      │  │   Management    │           │
│  │                 │  │                 │  │                 │           │
│  │ - Service       │  │ - Exponential   │  │ - Request       │           │
│  │   Protection    │  │   Backoff       │  │   Timeouts      │           │
│  │ - Auto Recovery │  │ - Jitter        │  │ - Graceful      │           │
│  │ - Fallback      │  │ - Circuit       │  │   Degradation   │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Data Resilience Layer                               │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   Database      │  │   Cache         │  │   Message Queue │           │
│  │   Failover      │  │   Failover      │  │   Redundancy    │           │
│  │                 │  │                 │  │                 │           │
│  │ - Primary/      │  │ - Redis Cluster │  │ - Queue         │           │
│  │   Replica      │  │ - Failover      │  │   Mirroring     │           │
│  │ - Connection    │  │ - Cache Warming │  │ - Dead Letter   │           │
│  │   Pooling       │  │ - Graceful      │  │   Queues        │           │
│  │                 │  │   Degradation   │  │                 │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Monitoring and Recovery                               │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   Health        │  │   Alerting      │  │   Auto          │           │
│  │   Monitoring    │  │   System        │  │   Healing       │           │
│  │                 │  │                 │  │                 │           │
│  │ - Service       │  │ - Alert         │  │ - Service       │           │
│  │   Health        │  │   Aggregation   │  │   Restart       │           │
│  │ - Dependency    │  │ - Multi-channel │  │ - Resource      │           │
│  │   Tracking      │  │ - Escalation    │  │   Scaling       │           │
│  │ - Performance   │  │ - Auto          │  │ - Failover      │           │
│  │   Metrics       │  │   Resolution    │  │   Promotion     │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 2. Circuit Breaker Pattern Implementation

### 2.1 Service-Level Circuit Breakers
```python
from enum import Enum
import time
import asyncio
from typing import Any, Callable, Optional, Dict
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker"""
    failure_threshold: int = 5
    recovery_timeout: float = 60.0  # seconds
    expected_exception: type = Exception
    success_threshold: int = 3  # For half-open state
    timeout: float = 30.0  # Function timeout
    max_retries: int = 3

class CircuitBreaker:
    """Circuit breaker implementation for service protection"""

    def __init__(self, name: str, config: CircuitBreakerConfig):
        self.name = name
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self.lock = asyncio.Lock()

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection"""

        async with self.lock:
            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    logger.info(f"Circuit breaker {self.name} entering HALF_OPEN state")
                else:
                    raise CircuitBreakerOpenException(
                        f"Circuit breaker {self.name} is OPEN"
                    )

        try:
            # Execute with timeout
            result = await asyncio.wait_for(
                func(*args, **kwargs),
                timeout=self.config.timeout
            )

            await self._on_success()
            return result

        except asyncio.TimeoutError:
            await self._on_failure()
            raise TimeoutException(f"Function {func.__name__} timed out")
        except self.config.expected_exception as e:
            await self._on_failure()
            raise e
        except Exception as e:
            await self._on_failure()
            raise ServiceUnavailableException(f"Service {self.name} unavailable: {e}")

    async def _on_success(self):
        """Handle successful execution"""

        async with self.lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.config.success_threshold:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    logger.info(f"Circuit breaker {self.name} reset to CLOSED state")
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0

    async def _on_failure(self):
        """Handle failed execution"""

        async with self.lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit breaker {self.name} returned to OPEN state")
            elif (
                self.state == CircuitState.CLOSED and
                self.failure_count >= self.config.failure_threshold
            ):
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit breaker {self.name} opened due to failures")

    def _should_attempt_reset(self) -> bool:
        """Check if circuit breaker should attempt reset"""

        if self.last_failure_time is None:
            return False

        return (
            time.time() - self.last_failure_time >=
            self.config.recovery_timeout
        )

    def get_state(self) -> Dict[str, Any]:
        """Get current circuit breaker state"""

        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_failure_time": self.last_failure_time,
            "recovery_timeout": self.config.recovery_timeout
        }

class CircuitBreakerRegistry:
    """Registry for managing circuit breakers"""

    def __init__(self):
        self._breakers: Dict[str, CircuitBreaker] = {}

    def register(self, name: str, config: CircuitBreakerConfig) -> CircuitBreaker:
        """Register a new circuit breaker"""

        if name in self._breakers:
            logger.warning(f"Circuit breaker {name} already exists, overwriting")

        breaker = CircuitBreaker(name, config)
        self._breakers[name] = breaker
        return breaker

    def get(self, name: str) -> Optional[CircuitBreaker]:
        """Get circuit breaker by name"""

        return self._breakers.get(name)

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """Get all circuit breaker states"""

        return {
            name: breaker.get_state()
            for name, breaker in self._breakers.items()
        }

# Global registry
circuit_breaker_registry = CircuitBreakerRegistry()

# Circuit breaker configurations
ANALYTICS_CIRCUIT_BREAKERS = {
    "neo4j_service": CircuitBreakerConfig(
        failure_threshold=5,
        recovery_timeout=30.0,
        expected_exception=Exception,
        timeout=15.0,
        max_retries=3
    ),
    "qdrant_service": CircuitBreakerConfig(
        failure_threshold=3,
        recovery_timeout=60.0,
        expected_exception=Exception,
        timeout=10.0,
        max_retries=2
    ),
    "postgres_service": CircuitBreakerConfig(
        failure_threshold=8,
        recovery_timeout=20.0,
        expected_exception=Exception,
        timeout=8.0,
        max_retries=3
    ),
    "redis_cache": CircuitBreakerConfig(
        failure_threshold=10,
        recovery_timeout=15.0,
        expected_exception=Exception,
        timeout=2.0,
        max_retries=5
    )
}
```

### 2.2 Decorator-Based Circuit Breaker
```python
def with_circuit_breaker(
    service_name: str,
    fallback_func: Optional[Callable] = None
):
    """Decorator for applying circuit breaker to functions"""

    def decorator(func):
        async def wrapper(*args, **kwargs):
            breaker = circuit_breaker_registry.get(service_name)
            if not breaker:
                # Create default circuit breaker if not exists
                breaker = circuit_breaker_registry.register(
                    service_name,
                    CircuitBreakerConfig()
                )

            try:
                return await breaker.call(func, *args, **kwargs)
            except CircuitBreakerOpenException:
                if fallback_func:
                    logger.warning(f"Circuit breaker open for {service_name}, using fallback")
                    return await fallback_func(*args, **kwargs)
                else:
                    raise ServiceUnavailableException(
                        f"Service {service_name} is currently unavailable"
                    )

        return wrapper
    return decorator

# Usage examples
@with_circuit_breaker("neo4j_service", fallback_func=get_cached_graph_metrics)
async def calculate_centrality_metrics(organization_id: str, algorithm: str):
    """Calculate centrality metrics with circuit breaker protection"""
    # Neo4j query implementation
    pass

@with_circuit_breaker("qdrant_service")
async def search_similar_entities(query_vector: List[float], limit: int):
    """Search similar entities with circuit breaker protection"""
    # Qdrant search implementation
    pass
```

## 3. Retry Pattern Implementation

### 3.1 Exponential Backoff with Jitter
```python
import random
import asyncio
from typing import Callable, Any, List, Type
from functools import wraps

class RetryConfig:
    """Configuration for retry behavior"""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
        retryable_exceptions: List[Type[Exception]] = None
    ):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
        self.retryable_exceptions = retryable_exceptions or [
            ConnectionError,
            TimeoutError,
            ServiceUnavailableException
        ]

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay for retry attempt"""

        delay = min(
            self.base_delay * (self.exponential_base ** (attempt - 1)),
            self.max_delay
        )

        if self.jitter:
            # Add random jitter to avoid thundering herd
            jitter_range = delay * 0.1
            delay += random.uniform(-jitter_range, jitter_range)

        return max(0, delay)

class RetryManager:
    """Retry manager with exponential backoff"""

    def __init__(self, config: RetryConfig):
        self.config = config

    async def execute_with_retry(
        self,
        func: Callable,
        *args,
        **kwargs
    ) -> Any:
        """Execute function with retry logic"""

        last_exception = None

        for attempt in range(1, self.config.max_attempts + 1):
            try:
                return await func(*args, **kwargs)

            except Exception as e:
                last_exception = e

                # Check if exception is retryable
                if not any(isinstance(e, exc_type)
                          for exc_type in self.config.retryable_exceptions):
                    raise e

                if attempt == self.config.max_attempts:
                    # Final attempt failed
                    logger.error(
                        f"Function {func.__name__} failed after "
                        f"{self.config.max_attempts} attempts. Last error: {e}"
                    )
                    raise MaxRetriesExceededException(
                        f"Max retries exceeded for {func.__name__}"
                    ) from e

                # Calculate delay and wait
                delay = self.config.calculate_delay(attempt)
                logger.warning(
                    f"Attempt {attempt} failed for {func.__name__}: {e}. "
                    f"Retrying in {delay:.2f} seconds"
                )
                await asyncio.sleep(delay)

        # This should not be reached
        raise last_exception

def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: List[Type[Exception]] = None
):
    """Decorator for applying retry logic to functions"""

    config = RetryConfig(
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
        exponential_base=exponential_base,
        jitter=jitter,
        retryable_exceptions=retryable_exceptions
    )
    retry_manager = RetryManager(config)

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await retry_manager.execute_with_retry(func, *args, **kwargs)

        return wrapper
    return decorator

# Usage examples
@with_retry(
    max_attempts=5,
    base_delay=0.5,
    max_delay=30.0,
    retryable_exceptions=[ConnectionError, TimeoutError]
)
async def fetch_graph_data(organization_id: str):
    """Fetch graph data with retry logic"""
    # Database query implementation
    pass

@with_retry(max_attempts=3, base_delay=1.0)
async def send_notification(notification_data: dict):
    """Send notification with retry logic"""
    # Notification service implementation
    pass
```

### 3.2 Conditional Retry Based on Response
```python
class ConditionalRetryManager(RetryManager):
    """Retry manager that can retry based on response conditions"""

    def __init__(
        self,
        config: RetryConfig,
        retry_conditions: List[Callable[[Any], bool]] = None
    ):
        super().__init__(config)
        self.retry_conditions = retry_conditions or []

    def should_retry(self, result: Any, exception: Exception) -> bool:
        """Determine if operation should be retried based on result or exception"""

        # Check exception-based retry conditions
        if exception and any(
            isinstance(exception, exc_type)
            for exc_type in self.config.retryable_exceptions
        ):
            return True

        # Check result-based retry conditions
        if result:
            return any(condition(result) for condition in self.retry_conditions)

        return False

    async def execute_with_conditional_retry(
        self,
        func: Callable,
        *args,
        **kwargs
    ) -> Any:
        """Execute with conditional retry logic"""

        last_exception = None
        last_result = None

        for attempt in range(1, self.config.max_attempts + 1):
            try:
                result = await func(*args, **kwargs)

                # Check if result indicates we should retry
                if self.should_retry(result, None):
                    if attempt == self.config.max_attempts:
                        logger.warning(
                            f"Result indicates retry but max attempts reached "
                            f"for {func.__name__}"
                        )
                        return result

                    delay = self.config.calculate_delay(attempt)
                    logger.warning(
                        f"Result indicates retry for {func.__name__}. "
                        f"Retrying in {delay:.2f} seconds"
                    )
                    await asyncio.sleep(delay)
                    last_result = result
                    continue

                return result

            except Exception as e:
                last_exception = e

                if not self.should_retry(None, e):
                    raise e

                if attempt == self.config.max_attempts:
                    logger.error(
                        f"Function {func.__name__} failed after "
                        f"{self.config.max_attempts} attempts"
                    )
                    raise MaxRetriesExceededException(
                        f"Max retries exceeded for {func.__name__}"
                    ) from e

                delay = self.config.calculate_delay(attempt)
                logger.warning(
                    f"Attempt {attempt} failed for {func.__name__}: {e}. "
                    f"Retrying in {delay:.2f} seconds"
                )
                await asyncio.sleep(delay)

        return last_result or last_exception

# Usage example with conditional retry
def should_retry_on_503(response):
    """Retry condition for HTTP 503 responses"""
    return hasattr(response, 'status_code') and response.status_code == 503

def should_retry_on_rate_limit(response):
    """Retry condition for rate limited responses"""
    return (
        hasattr(response, 'headers') and
        'retry-after' in response.headers
    )

conditional_retry_manager = ConditionalRetryManager(
    config=RetryConfig(max_attempts=5),
    retry_conditions=[should_retry_on_503, should_retry_on_rate_limit]
)
```

## 4. Timeout Management

### 4.1 Hierarchical Timeouts
```python
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

class TimeoutManager:
    """Manages hierarchical timeouts for complex operations"""

    def __init__(self, default_timeout: float = 30.0):
        self.default_timeout = default_timeout
        self.timeout_stack: List[float] = []

    @asynccontextmanager
    def timeout(self, timeout: Optional[float] = None):
        """Context manager for timeout"""

        effective_timeout = timeout or self.default_timeout
        self.timeout_stack.append(effective_timeout)

        try:
            async with asyncio.timeout(effective_timeout):
                yield effective_timeout
        finally:
            self.timeout_stack.pop()

    def get_remaining_timeout(self) -> float:
        """Get remaining timeout from current stack"""

        if not self.timeout_stack:
            return self.default_timeout

        # Return the smallest timeout in the stack
        return min(self.timeout_stack)

    async def execute_with_timeout(
        self,
        func: Callable,
        timeout: Optional[float] = None,
        *args,
        **kwargs
    ) -> Any:
        """Execute function with timeout management"""

        effective_timeout = timeout or self.get_remaining_timeout()

        try:
            return await asyncio.wait_for(
                func(*args, **kwargs),
                timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutException(
                f"Operation {func.__name__} timed out after {effective_timeout}s"
            )

# Global timeout manager
timeout_manager = TimeoutManager(default_timeout=30.0)

def with_timeout(timeout: Optional[float] = None):
    """Decorator for applying timeout to functions"""

    def decorator(func):
        async def wrapper(*args, **kwargs):
            return await timeout_manager.execute_with_timeout(
                func, timeout, *args, **kwargs
            )
        return wrapper
    return decorator

# Usage examples
@with_timeout(timeout=10.0)
async def fetch_external_data(url: str):
    """Fetch external data with timeout"""
    # HTTP request implementation
    pass

@with_timeout()
async def process_analytics_query(query: str):
    """Process analytics query with default timeout"""
    # Query processing implementation
    pass
```

### 4.2 Graceful Degradation
```python
class GracefulDegradationManager:
    """Manages graceful degradation when services are unavailable"""

    def __init__(self):
        self.fallback_strategies: Dict[str, Callable] = {}
        self.degraded_services: Dict[str, bool] = {}

    def register_fallback(self, service_name: str, fallback_func: Callable):
        """Register fallback strategy for a service"""

        self.fallback_strategies[service_name] = fallback_func

    def mark_degraded(self, service_name: str, degraded: bool = True):
        """Mark service as degraded or recovered"""

        was_degraded = self.degraded_services.get(service_name, False)
        self.degraded_services[service_name] = degraded

        if degraded and not was_degraded:
            logger.warning(f"Service {service_name} marked as degraded")
        elif not degraded and was_degraded:
            logger.info(f"Service {service_name} recovered from degraded state")

    async def execute_with_fallback(
        self,
        service_name: str,
        primary_func: Callable,
        *args,
        **kwargs
    ) -> Any:
        """Execute function with fallback when service is degraded"""

        if self.degraded_services.get(service_name, False):
            fallback_func = self.fallback_strategies.get(service_name)
            if fallback_func:
                logger.info(f"Using fallback for degraded service {service_name}")
                try:
                    return await fallback_func(*args, **kwargs)
                except Exception as e:
                    logger.error(f"Fallback failed for {service_name}: {e}")
                    raise ServiceUnavailableException(
                        f"Service {service_name} and fallback are unavailable"
                    )
            else:
                raise ServiceUnavailableException(
                    f"Service {service_name} is degraded and no fallback available"
                )

        try:
            return await primary_func(*args, **kwargs)
        except Exception as e:
            # Mark service as degraded on failure
            self.mark_degraded(service_name, True)

            # Try fallback
            fallback_func = self.fallback_strategies.get(service_name)
            if fallback_func:
                logger.warning(
                    f"Primary service {service_name} failed, trying fallback: {e}"
                )
                try:
                    return await fallback_func(*args, **kwargs)
                except Exception as fallback_error:
                    logger.error(f"Fallback failed for {service_name}: {fallback_error}")
                    raise ServiceUnavailableException(
                        f"Both primary and fallback failed for {service_name}"
                    )
            else:
                raise e

# Global degradation manager
degradation_manager = GracefulDegradationManager()

# Register fallback strategies
async def fallback_graph_metrics(organization_id: str) -> dict:
    """Fallback for graph metrics using cached data"""
    # Return cached or estimated metrics
    return {
        "estimated": True,
        "data_age": "cached",
        "metrics": {}  # Fallback metrics
    }

async def fallback_realtime_data(organization_id: str) -> dict:
    """Fallback for real-time data using latest available"""
    # Return slightly stale data
    return {
        "stale": True,
        "max_delay_minutes": 5,
        "data": {}  # Stale data
    }

degradation_manager.register_fallback("neo4j_service", fallback_graph_metrics)
degradation_manager.register_fallback("realtime_service", fallback_realtime_data)
```

## 5. Bulkhead Pattern

### 5.1 Service Isolation with Bulkheads
```python
import asyncio
from asyncio import Semaphore

class Bulkhead:
    """Bulkhead pattern for service isolation"""

    def __init__(self, name: str, max_concurrent: int, max_queue_size: int = 100):
        self.name = name
        self.semaphore = Semaphore(max_concurrent)
        self.max_queue_size = max_queue_size
        self.queue = asyncio.Queue(maxsize=max_queue_size)
        self.active_tasks = 0
        self.rejected_tasks = 0

    async def execute(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with bulkhead protection"""

        try:
            # Try to acquire semaphore immediately
            await self.semaphore.acquire()
        except asyncio.TimeoutError:
            self.rejected_tasks += 1
            raise BulkheadFullException(
                f"Bulkhead {self.name} is full. Task rejected."
            )

        try:
            self.active_tasks += 1
            return await func(*args, **kwargs)
        finally:
            self.active_tasks -= 1
            self.semaphore.release()

    def get_stats(self) -> dict:
        """Get bulkhead statistics"""

        return {
            "name": self.name,
            "active_tasks": self.active_tasks,
            "available_slots": self.semaphore._value,
            "rejected_tasks": self.rejected_tasks,
            "queue_size": self.queue.qsize(),
            "max_concurrent": self.semaphore._initial_value
        }

class BulkheadRegistry:
    """Registry for managing bulkheads"""

    def __init__(self):
        self.bulkheads: Dict[str, Bulkhead] = {}

    def register(self, name: str, max_concurrent: int, max_queue_size: int = 100):
        """Register a new bulkhead"""

        bulkhead = Bulkhead(name, max_concurrent, max_queue_size)
        self.bulkheads[name] = bulkhead
        return bulkhead

    def get(self, name: str) -> Optional[Bulkhead]:
        """Get bulkhead by name"""

        return self.bulkheads.get(name)

    def get_all_stats(self) -> Dict[str, dict]:
        """Get all bulkhead statistics"""

        return {
            name: bulkhead.get_stats()
            for name, bulkhead in self.bulkheads.items()
        }

# Global bulkhead registry
bulkhead_registry = BulkheadRegistry()

# Register bulkheads for different service categories
bulkhead_registry.register("graph_analytics", max_concurrent=5, max_queue_size=50)
bulkhead_registry.register("report_generation", max_concurrent=3, max_queue_size=20)
bulkhead_registry.register("realtime_updates", max_concurrent=10, max_queue_size=100)
bulkhead_registry.register("external_apis", max_concurrent=15, max_queue_size=200)

def with_bulkhead(bulkhead_name: str):
    """Decorator for applying bulkhead protection to functions"""

    def decorator(func):
        async def wrapper(*args, **kwargs):
            bulkhead = bulkhead_registry.get(bulkhead_name)
            if not bulkhead:
                # Execute without bulkhead protection
                return await func(*args, **kwargs)

            return await bulkhead.execute(func, *args, **kwargs)

        return wrapper
    return decorator

# Usage examples
@with_bulkhead("graph_analytics")
async def compute_graph_metrics(organization_id: str):
    """Compute graph metrics with bulkhead protection"""
    # Graph computation implementation
    pass

@with_bulkhead("report_generation")
async def generate_report(report_id: str):
    """Generate report with bulkhead protection"""
    # Report generation implementation
    pass
```

## 6. Health Check and Recovery

### 6.1 Comprehensive Health Checking
```python
from typing import Dict, List
from dataclasses import dataclass
from enum import Enum

class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"

@dataclass
class HealthCheckResult:
    """Result of a health check"""

    service_name: str
    status: HealthStatus
    message: str
    response_time_ms: float
    details: Dict[str, Any] = None
    timestamp: float = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()

class HealthChecker:
    """Health checker for various services"""

    def __init__(self):
        self.checks: Dict[str, Callable] = {}
        self.check_history: Dict[str, List[HealthCheckResult]] = {}

    def register_check(self, service_name: str, check_func: Callable):
        """Register a health check function"""

        self.checks[service_name] = check_func
        if service_name not in self.check_history:
            self.check_history[service_name] = []

    async def check_all(self) -> Dict[str, HealthCheckResult]:
        """Run all health checks"""

        results = {}
        tasks = []

        for service_name, check_func in self.checks.items():
            task = asyncio.create_task(
                self._run_check(service_name, check_func)
            )
            tasks.append((service_name, task))

        for service_name, task in tasks:
            try:
                result = await task
                results[service_name] = result
                self.check_history[service_name].append(result)

                # Keep only last 100 results
                if len(self.check_history[service_name]) > 100:
                    self.check_history[service_name] = self.check_history[service_name][-100:]

            except Exception as e:
                error_result = HealthCheckResult(
                    service_name=service_name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Health check failed: {e}",
                    response_time_ms=0
                )
                results[service_name] = error_result
                self.check_history[service_name].append(error_result)

        return results

    async def _run_check(
        self,
        service_name: str,
        check_func: Callable
    ) -> HealthCheckResult:
        """Run individual health check"""

        start_time = time.time()

        try:
            result = await check_func()
            response_time = (time.time() - start_time) * 1000

            if isinstance(result, HealthCheckResult):
                result.response_time_ms = response_time
                return result
            elif isinstance(result, bool):
                return HealthCheckResult(
                    service_name=service_name,
                    status=HealthStatus.HEALTHY if result else HealthStatus.UNHEALTHY,
                    message="OK" if result else "Health check failed",
                    response_time_ms=response_time
                )
            else:
                return HealthCheckResult(
                    service_name=service_name,
                    status=HealthStatus.HEALTHY,
                    message=str(result),
                    response_time_ms=response_time
                )

        except Exception as e:
            response_time = (time.time() - start_time) * 1000
            return HealthCheckResult(
                service_name=service_name,
                status=HealthStatus.UNHEALTHY,
                message=str(e),
                response_time_ms=response_time
            )

    def get_service_trend(self, service_name: str, lookback_minutes: int = 60) -> Dict[str, Any]:
        """Get health trend for a service"""

        if service_name not in self.check_history:
            return {"status": "no_data"}

        cutoff_time = time.time() - (lookback_minutes * 60)
        recent_checks = [
            check for check in self.check_history[service_name]
            if check.timestamp >= cutoff_time
        ]

        if not recent_checks:
            return {"status": "no_data"}

        status_counts = {}
        total_response_time = 0

        for check in recent_checks:
            status_counts[check.status.value] = status_counts.get(check.status.value, 0) + 1
            total_response_time += check.response_time_ms

        avg_response_time = total_response_time / len(recent_checks)
        success_rate = status_counts.get("healthy", 0) / len(recent_checks)

        return {
            "status": "healthy" if success_rate >= 0.95 else "degraded" if success_rate >= 0.8 else "unhealthy",
            "success_rate": success_rate,
            "avg_response_time_ms": avg_response_time,
            "total_checks": len(recent_checks),
            "status_distribution": status_counts
        }

# Global health checker
health_checker = HealthChecker()

# Health check implementations
async def check_postgres_health() -> HealthCheckResult:
    """Check PostgreSQL health"""

    try:
        # Test database connection
        async with DatabaseSession() as session:
            result = await session.execute("SELECT 1 as health_check")
            row = result.fetchone()

        return HealthCheckResult(
            service_name="postgres",
            status=HealthStatus.HEALTHY,
            message="Database connection successful",
            response_time_ms=0,
            details={"connection_pool_size": "10/10"}
        )

    except Exception as e:
        return HealthCheckResult(
            service_name="postgres",
            status=HealthStatus.UNHEALTHY,
            message=f"Database connection failed: {e}",
            response_time_ms=0
        )

async def check_redis_health() -> HealthCheckResult:
    """Check Redis health"""

    try:
        redis_client = RedisConnection()
        start_time = time.time()
        await redis_client.ping()
        response_time = (time.time() - start_time) * 1000

        info = await redis_client.info()

        return HealthCheckResult(
            service_name="redis",
            status=HealthStatus.HEALTHY,
            message="Redis connection successful",
            response_time_ms=response_time,
            details={
                "used_memory": info.get("used_memory_human"),
                "connected_clients": info.get("connected_clients")
            }
        )

    except Exception as e:
        return HealthCheckResult(
            service_name="redis",
            status=HealthStatus.UNHEALTHY,
            message=f"Redis connection failed: {e}",
            response_time_ms=0
        )

async def check_neo4j_health() -> HealthCheckResult:
    """Check Neo4j health"""

    try:
        driver = Neo4jDriver()
        start_time = time.time()

        with driver.session() as session:
            result = session.run("RETURN 1 as health_check")
            record = result.single()

        response_time = (time.time() - start_time) * 1000

        return HealthCheckResult(
            service_name="neo4j",
            status=HealthStatus.HEALTHY,
            message="Neo4j connection successful",
            response_time_ms=response_time,
            details={"query_successful": True}
        )

    except Exception as e:
        return HealthCheckResult(
            service_name="neo4j",
            status=HealthStatus.UNHEALTHY,
            message=f"Neo4j connection failed: {e}",
            response_time_ms=0
        )

# Register health checks
health_checker.register_check("postgres", check_postgres_health)
health_checker.register_check("redis", check_redis_health)
health_checker.register_check("neo4j", check_neo4j_health)
```

## 7. Custom Exception Classes

```python
class AnalyticsException(Exception):
    """Base exception for analytics service"""
    pass

class CircuitBreakerOpenException(AnalyticsException):
    """Raised when circuit breaker is open"""
    pass

class TimeoutException(AnalyticsException):
    """Raised when operation times out"""
    pass

class ServiceUnavailableException(AnalyticsException):
    """Raised when service is unavailable"""
    pass

class MaxRetriesExceededException(AnalyticsException):
    """Raised when maximum retry attempts are exceeded"""
    pass

class BulkheadFullException(AnalyticsException):
    """Raised when bulkhead is full"""
    pass

class CacheUnavailableException(AnalyticsException):
    """Raised when cache is unavailable"""
    pass

class DatabaseUnavailableException(AnalyticsException):
    """Raised when database is unavailable"""
    pass
```

## 8. Implementation Checklist

### Resilience Implementation Tasks

- [ ] Circuit breaker implementation for all external services
- [ ] Retry patterns with exponential backoff
- [ ] Timeout management at multiple levels
- [ ] Bulkhead pattern for service isolation
- [ ] Graceful degradation strategies
- [ ] Comprehensive health checking
- [ ] Automatic service recovery mechanisms
- [ ] Performance monitoring and alerting
- [ ] Chaos engineering testing
- [ ] Documentation of failure scenarios
- [ ] Service dependency mapping
- [ ] Recovery time objectives (RTO) definition

### Monitoring and Alerting

- [ ] Circuit breaker state monitoring
- [ ] Retry attempt tracking
- [ ] Timeout and latency monitoring
- [ ] Bulkhead utilization monitoring
- [ ] Service health trend analysis
- [ ] Degradation detection and alerting
- [ ] Performance impact assessment
- [ ] User experience impact monitoring

This comprehensive resilience patterns design ensures that the Knowledge Graph Analytics Dashboard can handle failures gracefully, maintain availability during partial outages, and provide consistent user experience even when individual services are experiencing issues.