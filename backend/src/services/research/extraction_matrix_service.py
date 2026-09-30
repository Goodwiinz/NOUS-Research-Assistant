from datetime import datetime

"""Extraction Matrix service for structured data extraction from documents."""

import json
from typing import Any, Dict, List, Optional
from uuid import UUID

import openai
import structlog
from sqlalchemy import Select, select

from src.core.database import AsyncSessionLocal
from src.models.document import Document
from src.models.extraction_matrix import ExtractionFormVersion, ExtractionMatrix
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.services.research.extraction_forms_service import (
    append_machine_observations,
    document_source_hash,
)
from src.services.research_engine.project_access import lock_active_project

logger = structlog.get_logger()


def _scoped_document_query(doc_id: UUID, project_id: Optional[UUID]) -> Select:
    """Document fetch constrained to a project's collection.

    Returns the document only when it is a member of ``project_id``'s
    collection_documents, so a document id outside the matrix's project is
    never read (tenant/project isolation)."""
    from src.services.research_engine.project_access import project_documents_query

    return project_documents_query(project_id).where(Document.id == doc_id)


PREDATES_FORMS = "Extraction task predates form versions; re-run"
MACHINE_MISSINGNESS = ("not_reported", "not_applicable")


def _already_observed_query(matrix_id: UUID, idempotency_key: str) -> Select:
    """A (task, document) run already recorded in the matrix stream."""
    return (
        select(ResearchDecisionEvent.id)
        .join(
            ResearchDecisionStream,
            ResearchDecisionStream.id == ResearchDecisionEvent.stream_id,
        )
        .where(
            ResearchDecisionStream.aggregate_type == "research_extraction",
            ResearchDecisionStream.aggregate_id == matrix_id,
            ResearchDecisionEvent.idempotency_key == idempotency_key,
        )
    )


# In-memory store is an L1 fallback; Redis is the shared status authority when
# configured so a status request can land on any API pod (R5-L16).
_EXTRACTION_STATUS_MAX = 500
_EXTRACTION_STATUS_TTL_SECONDS = 24 * 60 * 60
_EXTRACTION_STATUS_KEY_PREFIX = "research:extraction:"
_extraction_status: Dict[str, Dict[str, Any]] = {}


