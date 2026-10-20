# Observability

Telemetry package for the NOUS backend. Three pillars — metrics, tracing, structured logging — wired together via `integration.py` and mounted on the FastAPI app at startup.

## What is instrumented

- Every HTTP request (latency, status, user role) via `ObservabilityMiddleware`
- SQLAlchemy query execution via SQLAlchemy engine events
- Redis commands via client-method wrapping
- Outbound `httpx` calls via client-method wrapping
- Document processing pipeline (queue depth, per-file duration, entity/embedding counts)
- RAG quality scores (relevancy, faithfulness, contextual relevancy) against SLO thresholds
- Search queries (duration, result count, precision/recall)
- ML inference (model name, duration, confidence)
- System resources (CPU, memory, disk, network) collected every 30 s
- SLO compliance evaluated continuously; alert callbacks fire on status transitions

## Key files

| File                                   | Purpose                                                                                                                                                                                                                                                                  |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `config.py`                            | `ObservabilityConfig` (Pydantic settings) — OTEL endpoint, log level, Prometheus port, SLO targets                                                                                                                                                                       |
| `integration.py`                       | `setup_observability(app)` entry point; mounts middleware, `/metrics`, `/health/observability`                                                                                                                                                                           |
| `instrumentation.py`                   | `ObservabilityMiddleware`, `DatabaseInstrumentation`, `RedisInstrumentation`, `HTTPClientInstrumentation`; `instrument_app()` / `instrument_services()` helpers                                                                                                          |
| `metrics.py`                           | OpenTelemetry `MeterProvider` with dual export: Prometheus scrape + OTLP push (15 s interval); `MetricsRegistry`, `PerformanceTracker`, `record_rag_metrics()`, `record_search_metrics()`                                                                                |
| `prometheus_metrics.py`                | `PrometheusMetricsCollector` — full `prometheus_client` metric definitions for HTTP, documents, WebSocket, DB, cache, search, ML, system, and business dimensions                                                                                                        |
| `tracer.py`                            | OpenTelemetry `TracerProvider` → OTLP gRPC exporter (Jaeger port 4317); B3 propagation; `trace_span`, `async_trace_span`, `@trace_function`, `@trace_async_function` decorators; correlation-ID helpers                                                                  |
| `logging.py`                           | structlog pipeline with JSON output; `StructuredFormatter` injects `trace_id`, `span_id`, `correlation_id`, `user_id`, `tenant_id` into every record; `correlation_context()` context manager; `log_security_event()`, `log_performance_event()`, `log_business_event()` |
| `sentry.py`                            | `init_sentry()` — gated on `SENTRY_DSN`; integrates FastAPI, SQLAlchemy, Redis, httpx, asyncio, stdlib logging; scrubs `Authorization`/`Cookie`/`x-api-key` before send                                                                                                  |
| `slo_monitoring.py`                    | `SLOMonitor` with `SLOStatus` state machine (compliant → warning → violation → critical); feeds `record_slo_metrics()` called per request in middleware                                                                                                                  |
| `structured_logging.py`                | Alternative structured logger with `LogCategory` enum and Prometheus counters for log-level cardinality                                                                                                                                                                  |
| `performance_optimization.py`          | Connection pool manager, cache manager                                                                                                                                                                                                                                   |
| `celery_queue_metrics.py`              | Prometheus collector exporting Celery broker (Redis) queue depths, the backlog signal for autoscaling                                                                                                                                                                    |
| `performance_testing.py`               | In-process load-test runner; `RAGPerformanceTestSuite`                                                                                                                                                                                                                   |

## Prometheus metrics

The `/metrics` endpoint (registered by `setup_observability`) serves all metrics from the default `REGISTRY`. Key metric names:

**HTTP**

- `http_requests_total` — labels: `method`, `endpoint`, `status_code`, `user_role`
- `http_request_duration_seconds` — histogram, same labels
- `http_request_size_bytes` / `http_response_size_bytes`

**Documents**

- `document_processing_total` — labels: `file_type`, `status`, `user_role`
- `document_processing_duration_seconds` — labels: `file_type`, `stage`, `status`
- `document_processing_queue_size` / `document_processing_active_jobs` — gauges
- `entities_extracted_total` / `embeddings_generated_total`

**Agent / RAG**

- `agent_execution_duration_seconds` — recorded via `PerformanceTracker`
- `agent_tool_calls_total`
- `rag_answer_relevancy_score` / `rag_faithfulness_score` / `rag_contextual_relevancy_score` — histograms, SLO thresholds 0.7 / 0.9 / 0.7

**Search**

- `search_queries_total` / `search_query_duration_seconds` / `search_results_total`
- `search_precision_score` / `search_recall_score`

**Infrastructure**

- `database_query_duration_seconds` / `database_query_total` / `database_connections_active`
- `cache_operations_total` / `cache_hit_ratio`
- `ml_inference_duration_seconds` / `ml_inference_requests_total`
- `system_cpu_percent` / `system_memory_bytes` / `system_disk_bytes`
- `websocket_connections_total` / `websocket_connections_active` / `websocket_messages_total`

Prometheus scrapes `:8000/metrics` (or the `prometheus_port` configured in `config.py`, default 9090 for a standalone server). The OTLP exporter pushes to `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://jaeger:4317`) every 15 s.

## Distributed tracing

Spans are exported via OTLP gRPC to Jaeger. B3 multi-format propagation is used for cross-service headers. Every span carries `service.name`, `service.version`, `deployment.environment`, and `service.instance.id` from the OTel resource.

Use `trace_span` / `async_trace_span` for ad-hoc spans, or decorate functions with `@trace_function` / `@trace_async_function`. Correlation IDs are injected into OTel baggage and propagated outbound via `get_trace_headers()`.

## Structured logging conventions

All application code should call `get_logger(__name__)` from this package (returns a `structlog.BoundLogger`). The pipeline outputs JSON with these standard fields on every record:

```
timestamp  level  logger  message  service  environment  version
correlation_id  request_id  user_id  tenant_id  trace_id  span_id
```

Wrap request-scoped context in `correlation_context(correlation_id, user_id, ...)` — it sets `contextvars` and OTel baggage for the duration. Third-party loggers (`uvicorn.access`, `sqlalchemy.engine`, `httpx`, `opentelemetry`) are suppressed to WARNING or higher.

## Sentry

Call `init_sentry()` before constructing the FastAPI app. It is a no-op when `SENTRY_DSN` is absent. Traces sample rate defaults to 0.1 in production, 1.0 elsewhere. Auth tokens and cookies are stripped by a `before_send` hook.

## SLO targets (defaults)

| SLO               | Target   |
| ----------------- | -------- |
| p95 response time | 3 000 ms |
| p99 response time | 5 000 ms |
| Error rate        | ≤ 0.5 %  |
| Availability      | ≥ 99.5 % |

Override via environment variables (`SLO_RESPONSE_TIME_P95_TARGET`, etc.).
