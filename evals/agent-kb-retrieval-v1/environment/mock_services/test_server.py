from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

TOKEN = "isolated-test-token"
KB_UUID = "benchmark-kb-0701"
DOC1_ID = "00000000-0000-4000-8000-000000000704"
DOC2_ID = "00000000-0000-4000-8000-000000000705"


@pytest.fixture
def server_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the container-oriented server against its adjacent fixture."""
    fixture_text = Path(__file__).with_name("fixtures.json").read_text()
    original_read_text = Path.read_text

    def isolated_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == Path("/service/fixtures.json"):
            return fixture_text
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setenv("DO_BENCHMARK_TOKEN", TOKEN)
    monkeypatch.setattr(Path, "read_text", isolated_read_text)
    spec = importlib.util.spec_from_file_location(
        f"agent_kb_retrieval_mock_{uuid4().hex}",
        Path(__file__).with_name("server.py"),
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def running_server(module: ModuleType) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request_json(
    port: int,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str = TOKEN,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        finally:
            exc.close()


def retrieve_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"query": "rate limits", "num_results": 2}
    body.update(overrides)
    return body


def test_generic_retrieve_succeeds_and_records_only_bounded_evidence(
    server_module: ModuleType,
) -> None:
    long_query = "q" * 600
    with running_server(server_module) as port:
        status, payload = request_json(
            port,
            "POST",
            f"/v1/{KB_UUID}/retrieve",
            payload=retrieve_body(query=long_query, num_results=1),
        )
        events_status, events_payload = request_json(port, "GET", "/events")

    assert status == 200
    assert [item["metadata"]["fixture_id"] for item in payload["results"]] == ["R1"]
    assert events_status == 200
    event = events_payload["events"][-1]
    assert len(event["query"]) == 512
    serialized_event = json.dumps(event)
    assert TOKEN not in serialized_event
    assert "120 requests per minute" not in serialized_event


def test_single_item_name_filter_is_applied_before_num_results(
    server_module: ModuleType,
) -> None:
    filters = {"equals": {"key": "item_name", "value": f"{DOC2_ID}.txt"}}
    with running_server(server_module) as port:
        status, payload = request_json(
            port,
            "POST",
            f"/v1/{KB_UUID}/retrieve",
            payload=retrieve_body(num_results=1, filters=filters),
        )

    assert status == 200
    assert [item["metadata"]["fixture_id"] for item in payload["results"]] == ["R2"]
    assert payload["total_results"] == 1


def test_flat_or_filter_is_applied_before_num_results(
    server_module: ModuleType,
) -> None:
    filters = {
        "or_all": [
            {
                "equals": {
                    "key": "item_name",
                    "value": "00000000-0000-4000-8000-000000009999.txt",
                }
            },
            {"equals": {"key": "item_name", "value": f"{DOC2_ID}.txt"}},
        ]
    }
    with running_server(server_module) as port:
        status, payload = request_json(
            port,
            "POST",
            f"/v1/{KB_UUID}/retrieve",
            payload=retrieve_body(num_results=1, filters=filters),
        )

    assert status == 200
    assert [item["metadata"]["fixture_id"] for item in payload["results"]] == ["R2"]


def test_unknown_item_name_is_an_empty_success(server_module: ModuleType) -> None:
    filters = {
        "equals": {
            "key": "item_name",
            "value": "00000000-0000-4000-8000-000000009999.txt",
        }
    }
    with running_server(server_module) as port:
        status, payload = request_json(
            port,
            "POST",
            f"/v1/{KB_UUID}/retrieve",
            payload=retrieve_body(filters=filters),
        )

    assert status == 200
    assert payload == {"results": [], "total_results": 0}


@pytest.mark.parametrize(
    "filters",
    [
        None,
        {"contains": {"key": "item_name", "value": f"{DOC1_ID}.txt"}},
        {"equals": {"key": "title", "value": "API Rate Limit Policy"}},
        {"equals": {"key": "item_name", "value": 123}},
        {"or_all": []},
        {
            "or_all": [
                {"equals": {"key": "item_name", "value": f"{DOC1_ID}.txt"}},
                {"or_all": []},
            ]
        },
    ],
    ids=["null", "operator", "key", "value-type", "empty-or", "nested-or"],
)
def test_malformed_or_unknown_filters_are_rejected(
    server_module: ModuleType, filters: Any
) -> None:
    with running_server(server_module) as port:
        status, payload = request_json(
            port,
            "POST",
            f"/v1/{KB_UUID}/retrieve",
            payload=retrieve_body(filters=filters),
        )

    assert status == 400
    assert payload == {"error": "invalid_request"}


def test_wrong_bearer_is_rejected(server_module: ModuleType) -> None:
    with running_server(server_module) as port:
        status, payload = request_json(
            port,
            "POST",
            f"/v1/{KB_UUID}/retrieve",
            payload=retrieve_body(),
            token="wrong-token",
        )

    assert status == 401
    assert payload == {"error": "unauthorized"}
