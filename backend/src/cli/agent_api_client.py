from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from uuid import uuid4

import httpx

from src.cli.agent_event_parser import parse_sse_lines
from src.cli.types import CLIEvent


class AgentAPIClientError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def build_execute_payload(
    *,
    messages: Sequence[Mapping[str, Any]],
    **rest: Any,
) -> dict[str, Any]:
    """Build an /agent/execute or /agent/stream request body.

    Injects a fresh UUID4 ``client_message_id`` onto any user message that
    doesn't already carry one. Non-user messages are returned untouched.
    """
    out_messages: list[dict[str, Any]] = []
    for msg in messages:
        copy = dict(msg)
        if copy.get("role") == "user" and "client_message_id" not in copy:
            copy["client_message_id"] = str(uuid4())
        out_messages.append(copy)
    payload: dict[str, Any] = {"messages": out_messages}
    payload.update(rest)
    return payload


def build_stream_headers(*, token: str = "") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class AgentAPIClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0),
        )
        self._headers = build_stream_headers(token=token)

    def update_auth(self, token: str) -> None:
        self._headers = build_stream_headers(token=token)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def stream_message(
        self, request_body: Mapping[str, Any]
    ) -> AsyncIterator[CLIEvent]:
        async for event in self._stream_events("/api/v1/agent/stream", request_body):
            yield event

    async def stream_confirm(
        self, *, thread_id: str, confirmed: bool
    ) -> AsyncIterator[CLIEvent]:
        async for event in self._stream_events(
            "/api/v1/agent/stream/confirm",
            {"thread_id": thread_id, "confirmed": confirmed},
        ):
            yield event

    async def start_cli_auth(self) -> dict[str, Any]:
        response = await self._client.post(
            "/api/v1/cli-auth/start",
            headers=self._headers,
        )
        response.raise_for_status()
        return response.json()

    async def get_cli_auth_status(
        self,
        session_id: str,
        poll_token: str,
    ) -> dict[str, Any]:
        response = await self._client.get(
            f"/api/v1/cli-auth/status/{session_id}",
            headers={**self._headers, "X-CLI-Poll-Token": poll_token},
        )
        response.raise_for_status()
        return response.json()

    async def _stream_events(
        self, path: str, request_body: Mapping[str, Any]
    ) -> AsyncIterator[CLIEvent]:
        async with self._client.stream(
            "POST",
            path,
            json=request_body,
            headers=self._headers,
        ) as response:
            if response.is_error:
                await self._raise_cli_error(response)

            block: list[str] = []
            async for line in response.aiter_lines():
                if line == "":
                    for event in parse_sse_lines((*block, "")):
                        yield event
                    block = []
                    continue
                block.append(line)

            if block:
                for event in parse_sse_lines((*block, "")):
                    yield event

    async def _raise_cli_error(self, response: httpx.Response) -> None:
        detail = ""
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or "")

        status_code = response.status_code
        if status_code in {401, 403}:
            raise AgentAPIClientError(
                "auth> backend rejected credentials. Run /login.",
                status_code,
            )

        if detail:
            raise AgentAPIClientError(
                f"error> request failed ({status_code}): {detail}",
                status_code,
            )

        raise AgentAPIClientError(
            f"error> request failed ({status_code})",
            status_code,
        )
