# Monitoring Resilience Patterns and Error Handling

> **Stale / historical (2026-10-17):** It is a design document for a subsystem; the described service layout may not match the current code. Verify against `backend/src/` and `backend/openapi.json` before relying on it. Current contracts: [`docs/engineering/`](../../docs/engineering/README.md).

## Overview

This document outlines comprehensive resilience patterns and error handling strategies for the Multimodal RAG System's monitoring infrastructure. The design ensures system reliability, graceful degradation, and automatic recovery from failures.

## Resilience Patterns

### 1. Circuit Breaker Pattern

```python
import asyncio
import time
from enum import Enum
from typing import Callable, Any, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Circuit is open, calls fail fast
    HALF_OPEN = "half_open"  # Testing if service has recovered

@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker"""
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    expected_exception: type = Exception
    success_threshold: int = 3
    timeout: float = 30.0

class CircuitBreaker:
    """Circuit breaker for external monitoring dependencies"""

    def __init__(self, config: CircuitBreakerConfig):
        self.config = config
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED
        self.call_count = 0
        self.lock = asyncio.Lock()

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection"""
        async with self.lock:
            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self.state = CircuitState.HALF_OPEN
                    logger.info(f"Circuit breaker transitioning to HALF_OPEN")
                else:
                    raise CircuitBreakerOpenException("Circuit breaker is OPEN")

        try:
            # Execute with timeout
            result = await asyncio.wait_for(func(*args, **kwargs), timeout=self.config.timeout)
            await self._on_success()
            return result

        except self.config.expected_exception as e:
            await self._on_failure()
            raise
        except asyncio.TimeoutError:
            await self._on_failure()
            raise CircuitBreakerTimeoutException("Call timed out")
        except Exception as e:
            await self._on_failure()
            raise

    async def _on_success(self):
        """Handle successful call"""
        async with self.lock:
            self.call_count += 1

            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.config.success_threshold:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    self.success_count = 0
                    logger.info("Circuit breaker transitioning to CLOSED")

    async def _on_failure(self):
        """Handle failed call"""
        async with self.lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.failure_count >= self.config.failure_threshold:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit breaker transitioning to OPEN after {self.failure_count} failures")

    def _should_attempt_reset(self) -> bool:
        """Check if circuit breaker should attempt reset"""
        if self.last_failure_time is None:
            return True
        return time.time() - self.last_failure_time >= self.config.recovery_timeout

    def get_stats(self) -> dict:
        """Get circuit breaker statistics"""
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "call_count": self.call_count,
            "last_failure_time": self.last_failure_time
        }

class CircuitBreakerOpenException(Exception):
    """Exception raised when circuit breaker is open"""
    pass

class CircuitBreakerTimeoutException(Exception):
    """Exception raised when call times out"""
    pass

# Circuit breaker decorator
def with_circuit_breaker(config: CircuitBreakerConfig):
    """Decorator to apply circuit breaker to function"""
    circuit_breaker = CircuitBreaker(config)

    def decorator(func):
        async def wrapper(*args, **kwargs):
            return await circuit_breaker.call(func, *args, **kwargs)
        return wrapper
    return decorator
```

### 2. Retry Pattern with Exponential Backoff

