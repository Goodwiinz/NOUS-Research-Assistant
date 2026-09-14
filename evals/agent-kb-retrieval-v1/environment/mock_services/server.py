#!/usr/bin/env python3
"""Deterministic DO Knowledge Base retrieve protocol simulator.

Trimmed clone of rag-retrieval-safety-grounding-v1's mock_services/server.py:
same GET /events request-event recorder and POST /v1/{kb_uuid}/retrieve
(Bearer check, num_results 1-100), minus the Cohere /rerank endpoint this task
does not exercise (AGENT_DOKB_COHERE_RERANK stays off).
"""

from __future__ import annotations

import json
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURE = json.loads(Path("/service/fixtures.json").read_text())
RECORDS = list(FIXTURE["records"])
DO_TOKEN = os.environ.get("DO_BENCHMARK_TOKEN", "")
EVENTS: list[dict[str, Any]] = []
EVENT_LOCK = threading.Lock()


def record_event(event: dict[str, Any]) -> None:
    with EVENT_LOCK:
        EVENTS.append({"sequence": len(EVENTS) + 1, **event})


def _item_name_from_equals(clause: Any) -> str:
    if not isinstance(clause, dict) or set(clause) != {"equals"}:
        raise ValueError("filter clause must contain only equals")
    equals = clause["equals"]
    if not isinstance(equals, dict) or set(equals) != {"key", "value"}:
        raise ValueError("equals must contain only key and value")
    if equals["key"] != "item_name" or not isinstance(equals["value"], str):
        raise ValueError("equals supports only string item_name values")
    return equals["value"]


def item_names_from_filters(filters: Any) -> set[str]:
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")
    if set(filters) == {"equals"}:
        return {_item_name_from_equals(filters)}
    if set(filters) == {"or_all"}:
        clauses = filters["or_all"]
        if not isinstance(clauses, list) or not clauses:
            raise ValueError("or_all must be a non-empty list")
        return {_item_name_from_equals(clause) for clause in clauses}
    raise ValueError("unrecognized filter operator")


class Handler(BaseHTTPRequestHandler):
    server_version = "NOUSBenchmarkMock/1.0"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 100_000:
            raise ValueError("invalid body length")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def bearer_is(self, expected: str) -> bool:
        return self.headers.get("Authorization") == f"Bearer {expected}"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/health":
            self.send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path == "/events":
            with EVENT_LOCK:
                snapshot = list(EVENTS)
            self.send_json(HTTPStatus.OK, {"events": snapshot})
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        retrieve_match = re.fullmatch(r"/v1/([^/]+)/retrieve", self.path)
        if retrieve_match:
            self.handle_retrieve(retrieve_match.group(1))
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def handle_retrieve(self, kb_uuid: str) -> None:
        auth_valid = self.bearer_is(DO_TOKEN)
        if not auth_valid:
            record_event({"kind": "do_retrieve", "authorization_valid": False})
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            body = self.read_json()
            query = body["query"]
            num_results = int(body["num_results"])
            alpha = body.get("alpha")
            if kb_uuid != FIXTURE["kb_uuid"]:
                raise ValueError("unknown knowledge base")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("query is required")
            if not 1 <= num_results <= 100:
                raise ValueError("num_results out of range")
            if set(body) - {"query", "num_results", "alpha", "filters"}:
                raise ValueError("unrecognized field")
            allowed_item_names = (
                item_names_from_filters(body["filters"]) if "filters" in body else None
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            record_event(
                {
                    "kind": "do_retrieve",
                    "authorization_valid": True,
                    "accepted": False,
                    "error_type": type(exc).__name__,
                }
            )
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return

        # Deterministic, rank-order response like the production DO KB mock in
        # rag-retrieval-safety-grounding-v1 -- this simulator does not
        # reproduce real embedding relevance (see task.md "Fidelity limits").
        # The near-boundary empty-KB trial sends a query containing the fixed
        # sentinel below so it can get a genuine empty *success* without a
        # second mock endpoint or a relevance model.
        no_match = "no such policy exists" in query.casefold()
        eligible_records = (
            RECORDS
            if allowed_item_names is None
            else [
                record
                for record in RECORDS
                if f"{record['document_id']}.txt" in allowed_item_names
            ]
        )
        selected = [] if no_match else eligible_records[:num_results]
        results = [
            {
                "text_content": record["text"],
                "metadata": {
                    "item_name": f"{record['document_id']}.txt",
                    "title": record["title"],
                    "fixture_id": record["id"],
                },
            }
            for record in selected
        ]
        record_event(
            {
                "kind": "do_retrieve",
                "authorization_valid": True,
                "accepted": True,
                "kb_uuid": kb_uuid,
                "query": query[:512],
                "num_results": num_results,
                "alpha": alpha,
                "response_record_ids": [record["id"] for record in selected],
                "response_count": len(selected),
            }
        )
        self.send_json(
            HTTPStatus.OK,
            {"results": results, "total_results": len(selected)},
        )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