class ExtractionMatrixService:
    """Service for building extraction prompts and parsing LLM extraction results."""

    @staticmethod
    async def set_extraction_status(
        task_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge and persist status in the bounded L1 cache and Redis L2."""
        from src.services.agent.job_store import get_redis

        _extraction_status[task_id] = {
            **_extraction_status.get(task_id, {}),
            "updated_at": datetime.utcnow().isoformat(),
            **payload,
        }
        while len(_extraction_status) > _EXTRACTION_STATUS_MAX:
            # M11-review: evict oldest TERMINAL entry first so a live
            # running extraction can never be dropped under churn.
            terminal = [
                k
                for k, v in _extraction_status.items()
                if v.get("status") in ("completed", "failed")
            ]
            pool = terminal or list(_extraction_status)
            oldest = min(
                pool, key=lambda k: _extraction_status[k].get("updated_at", "")
            )
            _extraction_status.pop(oldest)

        status = dict(_extraction_status[task_id])
        try:
            redis_client = await get_redis()
            if redis_client is not None:
                key = f"{_EXTRACTION_STATUS_KEY_PREFIX}{task_id}"
                raw = await redis_client.get(key)
                shared = json.loads(raw) if raw else {}
                # Carry forward fields from a status update written by a
                # different pod, then apply this update as the newest write.
                status = {
                    **(shared if isinstance(shared, dict) else {}),
                    **status,
                }
                _extraction_status[task_id] = status
                await redis_client.setex(
                    key,
                    _EXTRACTION_STATUS_TTL_SECONDS,
                    json.dumps(status, default=str),
                )
        except Exception as exc:  # pragma: no cover - Redis is optional
            logger.debug("extraction status Redis mirror unavailable: %s", exc)
        return status

    @staticmethod
    async def get_extraction_status(task_id: str) -> Optional[Dict[str, Any]]:
        """Get status from Redis (shared) with the L1 fallback."""
        try:
            from src.services.agent.job_store import get_redis

            redis_client = await get_redis()
            if redis_client is not None:
                raw = await redis_client.get(
                    f"{_EXTRACTION_STATUS_KEY_PREFIX}{task_id}"
                )
                if raw:
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        _extraction_status[task_id] = value
                        return value
        except Exception as exc:  # pragma: no cover - Redis is optional
            logger.debug("extraction status Redis read unavailable: %s", exc)
        return _extraction_status.get(task_id)

    @staticmethod
    def _get_openai_client() -> tuple:
        """Create an OpenAI client from settings. Returns (client, model)."""
        from src.core.config import settings

        azure_key = settings.AZURE_OPENAI_CHAT_API_KEY or settings.AZURE_OPENAI_API_KEY
        azure_endpoint = (
            settings.AZURE_OPENAI_CHAT_ENDPOINT or settings.AZURE_OPENAI_ENDPOINT
        )
        azure_deployment = getattr(
            settings, "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME", "gpt-4o"
        )
        azure_api_version = getattr(
            settings, "AZURE_OPENAI_CHAT_API_VERSION", "2024-05-01-preview"
        )
        openai_key = settings.OPENAI_API_KEY

        if azure_key and azure_endpoint:
            client = openai.AsyncAzureOpenAI(
                api_key=azure_key,
                azure_endpoint=azure_endpoint,
                api_version=azure_api_version,
            )
            return client, azure_deployment
        elif openai_key:
            client = openai.AsyncOpenAI(api_key=openai_key)
            return client, "gpt-4o-mini"
        else:
            raise RuntimeError("No OpenAI or Azure OpenAI API key configured.")

    async def run_background_extraction(
        self,
        matrix_id: UUID,
        document_ids: List[UUID],
        columns: List[Dict[str, Any]],
        task_id: str,
        *,
        form_version_id: Optional[UUID] = None,
        initiated_by_user_id: Optional[UUID] = None,
        source_hashes: Optional[Dict[str, str]] = None,
    ) -> None:
        """Append machine observations per document, in its own DB session.

        ``columns`` is ignored: the prompt comes from the pinned form version.
        A document is recorded once per task id (a Celery retry skips it),
        only while its source still matches the hash pinned at enqueue, and
        only under ``lock_active_project``. Nothing here can accept a value.
        """
        await self.set_extraction_status(
            task_id,
            {
                "status": "running",
                "matrix_id": str(matrix_id),
                "total": len(document_ids),
                "completed": 0,
                "failed": 0,
                "skipped": 0,
                "error": None,
            },
        )

        async def bump(counter: str) -> None:
            count = _extraction_status[task_id].get(counter, 0) + 1
            await self.set_extraction_status(task_id, {counter: count})

        if form_version_id is None or initiated_by_user_id is None:
            # Filling in an actor or a form would invent provenance.
            await self.set_extraction_status(
                task_id, {"status": "failed", "error": PREDATES_FORMS}
            )
            return
        pinned = source_hashes or {}

        try:
            client, model = self._get_openai_client()
        except RuntimeError as e:
            await self.set_extraction_status(task_id, {"status": "failed"})
            await self.set_extraction_status(task_id, {"error": str(e)})
            return

        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(ExtractionMatrix, ExtractionFormVersion)
                    .join(
                        ExtractionFormVersion,
                        ExtractionFormVersion.matrix_id == ExtractionMatrix.id,
                    )
                    .where(
                        ExtractionMatrix.id == matrix_id,
                        ExtractionMatrix.is_deleted.is_(False),
                        ExtractionFormVersion.id == form_version_id,
                    )
                )
            ).first()

            for doc_id in document_ids:
                try:
                    if row is None:
                        await bump("skipped")  # matrix deleted since enqueue
                        continue
                    matrix, version = row
                    key = f"{task_id}:{doc_id}"
                    already = await db.execute(_already_observed_query(matrix_id, key))
                    if already.first() is not None:
                        await bump("completed")  # Celery retry: already recorded
                        continue
                    # Scoped to the matrix's project: a foreign id is never read.
                    document = (
                        await db.execute(
                            _scoped_document_query(doc_id, matrix.project_id)
                        )
                    ).scalar_one_or_none()
                    expected = pinned.get(str(doc_id))
                    if (
                        document is None
                        or expected is None
                        or document_source_hash(document) != expected
                    ):
                        await bump("skipped")
                        logger.info(
                            "bg_extraction_skip",
                            task_id=task_id,
                            document_id=str(doc_id),
                        )
                        continue

                    fields = list(version.fields)
                    if not document.content_text:
                        parsed: Dict[str, Dict[str, Any]] = {
                            f["name"]: {"missing": "unavailable_text"} for f in fields
                        }
                    else:
                        prompt = self._build_extraction_prompt(
                            fields, document.content_text[:12000]
                        )
                        response = await client.chat.completions.create(
                            model=model,
                            messages=[{"role": "user", "content": prompt}],
                            temperature=0.1,
                            max_tokens=2000,
                        )
                        raw_json = response.choices[0].message.content or ""
                        parsed = self._parse_extraction_result(raw_json, fields)

                    await lock_active_project(db, matrix.project_id)
                    await append_machine_observations(
                        db,
                        matrix=matrix,
                        version=version,
                        document=document,
                        parsed=parsed,
                        actor_id=initiated_by_user_id,
                        run_id=task_id,
                        model=model,
                    )
                    await db.commit()
                    await bump("completed")

                except Exception as e:
                    await db.rollback()
                    logger.error(
                        "bg_extraction_doc_failed",
                        task_id=task_id,
                        document_id=str(doc_id),
                        error=str(e),
                    )
                    await bump("failed")

        await self.set_extraction_status(task_id, {"status": "completed"})
        logger.info(
            "bg_extraction_complete",
            task_id=task_id,
            matrix_id=str(matrix_id),
            total=_extraction_status[task_id]["total"],
            completed=_extraction_status[task_id]["completed"],
            failed=_extraction_status[task_id]["failed"],
            skipped=_extraction_status[task_id]["skipped"],
        )

    def _build_extraction_prompt(
        self, columns: List[Dict[str, Any]], document_text: str
    ) -> str:
        """Build a structured extraction prompt for the LLM.

        Args:
            columns: List of column definitions, each with 'name' and optional 'description'.
            document_text: The full text of the document to extract from.

        Returns:
            A prompt string instructing the LLM to return JSON with keys matching column names.
        """
        column_descriptions = []
        for col in columns:
            name = col["name"]
            desc = col.get("description") or "Extract relevant information"
            hints = [col.get("type") or "text"]
            if col.get("unit"):
                hints.append(f"unit: {col['unit']}")
            if col.get("timepoint"):
                hints.append(f"timepoint: {col['timepoint']}")
            if col.get("categories"):
                hints.append("one of: " + ", ".join(col["categories"]))
            column_descriptions.append(f'- "{name}" ({"; ".join(hints)}): {desc}')

        columns_block = "\n".join(column_descriptions)

        prompt = (
            "You are a research data extraction assistant. "
            "Extract the following information from the document text below.\n\n"
            "For each column, provide a JSON object with keys matching the column names. "
            "Each value should be an object with three fields:\n"
            '  - "value": the extracted information in the column\'s type, or null\n'
            '  - "missing": null when a value is given; otherwise "not_reported" '
            '(the document does not report it) or "not_applicable" (it does not apply)\n'
            '  - "citation": a brief quote or page reference from the source text (string or null)\n\n'
            "Columns to extract:\n"
            f"{columns_block}\n\n"
            "Document text:\n"
            f"{document_text}\n\n"
            "Respond ONLY with valid JSON. No markdown, no explanation."
        )

        return prompt

    def _parse_extraction_result(
        self, raw_json: str, columns: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """Parse the LLM's JSON output into a structured result.

        Args:
            raw_json: Raw JSON string from the LLM.
            columns: List of column definitions to validate against.

        Returns:
            Dict mapping column names to {"value", "missing", "citation"}:
            exactly one of value / missing is set. A column that is absent or
            malformed, or a whole unparseable response, is
            ``missing="extraction_error"`` - never a silent blank.
        """
        result: Dict[str, Dict[str, Any]] = {}

        # Strip markdown code fences if present
        cleaned = raw_json.strip() if raw_json else ""
        if cleaned.startswith("```"):
            # Remove opening fence (```json or ```)
            first_newline = cleaned.find("\n")
            if first_newline != -1:
                cleaned = cleaned[first_newline + 1 :]
            # Remove closing fence
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()

        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "extraction_parse_failed",
                raw_preview=raw_json[:200] if raw_json else "",
            )
            parsed = {}

        if not isinstance(parsed, dict):
            parsed = {}
        for col in columns:
            name = col["name"]
            entry = parsed.get(name)
            entry = entry if isinstance(entry, dict) else {}
            value, missing = entry.get("value"), entry.get("missing")
            if value is not None and missing is None:
                result[name] = {"value": value, "missing": None}
            elif value is None and missing in MACHINE_MISSINGNESS:
                result[name] = {"value": None, "missing": missing}
            else:
                result[name] = {"value": None, "missing": "extraction_error"}
            citation = entry.get("citation")
            result[name]["citation"] = citation if isinstance(citation, str) else None

        return result