```python
import random
import math
from typing import List, Type
from functools import wraps

class RetryConfig:
    """Configuration for retry logic"""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
        retry_on: List[Type[Exception]] = None
    ):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
        self.retry_on = retry_on or [Exception]

class RetryManager:
    """Manages retry logic with exponential backoff"""

    def __init__(self, config: RetryConfig):
        self.config = config
        self.retry_stats = {}

    async def execute_with_retry(
        self,
        func: Callable,
        *args,
        operation_name: str = None,
        **kwargs
    ) -> Any:
        """Execute function with retry logic"""
        operation_name = operation_name or func.__name__

        for attempt in range(self.config.max_attempts):
            try:
                result = await func(*args, **kwargs)

                # Record success
                if operation_name not in self.retry_stats:
                    self.retry_stats[operation_name] = {"successes": 0, "failures": 0, "retries": 0}
                self.retry_stats[operation_name]["successes"] += 1

                return result

            except Exception as e:
                if not self._should_retry(e, attempt):
                    # Record failure
                    if operation_name not in self.retry_stats:
                        self.retry_stats[operation_name] = {"successes": 0, "failures": 0, "retries": 0}
                    self.retry_stats[operation_name]["failures"] += 1
                    raise

                if attempt < self.config.max_attempts - 1:
                    delay = self._calculate_delay(attempt)
                    logger.warning(
                        f"Attempt {attempt + 1} failed for {operation_name}: {e}. "
                        f"Retrying in {delay:.2f} seconds"
                    )
                    await asyncio.sleep(delay)

        # Record failure after all attempts
        if operation_name not in self.retry_stats:
            self.retry_stats[operation_name] = {"successes": 0, "failures": 0, "retries": 0}
        self.retry_stats[operation_name]["failures"] += 1
        self.retry_stats[operation_name]["retries"] += attempt

        raise MaxRetriesExceededException(
            f"Max retries ({self.config.max_attempts}) exceeded for {operation_name}"
        )

    def _should_retry(self, exception: Exception, attempt: int) -> bool:
        """Check if operation should be retried"""
        if attempt >= self.config.max_attempts - 1:
            return False

        return any(isinstance(exception, retry_type) for retry_type in self.config.retry_on)

    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay with exponential backoff and jitter"""
        delay = self.config.base_delay * (self.config.exponential_base ** attempt)
        delay = min(delay, self.config.max_delay)

        if self.config.jitter:
            # Add random jitter (±25%)
            jitter_range = delay * 0.25
            delay += random.uniform(-jitter_range, jitter_range)
            delay = max(0, delay)  # Ensure non-negative

        return delay

    def get_retry_stats(self) -> dict:
        """Get retry statistics"""
        return self.retry_stats.copy()

def with_retry(config: RetryConfig):
    """Decorator to apply retry logic to function"""
    retry_manager = RetryManager(config)

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await retry_manager.execute_with_retry(func, *args, **kwargs)
        return wrapper
    return decorator

class MaxRetriesExceededException(Exception):
    """Exception raised when max retries are exceeded"""
    pass
```

### 3. Bulkhead Pattern

```python
import asyncio
from typing import Dict, Any
from dataclasses import dataclass

@dataclass
class BulkheadConfig:
    """Configuration for bulkhead pattern"""
    max_concurrent: int = 10
    max_queue_size: int = 100
    timeout: float = 30.0

class Bulkhead:
    """Bulkhead pattern for resource isolation"""

    def __init__(self, config: BulkheadConfig):
        self.config = config
        self.semaphore = asyncio.Semaphore(config.max_concurrent)
        self.queue = asyncio.Queue(maxsize=config.max_queue_size)
        self.active_tasks = set()
        self.rejected_tasks = 0
        self.completed_tasks = 0

    async def execute(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with bulkhead protection"""
        try:
            # Add to queue
            await self.queue.put(None)

            try:
                # Acquire semaphore
                await asyncio.wait_for(self.semaphore.acquire(), timeout=self.config.timeout)

                task = asyncio.create_task(func(*args, **kwargs))
                self.active_tasks.add(task)

                try:
                    result = await task
                    self.completed_tasks += 1
                    return result
                finally:
                    self.active_tasks.discard(task)
                    self.semaphore.release()

            except asyncio.TimeoutError:
                self.rejected_tasks += 1
                raise BulkheadTimeoutException("Bulkhead timeout - too many concurrent tasks")
            finally:
                # Remove from queue
                self.queue.get_nowait()
                self.queue.task_done()

        except asyncio.QueueFull:
            self.rejected_tasks += 1
            raise BulkheadFullException("Bulkhead queue is full")

    def get_stats(self) -> Dict[str, Any]:
        """Get bulkhead statistics"""
        return {
            "active_tasks": len(self.active_tasks),
            "queue_size": self.queue.qsize(),
            "max_concurrent": self.config.max_concurrent,
            "max_queue_size": self.config.max_queue_size,
            "rejected_tasks": self.rejected_tasks,
            "completed_tasks": self.completed_tasks
        }

class BulkheadTimeoutException(Exception):
    """Exception raised when bulkhead times out"""
    pass

class BulkheadFullException(Exception):
    """Exception raised when bulkhead queue is full"""
    pass

# Bulkhead manager for different resource types
class BulkheadManager:
    """Manages multiple bulkheads for different resource types"""

    def __init__(self):
        self.bulkheads = {}

    def create_bulkhead(self, name: str, config: BulkheadConfig):
        """Create a new bulkhead"""
        self.bulkheads[name] = Bulkhead(config)

    def get_bulkhead(self, name: str) -> Bulkhead:
        """Get existing bulkhead"""
        if name not in self.bulkheads:
            raise ValueError(f"Bulkhead '{name}' not found")
        return self.bulkheads[name]

    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all bulkheads"""
        return {name: bulkhead.get_stats() for name, bulkhead in self.bulkheads.items()}
```

