"""Base classes for source connectors."""

import asyncio
import hashlib
import inspect
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional
from uuid import UUID, uuid4, uuid5


@dataclass
class SourceDocument:
    """A document retrieved from a source connector."""

    connector_type: str
    external_id: Optional[str] = None
    title: str = ""
    authors: List[str] = field(default_factory=list)
    abstract: Optional[str] = None
    url: Optional[str] = None
    full_text: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    content_hash: Optional[str] = None

    def compute_hash(self) -> None:
        """Compute SHA-256 hash of title + abstract + full_text."""
        parts = [
            self.title or "",
            self.abstract or "",
            self.full_text or "",
        ]
        combined = "".join(parts)
        self.content_hash = hashlib.sha256(combined.encode()).hexdigest()


_SECRET_KEY = re.compile(
    r"(?:api.?key|access.?token|authorization|password|secret|mailto|email)", re.I
)


def redact_search_values(value: Any, key: str = "") -> Any:
    """Keep useful search intent while excluding credentials and contact data."""
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): redact_search_values(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_search_values(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclass
class SearchTrace:
    """Mutable per-provider trace accumulated while a connector pages results."""

    execution_id: str
    provider: str
    requested_limit: int
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    pages: List[Dict[str, Any]] = field(default_factory=list)
    documents: List[SourceDocument] = field(default_factory=list)
    error_type: Optional[str] = None
    pending_request: Optional[Dict[str, Any]] = None
    attempt_id: str = field(default_factory=lambda: str(uuid4()))
    on_page_update: Optional[Callable[[str, str, Dict[str, Any]], Awaitable[None]]] = (
        field(default=None, repr=False)
    )
    pending_page_index: Optional[int] = None

    async def begin_request(self, endpoint: str, params: Dict[str, Any]) -> None:
        """Retain the literal safe request before network or local I/O begins."""
        self.pending_request = {
            "method": "GET",
            "endpoint": endpoint,
            "params": redact_search_values(params),
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        page_index = len(self.pages)
        self.pending_page_index = page_index
        page = {
            "page_id": str(uuid5(UUID(self.execution_id), f"page:{page_index}")),
            "page_index": page_index,
            "attempt_id": self.attempt_id,
            "page_status": "requested",
            "request": self.pending_request,
            "response": {
                "status_code": None,
                "received_at": None,
                "parsed_count": 0,
                "has_more": None,
                "request_attempts": [],
            },
            "record_keys": [],
            "imported_count": 0,
            "imported_source_ids": [],
        }
        self.pages.append(page)
        await self._publish_page(page)

    async def record_response(self, response: Any) -> None:
        """Persist response receipt before connector parsing can fail or time out."""
        if self.pending_page_index is None:
            return
        page = self.pages[self.pending_page_index]
        extensions = getattr(response, "extensions", {}) or {}
        page["response"].update(
            {
                "status_code": getattr(response, "status_code", None),
                "received_at": datetime.now(timezone.utc).isoformat(),
                "attempt": extensions.get("search_request_attempt", 1),
                "request_attempts": extensions.get("search_request_attempts", []),
            }
        )
        await self._publish_page(page)

    async def _publish_page(self, page: Dict[str, Any]) -> None:
        if self.on_page_update is not None:
            await self.on_page_update(self.provider, self.execution_id, dict(page))

    async def record_failed_request(
        self,
        error_type: Optional[str] = None,
        *,
        attempts: int = 1,
        attempt_history: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Keep the attempted request when it failed before a page was parsed."""
        if self.pending_page_index is None:
            return
        page = self.pages[self.pending_page_index]
        page["page_status"] = "failed"
        page["response"].update(
            {
                "received_at": datetime.now(timezone.utc).isoformat(),
                "error_type": error_type or self.error_type,
                "attempts": attempts,
                "request_attempts": attempt_history
                or page["response"].get("request_attempts", []),
            }
        )
        await self._publish_page(page)
        self.pending_request = None
        self.pending_page_index = None

    async def mark_interrupted(self) -> None:
        """Keep the outstanding page when a task is cancelled mid-request."""
        if self.pending_page_index is None:
            return
        page = self.pages[self.pending_page_index]
        page["page_status"] = "interrupted"
        page["response"]["error_type"] = "InterruptedError"
        page["response"]["received_at"] = datetime.now(timezone.utc).isoformat()
        await self._publish_page(page)
        self.pending_request = None
        self.pending_page_index = None

    async def record_page(
        self,
        *,
        endpoint: Optional[str],
        params: Dict[str, Any],
        response: Any = None,
        documents: Optional[List[SourceDocument]] = None,
        total_available: Optional[int] = None,
        next_cursor: Any = None,
        has_more: Optional[bool] = None,
        requested_at: Optional[str] = None,
        provider_count: Optional[int] = None,
        error_type: Optional[str] = None,
    ) -> None:
        page_documents = documents or []
        if self.pending_page_index is None:
            await self.begin_request(endpoint or "unknown", params)
        page_index = self.pending_page_index
        assert page_index is not None
        response_extensions = getattr(response, "extensions", {}) or {}
        pending_request = self.pending_request or {}
        safe_cursor = redact_search_values({"cursor": next_cursor}).get("cursor")
        if isinstance(safe_cursor, str) and len(safe_cursor) > 512:
            safe_cursor = f"sha256:{hashlib.sha256(safe_cursor.encode()).hexdigest()}"
        page = self.pages[page_index]
        page.update(
            {
                "page_status": "failed" if error_type else "completed",
                "request": {
                    **page.get("request", {}),
                    "method": "GET",
                    "endpoint": endpoint or pending_request.get("endpoint"),
                    "params": redact_search_values(
                        params or pending_request.get("params", {})
                    ),
                    "requested_at": (
                        requested_at
                        or response_extensions.get("search_request_started_at")
                        or pending_request.get("requested_at")
                        or datetime.now(timezone.utc).isoformat()
                    ),
                },
                "response": {
                    **page.get("response", {}),
                    "status_code": getattr(response, "status_code", None),
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "attempt": response_extensions.get("search_request_attempt", 1),
                    "request_attempts": response_extensions.get(
                        "search_request_attempts", []
                    ),
                    "error_type": error_type,
                    "provider_count": provider_count,
                    "parsed_count": len(page_documents),
                    "total_available": total_available,
                    "has_more": has_more,
                    "next_cursor": safe_cursor,
                },
                "record_keys": [
                    {"provider": doc.connector_type, "external_id": doc.external_id}
                    for doc in page_documents
                    if doc.external_id
                ],
                "imported_count": len(page_documents),
                "imported_source_ids": [],
            }
        )
        self.documents.extend(page_documents)
        self.pending_request = None
        await self._publish_page(page)
        self.pending_page_index = None

    def as_receipt(
        self, *, returned_count: int, status: Optional[str] = None
    ) -> Dict[str, Any]:
        if status is None:
            status = (
                "partial"
                if self.error_type and self.documents
                else "failed" if self.error_type else "ok"
            )
        last_response = self.pages[-1]["response"] if self.pages else {}
        if self.error_type:
            completion = "partial_failure" if self.documents else "failed"
        elif last_response.get("has_more") is True:
            completion = (
                "cap_reached"
                if returned_count >= self.requested_limit
                else "more_available"
            )
        elif last_response.get("has_more") is False:
            completion = "exhausted"
        elif returned_count >= self.requested_limit:
            completion = "cap_reached"
        else:
            completion = "unknown"
        return {
            "execution_id": self.execution_id,
            "attempt_id": self.attempt_id,
            "provider": self.provider,
            "status": status,
            "completion": completion,
            "requested_limit": self.requested_limit,
            "returned_count": returned_count,
            "imported_count": returned_count,
            "error_type": self.error_type,
            "started_at": self.started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "pages": self.pages,
        }


class SourceConnector(ABC):
    """Abstract base class for source connectors."""

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 50,
        *,
        search_trace: Optional[SearchTrace] = None,
        **kwargs: Any,
    ) -> List[SourceDocument]:
        """Search the source for documents matching the query."""
        ...

    async def search_with_receipts(
        self,
        query: str,
        max_results: int,
        *,
        execution_id: Optional[str] = None,
        provider: Optional[str] = None,
        trace: Optional[SearchTrace] = None,
    ) -> tuple[List[SourceDocument], Dict[str, Any]]:
        """Run a search while retaining safe request and page evidence."""
        trace = trace or SearchTrace(
            execution_id=execution_id or str(uuid4()),
            provider=provider or type(self).__name__,
            requested_limit=max_results,
        )
        try:
            search_method = self.search
            try:
                supports_trace = (
                    "search_trace" in inspect.signature(search_method).parameters
                )
            except (TypeError, ValueError):
                # If a callable cannot be inspected, preserve the long-standing
                # connector contract and record a generic one-shot receipt.
                supports_trace = False
            if supports_trace:
                documents = await search_method(
                    query, max_results=max_results, search_trace=trace
                )
            else:
                endpoint = getattr(self, "endpoint", None)
                endpoint = (
                    endpoint if isinstance(endpoint, str) else type(self).__name__
                )
                params = {"query": query, "max_results": max_results}
                await trace.begin_request(endpoint, params)
                documents = await search_method(query, max_results=max_results)
                await trace.record_page(
                    endpoint=endpoint,
                    params=params,
                    documents=documents,
                )
        except asyncio.CancelledError:
            await trace.mark_interrupted()
            raise
        except Exception as exc:
            # Provider exception strings may contain signed URLs or credentials.
            trace.error_type = type(exc).__name__
            await trace.record_failed_request(trace.error_type)
            documents = trace.documents
        if not trace.pages:
            endpoint = getattr(self, "endpoint", None)
            await trace.record_page(
                endpoint=endpoint if isinstance(endpoint, str) else None,
                params={"query": query, "max_results": max_results},
                documents=documents,
            )
        return documents, trace.as_receipt(returned_count=len(documents))
