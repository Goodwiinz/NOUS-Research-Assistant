"""
Celery task for executing research workflows.
"""

import asyncio
import functools
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from celery import current_app

from src.core.config import settings
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_run import ResearchRun, RunStatus
from src.models.research_step import ResearchStep
from src.services.research_engine.connectors import (
    ArxivConnector,
    CrossrefConnector,
    PubMedConnector,
    RagStoreConnector,
    SemanticScholarConnector,
)
from src.services.research_engine.connectors.registry import build_connectors
from src.services.research_engine.engine import WorkflowEngine
from src.services.research_engine.providers import (
    ClaudeProvider,
    OllamaProvider,
    OpenAIProvider,
    ProviderConfig,
)
from src.services.research_engine.step_executor import StepExecutor
from src.tasks.processing_tasks import SessionLocal

logger = logging.getLogger(__name__)


def _create_provider(model_id: str):
    """Factory that returns an LLM provider for a model_id, if configured."""
    normalized = (model_id or "").strip()
    if not normalized:
        return None

    lower = normalized.lower()
    if lower.startswith("claude"):
        if not settings.ANTHROPIC_API_KEY:
            return None
        return ClaudeProvider(
            ProviderConfig(
                provider_type="claude",
                model_id=normalized,
                api_key=settings.ANTHROPIC_API_KEY,
            )
        )

    if lower.startswith("gpt") or lower.startswith("o1") or lower.startswith("o3"):
        if not settings.OPENAI_API_KEY:
            return None
        return OpenAIProvider(
            ProviderConfig(
                provider_type="openai",
                model_id=normalized,
                api_key=settings.OPENAI_API_KEY,
            )
        )

    if (
        lower.startswith("ollama/")
        or lower.startswith("llama")
        or lower.startswith("mistral")
    ):
        ollama_model = (
            normalized.split("/", 1)[1] if lower.startswith("ollama/") else normalized
        )
        return OllamaProvider(
            ProviderConfig(
                provider_type="ollama",
                model_id=ollama_model,
                base_url=os.getenv("OLLAMA_BASE_URL"),
            )
        )

    logger.info(f"No provider mapping available for model_id={model_id}")
    return None


def _get_step_params(step_def: Dict[str, Any]) -> Dict[str, Any]:
    return step_def.get("params") or step_def.get("parameters") or {}


def _build_providers(steps: list) -> dict:
    """Scan blueprint steps and build a provider dict keyed by model_id."""
    providers: dict = {}
    for step_def in steps:
        params = _get_step_params(step_def)
        model_id = step_def.get("model_id") or params.get("model_id")
        if model_id and model_id not in providers:
            provider = _create_provider(model_id)
            if provider is not None:
                providers[model_id] = provider
    return providers


async def _search_rag_store(
    query: str, max_results: int = 50, *, organization_id: Optional[str] = None
) -> Dict[str, Any]:
    """Search the existing hybrid RAG index for local-store style connector output.

    MUST be scoped to the run owner's ``organization_id``. ``hybrid_search_service``
    forwards it to ``fulltext_search_service``, which applies the tenant filter
    only ``if organization_id:`` — so passing nothing means *no filter*, and a
    research run would surface (and copy ``full_text`` from) every tenant's
    documents. The HTTP path already scopes this (``api/research_engine/runs.py``);
    the Celery path did not.

    Fails closed: an unresolved organization returns no local results rather
    than silently searching every tenant.
    """
    from src.models.search_schemas import SearchQuery
    from src.services.search.hybrid_search_service import hybrid_search_service

    if not organization_id:
        logger.error(
            "rag_store search skipped: no organization_id resolved for this run — "
            "refusing to run an org-unfiltered search"
        )
        return {"results": []}

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        None,
        lambda: hybrid_search_service.search(
            SearchQuery(query=query, limit=max_results, search_type="hybrid"),
            organization_id=organization_id,
        ),
    )

    results = []
    for item in response.results:
        metadata = getattr(item, "metadata", {}) or {}
        content = (
            metadata.get("full_text")
            or metadata.get("text")
            or getattr(item, "content_preview", None)
            or getattr(item, "content_snippet", None)
            or ""
        )
        results.append(
            {
                "id": str(getattr(item, "document_id", "")),
                "title": getattr(item, "title", ""),
                "content": content,
                "metadata": metadata,
            }
        )
    return {"results": results}


def _build_connectors(organization_id: Optional[str] = None) -> dict:
    """Create connector instances for the workflow.

    ``organization_id`` (the run owner's) is bound into the rag_store search so
    the local-index connector only ever returns this tenant's documents —
    mirrors ``api/research_engine/runs.py::_build_connectors``.
    """
    rag_search = functools.partial(_search_rag_store, organization_id=organization_id)
    return build_connectors(rag_search)


def _resolve_run_organization_id(db: Any, blueprint: Any) -> Optional[str]:
    """Owning organization for a run: blueprint → project → owner → org.

    ``ResearchRun`` carries no tenant column of its own, so the owner is
    reached through the blueprint's project. Returns None when it cannot be
    resolved; callers must treat that as "do not search", never as "search
    everything".
    """
    from src.models.research_project import ResearchProject
    from src.models.user import User

    org_id = (
        db.query(User.organization_id)
        .join(ResearchProject, ResearchProject.owner_id == User.id)
        .filter(ResearchProject.id == blueprint.project_id)
        .scalar()
    )
    return str(org_id) if org_id else None


async def _run_engine(engine: WorkflowEngine, blueprint_dict: dict, run_id: UUID):
    """Consume the async generator from WorkflowEngine.run() and collect events."""
    events = []
    async for event in engine.run(blueprint_dict, run_id):
        events.append(event)
    return events


@current_app.task(
    name="src.tasks.research_tasks.run_extraction_matrix",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def run_extraction_matrix(
    matrix_id: str,
    document_ids: List[str],
    columns: List[Dict[str, Any]],
    task_id: str,
) -> None:
    """Run matrix extraction in a durable Celery worker."""
    from src.services.research.extraction_matrix_service import ExtractionMatrixService

    asyncio.run(
        ExtractionMatrixService().run_background_extraction(
            matrix_id=UUID(matrix_id),
            document_ids=[UUID(document_id) for document_id in document_ids],
            columns=columns,
            task_id=task_id,
        )
    )