### 4. Timeout Pattern

```python
import asyncio
from typing import Optional, Any
from contextlib import asynccontextmanager

class TimeoutManager:
    """Manages timeouts for various operations"""

    def __init__(self):
        self.timeouts = {
            "metrics_collection": 30.0,
            "health_check": 10.0,
            "alert_processing": 60.0,
            "log_processing": 120.0,
            "slo_evaluation": 45.0,
            "dashboard_refresh": 30.0,
            "trace_processing": 60.0
        }

    def set_timeout(self, operation: str, timeout: float):
        """Set timeout for specific operation"""
        self.timeouts[operation] = timeout

    def get_timeout(self, operation: str) -> float:
        """Get timeout for operation"""
        return self.timeouts.get(operation, 30.0)

    @asynccontextmanager
    async def timeout_context(self, operation: str, timeout: Optional[float] = None):
        """Context manager for operation timeout"""
        timeout = timeout or self.get_timeout(operation)

        try:
            async with asyncio.timeout(timeout):
                yield
        except asyncio.TimeoutError:
            raise OperationTimeoutException(f"Operation '{operation}' timed out after {timeout}s")

    async def execute_with_timeout(
        self,
        func: Callable,
        operation: str,
        *args,
        timeout: Optional[float] = None,
        **kwargs
    ) -> Any:
        """Execute function with timeout"""
        timeout = timeout or self.get_timeout(operation)

        try:
            return await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
        except asyncio.TimeoutError:
            raise OperationTimeoutException(f"Operation '{operation}' timed out after {timeout}s")

class OperationTimeoutException(Exception):
    """Exception raised when operation times out"""
    pass
```

## Error Handling Strategies

### 1. Error Classification and Handling

