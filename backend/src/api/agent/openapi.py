"""Register SSE data models without claiming the HTTP stream is JSON."""

from typing import Any

from fastapi import FastAPI
from pydantic.json_schema import models_json_schema

from src.services.agent.schemas import RagContextPayload


def register_agent_stream_schemas(app: FastAPI) -> None:
    """Expose typed SSE data in components and a transport-specific extension.

    FastAPI only discovers models used by HTTP responses. StreamingResponse
    serves text/event-stream, so its individual event models need an explicit
    registration for the generated frontend contract. Pydantic still owns
    every property and reference; this adds no alternate JSON response.
    """
    original_openapi = app.openapi

    def stream_openapi() -> dict[str, Any]:
        schema: dict[str, Any] = original_openapi()
        refs, definitions = models_json_schema(
            [(RagContextPayload, "serialization")],
            ref_template="#/components/schemas/{model}",
        )
        schema.setdefault("components", {}).setdefault("schemas", {}).update(
            definitions.get("$defs", {})
        )
        for path in ("/api/v1/agent/stream", "/api/v1/agent/stream/confirm"):
            operation = schema.get("paths", {}).get(path, {}).get("post")
            if operation is not None:
                operation["x-sse-data-schemas"] = {
                    "rag_context": refs[(RagContextPayload, "serialization")]
                }
        return schema

    app.openapi = stream_openapi
