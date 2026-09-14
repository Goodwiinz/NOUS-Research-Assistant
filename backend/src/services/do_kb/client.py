"""Async HTTP client for DigitalOcean Knowledge Base / GradientAI Platform.

Endpoints:
- Control plane: api.digitalocean.com/v2/gen-ai/knowledge_bases/...
- Indexing jobs: api.digitalocean.com/v2/gen-ai/indexing_jobs/...
- Retrieve plane: kbaas.do-ai.run/v1/{kb_uuid}/retrieve

Bearer auth, exponential backoff on 429/5xx (3 attempts), 30s default timeout.
Public Preview API — wrap parsing in permissive Pydantic models so field
additions don't break us.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from src.core.config import Settings
from src.core.config import settings as global_settings

from .models import Chunk, DataSource, IndexingJob, KnowledgeBase, RetrieveResult

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 60  # cap a server-supplied wait so we don't hang


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header (delta-seconds form) into a bounded float.

    Returns None when absent or not a plain integer (HTTP-date form is ignored —
    we fall back to exponential backoff). Capped at _MAX_RETRY_AFTER_SECONDS.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (ValueError, TypeError, AttributeError):
        return None
    if seconds < 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


class DOKnowledgeBaseError(RuntimeError):
    """Raised on non-retryable DO KB API failures."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DOKnowledgeBaseClient:
    """Async client for DO KB control + retrieve planes.

    Settings are injected so tests can swap them without touching env.
    """

    def __init__(self, cfg: Optional[Settings] = None) -> None:
        self._settings = cfg or global_settings
        self._http: Optional[httpx.AsyncClient] = None

    def _client(self) -> httpx.AsyncClient:
        """Lazily create one pooled AsyncClient and reuse it across requests.

        A fresh client per request (the old `async with httpx.AsyncClient()`) paid
        full TLS setup every call — magnified per-document + per-retry during bulk
        ingest. Timeout is passed per-request so a single pooled client still
        honors the longer indexing timeout.
        """
        if self._http is None:
            self._http = httpx.AsyncClient()
        return self._http

    async def aclose(self) -> None:
        """Close the pooled client (call on app shutdown)."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def _api_base(self) -> str:
        return self._settings.DO_KB_API_HOST.rstrip("/")

    @property
    def _retrieve_base(self) -> str:
        return self._settings.DO_KB_RETRIEVE_HOST.rstrip("/")

    @property
    def _auth_headers(self) -> dict[str, str]:
        token = self._settings.DO_API_TOKEN
        if not token:
            raise DOKnowledgeBaseError("DO_API_TOKEN not configured")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        timeout_s = timeout or self._settings.DO_KB_REQUEST_TIMEOUT_SECONDS
        last_exc: Optional[Exception] = None
        last_status: Optional[int] = None

        client = self._client()
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await client.request(
                    method,
                    url,
                    headers=self._auth_headers,
                    json=json_body,
                    timeout=timeout_s,
                )
                if response.status_code in _RETRYABLE_STATUS:
                    # Remember the status so an all-retryable run reports the real
                    # code instead of "exhausted retries: None".
                    last_status = response.status_code
                    retry_after = _parse_retry_after(
                        response.headers.get("Retry-After")
                    )
                    wait = retry_after if retry_after is not None else 2**attempt
                    logger.warning(
                        "do_kb retryable status",
                        extra={
                            "status": response.status_code,
                            "url": url,
                            "attempt": attempt + 1,
                            "retry_after": retry_after,
                        },
                    )
                    # Do not delay the caller after the final attempt. The
                    # previous loop paid one extra backoff even though no
                    # request remained to retry.
                    if attempt < _MAX_ATTEMPTS - 1:
                        await asyncio.sleep(wait)
                    continue
                if response.status_code >= 400:
                    raise DOKnowledgeBaseError(
                        f"DO KB request failed: {response.status_code} {response.text[:300]}",
                        status_code=response.status_code,
                    )
                if not response.content:
                    return {}
                try:
                    return response.json()
                except (TypeError, ValueError) as exc:
                    raise DOKnowledgeBaseError(
                        "DO KB response was not valid JSON",
                        status_code=response.status_code,
                    ) from exc
            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning(
                    "do_kb timeout",
                    extra={"url": url, "attempt": attempt + 1},
                )
                await asyncio.sleep(2**attempt)
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "do_kb http error",
                    extra={"url": url, "attempt": attempt + 1, "error": str(exc)},
                )
                await asyncio.sleep(2**attempt)

        if last_status is not None:
            raise DOKnowledgeBaseError(
                f"DO KB request exhausted retries: HTTP {last_status}",
                status_code=last_status,
            )
        raise DOKnowledgeBaseError(f"DO KB request exhausted retries: {last_exc}")

    # ── Control plane ────────────────────────────────────────────────────────

    async def create_kb(
        self,
        *,
        name: str,
        region: str,
        project_id: str,
        embedding_model_uuid: str,
        tags: Optional[list[str]] = None,
    ) -> KnowledgeBase:
        body: dict[str, Any] = {
            "name": name,
            "region": region,
            "project_id": project_id,
            "embedding_model_uuid": embedding_model_uuid,
        }
        if tags:
            body["tags"] = tags
        payload = await self._request(
            "POST",
            f"{self._api_base}/v2/gen-ai/knowledge_bases",
            json_body=body,
        )
        kb_data = payload.get("knowledge_base", payload)
        return KnowledgeBase.model_validate(kb_data)

    async def add_spaces_data_source(
        self,
        *,
        kb_uuid: str,
        bucket: str,
        key: str,
        region: Optional[str] = None,
    ) -> DataSource:
        body: dict[str, Any] = {
            "knowledge_base_uuid": kb_uuid,
            "spaces_data_source": {
                "bucket_name": bucket,
                "item_path": key,
                "region": region or self._settings.DO_KB_REGION,
            },
        }
        payload = await self._request(
            "POST",
            f"{self._api_base}/v2/gen-ai/knowledge_bases/{kb_uuid}/data_sources",
            json_body=body,
        )
        ds_data = payload.get("knowledge_base_data_source", payload)
        return DataSource.model_validate(ds_data)

    async def list_data_sources(self, *, kb_uuid: str) -> list[dict[str, Any]]:
        """Return the KB's existing data sources as raw dicts.

        Best-effort + defensive: the exact list-response shape is treated as
        unverified, so callers should match fields with .get() and fall back to
        adding when nothing matches. Returns [] on an unexpected shape.
        """
        payload = await self._request(
            "GET",
            f"{self._api_base}/v2/gen-ai/knowledge_bases/{kb_uuid}/data_sources",
        )
        raw = (
            payload.get("knowledge_base_data_sources")
            or payload.get("data_sources")
            or []
        )
        return [s for s in raw if isinstance(s, dict)]

    async def delete_data_source(self, *, kb_uuid: str, ds_uuid: str) -> None:
        """Delete a data source from the KB.

        Used to remove a file that consistently fails to index (e.g. a PDF
        that exceeds DO's processing time limit). The DO API returns 204 on
        success; the ``_request`` wrapper handles retries on 429/5xx and
        returns ``{}`` on an empty body.
        """
        await self._request(
            "DELETE",
            f"{self._api_base}/v2/gen-ai/knowledge_bases/{kb_uuid}/data_sources/{ds_uuid}",
        )

    async def start_indexing(self, *, kb_uuid: str) -> IndexingJob:
        payload = await self._request(
            "POST",
            f"{self._api_base}/v2/gen-ai/indexing_jobs",
            json_body={"knowledge_base_uuid": kb_uuid},
            timeout=self._settings.DO_KB_INDEXING_TIMEOUT_SECONDS,
        )
        job_data = payload.get("job", payload)
        return IndexingJob.model_validate(job_data)

    async def get_indexing_job(self, *, kb_uuid: str, job_uuid: str) -> IndexingJob:
        payload = await self._request(
            "GET",
            f"{self._api_base}/v2/gen-ai/indexing_jobs/{job_uuid}",
        )
        job_data = payload.get("job", payload)
        return IndexingJob.model_validate(job_data)

    # ── Retrieve plane ───────────────────────────────────────────────────────

    async def retrieve(
        self,
        *,
        kb_uuid: str,
        query: str,
        top_k: Optional[int] = None,
        alpha: Optional[float] = None,
        filters: Optional[dict[str, Any]] = None,
    ) -> RetrieveResult:
        # DO KBaaS ``/v1/{kb}/retrieve`` now validates the body against a strict
        # schema — any unrecognized field yields ``400 {"message":"invalid
        # request body"}``. The live-proven generic fields are exactly:
        #   query: str (required)
        #   num_results: int 1-100 (required; NOT top_k)
        #   alpha: float 0-1 (optional; lexical vs semantic balance)
        # ``reranking`` and ``search_type`` were accepted during Public Preview
        # but are now rejected — sending either 400s the call, which the rag
        # node silently swallows into a keyword-only fallback. DO reranks
        # server-side by default; client-side re-scoring lives behind
        # ``AGENT_DOKB_COHERE_RERANK`` (``cohere_rescore_chunks``), independent
        # of this request body.
        k = top_k if top_k is not None else self._settings.DO_KB_DEFAULT_TOP_K
        body: dict[str, Any] = {"query": query, "num_results": max(1, min(k, 100))}
        resolved_alpha = (
            alpha if alpha is not None else self._settings.DO_KB_RETRIEVE_ALPHA
        )
        if resolved_alpha is not None:
            body["alpha"] = resolved_alpha
        # Current DO documentation advertises optional scoped filters. Generic
        # callers omit this field; named-document retrieval sends it once and
        # fails closed if the deployed endpoint rejects it.
        if filters is not None:
            body["filters"] = filters

        payload = await self._request(
            "POST",
            f"{self._retrieve_base}/v1/{kb_uuid}/retrieve",
            json_body=body,
        )
        raw_chunks = payload.get("results") or payload.get("chunks") or []
        chunks = [
            Chunk.from_do_payload(raw, rank=i) for i, raw in enumerate(raw_chunks)
        ]
        total = payload.get("total_results") or payload.get("total") or len(chunks)
        return RetrieveResult(chunks=chunks, total=total)


_default_client: Optional[DOKnowledgeBaseClient] = None


def get_do_kb_client() -> DOKnowledgeBaseClient:
    """Process-wide singleton. Tests should construct DOKnowledgeBaseClient
    directly rather than using this getter."""
    global _default_client
    if _default_client is None:
        _default_client = DOKnowledgeBaseClient()
    return _default_client