```python
from enum import Enum
from typing import Dict, Any, List, Optional
import traceback

class ErrorSeverity(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class ErrorCategory(Enum):
    NETWORK = "network"
    DATABASE = "database"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    VALIDATION = "validation"
    SYSTEM = "system"
    BUSINESS = "business"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"

@dataclass
class ErrorContext:
    """Context information for error handling"""
    operation: str
    service_name: str
    user_id: Optional[str] = None
    organization_id: Optional[str] = None
    request_id: Optional[str] = None
    correlation_id: Optional[str] = None
    additional_data: Optional[Dict[str, Any]] = None

class MonitoringErrorHandler:
    """Comprehensive error handling for monitoring system"""

    def __init__(self):
        self.error_handlers = {}
        self.error_stats = {}
        self.setup_default_handlers()

    def setup_default_handlers(self):
        """Setup default error handlers"""
        self.register_handler(
            ConnectionError,
            self._handle_connection_error,
            ErrorCategory.NETWORK,
            ErrorSeverity.HIGH
        )

        self.register_handler(
            TimeoutError,
            self._handle_timeout_error,
            ErrorCategory.TIMEOUT,
            ErrorSeverity.MEDIUM
        )

        self.register_handler(
            ValueError,
            self._handle_validation_error,
            ErrorCategory.VALIDATION,
            ErrorSeverity.LOW
        )

        self.register_handler(
            PermissionError,
            self._handle_authorization_error,
            ErrorCategory.AUTHORIZATION,
            ErrorSeverity.MEDIUM
        )

        self.register_handler(
            Exception,
            self._handle_generic_error,
            ErrorCategory.SYSTEM,
            ErrorSeverity.HIGH
        )

    def register_handler(
        self,
        exception_type: type,
        handler_func: Callable,
        category: ErrorCategory,
        severity: ErrorSeverity
    ):
        """Register error handler"""
        self.error_handlers[exception_type] = {
            "handler": handler_func,
            "category": category,
            "severity": severity
        }

    async def handle_error(
        self,
        exception: Exception,
        context: ErrorContext
    ) -> Dict[str, Any]:
        """Handle error with appropriate strategy"""
        # Find appropriate handler
        handler_info = self._find_handler(type(exception))

        if not handler_info:
            handler_info = self.error_handlers[Exception]

        # Record error statistics
        self._record_error_stats(handler_info["category"], exception)

        # Execute handler
        try:
            result = await handler_info["handler"](exception, context)
            result["category"] = handler_info["category"].value
            result["severity"] = handler_info["severity"].value
            return result
        except Exception as handler_error:
            logger.error(f"Error in error handler: {handler_error}")
            return self._create_fallback_error_response(exception, context)

    def _find_handler(self, exception_type: type) -> Optional[Dict]:
        """Find appropriate handler for exception type"""
        # Look for exact match
        if exception_type in self.error_handlers:
            return self.error_handlers[exception_type]

        # Look for parent class match
        for exc_type, handler in self.error_handlers.items():
            if issubclass(exception_type, exc_type):
                return handler

        return None

    async def _handle_connection_error(
        self,
        exception: ConnectionError,
        context: ErrorContext
    ) -> Dict[str, Any]:
        """Handle connection errors"""
        logger.error(f"Connection error in {context.operation}: {exception}")

        return {
            "error_type": "connection_error",
            "message": "Service temporarily unavailable",
            "retry_after": 30,
            "should_retry": True,
            "max_retries": 3,
            "context": context.__dict__
        }

    async def _handle_timeout_error(
        self,
        exception: TimeoutError,
        context: ErrorContext
    ) -> Dict[str, Any]:
        """Handle timeout errors"""
        logger.warning(f"Timeout in {context.operation}: {exception}")

        return {
            "error_type": "timeout_error",
            "message": "Operation timed out",
            "retry_after": 60,
            "should_retry": True,
            "max_retries": 2,
            "context": context.__dict__
        }

    async def _handle_validation_error(
        self,
        exception: ValueError,
        context: ErrorContext
    ) -> Dict[str, Any]:
        """Handle validation errors"""
        logger.warning(f"Validation error in {context.operation}: {exception}")

        return {
            "error_type": "validation_error",
            "message": "Invalid input data",
            "should_retry": False,
            "details": str(exception),
            "context": context.__dict__
        }

    async def _handle_authorization_error(
        self,
        exception: PermissionError,
        context: ErrorContext
    ) -> Dict[str, Any]:
        """Handle authorization errors"""
        logger.warning(f"Authorization error in {context.operation}: {exception}")

        return {
            "error_type": "authorization_error",
            "message": "Insufficient permissions",
            "should_retry": False,
            "context": context.__dict__
        }

    async def _handle_generic_error(
        self,
        exception: Exception,
        context: ErrorContext
    ) -> Dict[str, Any]:
        """Handle generic errors"""
        logger.error(f"Unexpected error in {context.operation}: {exception}", exc_info=True)

        return {
            "error_type": "system_error",
            "message": "Internal system error",
            "should_retry": True,
            "retry_after": 300,
            "context": context.__dict__
        }

    def _create_fallback_error_response(
        self,
        exception: Exception,
        context: ErrorContext
    ) -> Dict[str, Any]:
        """Create fallback error response"""
        return {
            "error_type": "fallback_error",
            "message": "An error occurred",
            "should_retry": False,
            "context": context.__dict__
        }

    def _record_error_stats(self, category: ErrorCategory, exception: Exception):
        """Record error statistics"""
        error_key = f"{category.value}:{type(exception).__name__}"

        if error_key not in self.error_stats:
            self.error_stats[error_key] = {
                "count": 0,
                "first_occurrence": time.time(),
                "last_occurrence": None
            }

        self.error_stats[error_key]["count"] += 1
        self.error_stats[error_key]["last_occurrence"] = time.time()

    def get_error_stats(self) -> Dict[str, Any]:
        """Get error statistics"""
        return self.error_stats.copy()
```

### 2. Graceful Degradation Strategies

