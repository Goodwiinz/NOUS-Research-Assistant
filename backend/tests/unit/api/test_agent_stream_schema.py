"""The generated PDF metadata contract describes SSE data, not a JSON body."""

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from src.api.agent.openapi import register_agent_stream_schemas


def test_sse_payload_references_are_resolvable_without_json_response() -> None:
    app = FastAPI()

    @app.post(
        "/api/v1/agent/stream",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    def stream() -> StreamingResponse:
        return StreamingResponse(iter(()), media_type="text/event-stream")

    register_agent_stream_schemas(app)
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/agent/stream"]["post"]
    assert operation["x-sse-data-schemas"]["rag_context"] == {
        "$ref": "#/components/schemas/RagContextPayload"
    }
    assert "application/json" not in operation["responses"]["200"]["content"]
    models = schema["components"]["schemas"]
    assert "accepted_pages" in models["PageFilterResponse"]["properties"]
    assert "selection_page_range" in models["PageFilterResponse"]["properties"]
    assert app.openapi() == schema

    def check_refs(value: object) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/components/schemas/")
                assert value["$ref"].rsplit("/", 1)[1] in models
            for item in value.values():
                check_refs(item)
        elif isinstance(value, list):
            for item in value:
                check_refs(item)

    check_refs(schema)