```python
from typing import List, Dict, Any, Optional
from enum import Enum

class DegradationLevel(Enum):
    FULL = "full"
    DEGRADED = "degraded"
    MINIMAL = "minimal"
    UNAVAILABLE = "unavailable"

class DegradationStrategy:
    """Base class for degradation strategies"""

    async def execute(self, operation: str, context: ErrorContext) -> Any:
        """Execute degraded operation"""
        raise NotImplementedError

class CacheFallbackStrategy(DegradationStrategy):
    """Fallback to cached data when service is unavailable"""

    def __init__(self, cache_manager, max_age_seconds: int = 300):
        self.cache_manager = cache_manager
        self.max_age_seconds = max_age_seconds

    async def execute(self, operation: str, context: ErrorContext) -> Any:
        """Return cached data"""
        cache_key = f"fallback:{operation}:{context.organization_id}"
        cached_data = await self.cache_manager.get(cache_key)

        if cached_data:
            logger.info(f"Returning cached data for {operation}")
            return {
                "data": cached_data,
                "degradation_level": DegradationLevel.DEGRADED.value,
                "source": "cache",
                "timestamp": time.time()
            }

        return {
            "data": None,
            "degradation_level": DegradationLevel.UNAVAILABLE.value,
            "source": "none",
            "timestamp": time.time()
        }

class DefaultDataStrategy(DegradationStrategy):
    """Return default/predefined data"""

    def __init__(self, default_data: Dict[str, Any]):
        self.default_data = default_data

    async def execute(self, operation: str, context: ErrorContext) -> Any:
        """Return default data"""
        logger.info(f"Returning default data for {operation}")

        return {
            "data": self.default_data.get(operation, {}),
            "degradation_level": DegradationLevel.MINIMAL.value,
            "source": "default",
            "timestamp": time.time()
        }

class GracefulDegradationManager:
    """Manages graceful degradation for monitoring operations"""

    def __init__(self):
        self.strategies = {}
        self.degradation_status = {}
        self.setup_default_strategies()

    def setup_default_strategies(self):
        """Setup default degradation strategies"""
        # Metrics fallback
        self.register_strategy(
            "get_metrics",
            [CacheFallbackStrategy(cache_manager), DefaultDataStrategy({"get_metrics": []})]
        )

        # Health check fallback
        self.register_strategy(
            "health_check",
            [DefaultDataStrategy({"health_check": {"status": "unknown"}})]
        )

        # SLO fallback
        self.register_strategy(
            "get_sli",
            [CacheFallbackStrategy(cache_manager), DefaultDataStrategy({"get_sli": {"sli_value": 0.0}})]
        )

    def register_strategy(self, operation: str, strategies: List[DegradationStrategy]):
        """Register degradation strategies for operation"""
        self.strategies[operation] = strategies

    async def execute_with_fallback(
        self,
        operation: str,
        primary_func: Callable,
        context: ErrorContext,
        *args,
        **kwargs
    ) -> Any:
        """Execute operation with fallback strategies"""
        try:
            # Try primary function first
            result = await primary_func(*args, **kwargs)
            self.degradation_status[operation] = DegradationLevel.FULL.value
            return result

        except Exception as e:
            logger.warning(f"Primary operation {operation} failed: {e}")

            # Try fallback strategies
            if operation in self.strategies:
                for strategy in self.strategies[operation]:
                    try:
                        result = await strategy.execute(operation, context)
                        self.degradation_status[operation] = result.get("degradation_level", "unknown")
                        return result
                    except Exception as strategy_error:
                        logger.error(f"Fallback strategy failed: {strategy_error}")
                        continue

            # All strategies failed
            self.degradation_status[operation] = DegradationLevel.UNAVAILABLE.value
            raise ServiceUnavailableException(f"All fallback strategies failed for {operation}")

    def get_degradation_status(self) -> Dict[str, str]:
        """Get current degradation status"""
        return self.degradation_status.copy()

class ServiceUnavailableException(Exception):
    """Exception raised when service is unavailable"""
    pass
```

### 3. Error Recovery Automation

```python
import asyncio
from typing import Dict, List, Callable, Any
from datetime import datetime, timedelta

class RecoveryAction:
    """Base class for recovery actions"""

    def __init__(self, name: str, priority: int = 0):
        self.name = name
        self.priority = priority

    async def execute(self, context: Dict[str, Any]) -> bool:
        """Execute recovery action"""
        raise NotImplementedError

class ServiceRestartAction(RecoveryAction):
    """Restart service recovery action"""

    def __init__(self, service_name: str):
        super().__init__(f"restart_service_{service_name}", priority=10)
        self.service_name = service_name

    async def execute(self, context: Dict[str, Any]) -> bool:
        """Restart service"""
        try:
            logger.info(f"Attempting to restart service {self.service_name}")
            # Implement service restart logic
            # await docker.restart_container(self.service_name)
            return True
        except Exception as e:
            logger.error(f"Failed to restart service {self.service_name}: {e}")
            return False

class CacheClearAction(RecoveryAction):
    """Clear cache recovery action"""

    def __init__(self, cache_manager):
        super().__init__("clear_cache", priority=5)
        self.cache_manager = cache_manager

    async def execute(self, context: Dict[str, Any]) -> bool:
        """Clear cache"""
        try:
            logger.info("Clearing monitoring cache")
            await self.cache_manager.clear_all()
            return True
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            return False

class DatabaseReconnectAction(RecoveryAction):
    """Reconnect to database recovery action"""

    def __init__(self, db_connection):
        super().__init__("database_reconnect", priority=8)
        self.db_connection = db_connection

    async def execute(self, context: Dict[str, Any]) -> bool:
        """Reconnect to database"""
        try:
            logger.info("Attempting to reconnect to database")
            await self.db_connection.reconnect()
            return True
        except Exception as e:
            logger.error(f"Failed to reconnect to database: {e}")
            return False

class ErrorRecoveryManager:
    """Manages automated error recovery"""

    def __init__(self):
        self.recovery_actions = []
        self.recovery_history = {}
        self.active_recoveries = set()

    def register_action(self, action: RecoveryAction):
        """Register recovery action"""
        self.recovery_actions.append(action)
        self.recovery_actions.sort(key=lambda x: x.priority, reverse=True)

    async def handle_error_with_recovery(
        self,
        error: Exception,
        context: ErrorContext,
        error_category: ErrorCategory
    ) -> Dict[str, Any]:
        """Handle error with automatic recovery"""
        recovery_key = f"{context.service_name}:{error_category.value}"

        # Check if recovery is already in progress
        if recovery_key in self.active_recoveries:
            return {
                "recovery_status": "in_progress",
                "message": "Recovery already in progress"
            }

        # Get appropriate recovery actions
        actions = self._get_recovery_actions(error_category)

        if not actions:
            return {
                "recovery_status": "no_actions",
                "message": "No recovery actions available"
            }

        # Execute recovery actions
        self.active_recoveries.add(recovery_key)

        try:
            for action in actions:
                logger.info(f"Executing recovery action: {action.name}")

                success = await action.execute({
                    "error": error,
                    "context": context,
                    "error_category": error_category
                })

                # Record recovery attempt
                self._record_recovery_attempt(recovery_key, action.name, success)

                if success:
                    logger.info(f"Recovery action {action.name} succeeded")
                    return {
                        "recovery_status": "success",
                        "action": action.name,
                        "message": f"Recovery successful using {action.name}"
                    }
                else:
                    logger.warning(f"Recovery action {action.name} failed")

            # All recovery actions failed
            return {
                "recovery_status": "failed",
                "message": "All recovery actions failed"
            }

        finally:
            self.active_recoveries.discard(recovery_key)

    def _get_recovery_actions(self, error_category: ErrorCategory) -> List[RecoveryAction]:
        """Get recovery actions for error category"""
        # Map error categories to recovery actions
        action_mapping = {
            ErrorCategory.DATABASE: ["database_reconnect"],
            ErrorCategory.NETWORK: ["cache_clear"],
            ErrorCategory.SYSTEM: ["restart_service_*"],
        }

        matching_actions = []
        patterns = action_mapping.get(error_category, [])

        for action in self.recovery_actions:
            for pattern in patterns:
                if pattern.endswith("*"):
                    if action.name.startswith(pattern[:-1]):
                        matching_actions.append(action)
                        break
                elif action.name == pattern:
                    matching_actions.append(action)
                    break

        return matching_actions

    def _record_recovery_attempt(self, recovery_key: str, action_name: str, success: bool):
        """Record recovery attempt"""
        if recovery_key not in self.recovery_history:
            self.recovery_history[recovery_key] = []

        self.recovery_history[recovery_key].append({
            "action": action_name,
            "success": success,
            "timestamp": datetime.utcnow().isoformat()
        })

        # Keep only last 10 attempts
        self.recovery_history[recovery_key] = self.recovery_history[recovery_key][-10:]

    def get_recovery_history(self, recovery_key: str = None) -> Dict[str, List[Dict]]:
        """Get recovery history"""
        if recovery_key:
            return {recovery_key: self.recovery_history.get(recovery_key, [])}
        return self.recovery_history.copy()

# Integration example
class ResilientMonitoringService:
    """Resilient monitoring service with all patterns integrated"""

    def __init__(self):
        self.circuit_breakers = {}
        self.retry_managers = {}
        self.bulkheads = {}
        self.error_handler = MonitoringErrorHandler()
        self.degradation_manager = GracefulDegradationManager()
        self.recovery_manager = ErrorRecoveryManager()
        self.timeout_manager = TimeoutManager()

        # Setup recovery actions
        self.recovery_manager.register_action(CacheClearAction(cache_manager))
        self.recovery_manager.register_action(DatabaseReconnectAction(db_connection))

    async def get_metrics_with_resilience(
        self,
        service_name: str,
        context: ErrorContext
    ) -> Dict[str, Any]:
        """Get metrics with all resilience patterns"""
        operation = f"get_metrics_{service_name}"

        try:
            # Execute with bulkhead protection
            bulkhead = self._get_bulkhead("metrics_collection")

            return await bulkhead.execute(
                self._get_metrics_with_circuit_breaker,
                service_name,
                context
            )

        except Exception as e:
            # Handle error with recovery
            error_response = await self.error_handler.handle_error(e, context)

            # Attempt recovery
            recovery_result = await self.recovery_manager.handle_error_with_recovery(
                e, context, ErrorCategory(error_response["category"])
            )

            # Try degraded operation
            degraded_result = await self.degradation_manager.execute_with_fallback(
                operation,
                lambda: {"error": str(e)},
                context
            )

            return {
                "error": error_response,
                "recovery": recovery_result,
                "data": degraded_result
            }

    async def _get_metrics_with_circuit_breaker(
        self,
        service_name: str,
        context: ErrorContext
    ) -> Dict[str, Any]:
        """Get metrics with circuit breaker and retry"""
        circuit_breaker = self._get_circuit_breaker(service_name)
        retry_manager = self._get_retry_manager("metrics_collection")

        return await retry_manager.execute_with_retry(
            circuit_breaker.call,
            self._fetch_metrics,
            service_name,
            context,
            operation_name="fetch_metrics"
        )

    def _get_circuit_breaker(self, service_name: str) -> CircuitBreaker:
        """Get or create circuit breaker for service"""
        if service_name not in self.circuit_breakers:
            config = CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout=60.0,
                timeout=30.0
            )
            self.circuit_breakers[service_name] = CircuitBreaker(config)
        return self.circuit_breakers[service_name]

    def _get_retry_manager(self, operation: str) -> RetryManager:
        """Get or create retry manager for operation"""
        if operation not in self.retry_managers:
            config = RetryConfig(
                max_attempts=3,
                base_delay=1.0,
                max_delay=30.0
            )
            self.retry_managers[operation] = RetryManager(config)
        return self.retry_managers[operation]

    def _get_bulkhead(self, resource_type: str) -> Bulkhead:
        """Get or create bulkhead for resource type"""
        if resource_type not in self.bulkheads:
            config = BulkheadConfig(
                max_concurrent=10,
                max_queue_size=100,
                timeout=30.0
            )
            self.bulkheads[resource_type] = Bulkhead(config)
        return self.bulkheads[resource_type]

    async def _fetch_metrics(self, service_name: str, context: ErrorContext) -> Dict[str, Any]:
        """Actually fetch metrics from service"""
        # Implement actual metrics fetching logic
        await asyncio.sleep(0.1)  # Simulate API call
        return {"metrics": [{"name": "cpu_usage", "value": 75.5}]}
```

This comprehensive resilience and error handling design ensures the monitoring system can gracefully handle failures, automatically recover from issues, and maintain service availability even under adverse conditions. The combination of circuit breakers, retries, bulkheads, and graceful degradation creates a robust monitoring infrastructure that can withstand various failure scenarios.