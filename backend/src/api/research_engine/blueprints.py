"""Research Engine blueprint endpoints."""

import logging
from copy import deepcopy
from typing import Any, Dict, List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.user import User
from src.schemas.research_engine import (
    BlueprintCreate,
    BlueprintResponse,
    BlueprintStepDefinition,
    BlueprintTemplateDetailResponse,
)
from src.services.research_engine.blueprints.loader import (
    DAILY_RESEARCH_BRIEF_TEMPLATE,
    BlueprintLoader,
)
from src.services.research_engine.connectors.registry import (
    normalize_connector_selection,
)
from src.services.research_engine.project_access import (
    ResearchAction,
    require_blueprint,
    require_research_project,
    resolve_engine_project_context,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/research-engine/blueprints",
    tags=["research-engine"],
)


@router.get(
    "/templates",
    response_model=List[Dict[str, Any]],
    dependencies=[Depends(get_current_user)],
)
async def list_templates() -> List[Dict[str, Any]]:
    """List available YAML blueprint templates."""
    loader = BlueprintLoader()
    return loader.list_templates()


@router.get(
    "/templates/{slug}",
    response_model=BlueprintTemplateDetailResponse,
    dependencies=[Depends(get_current_user)],
)
async def get_template_detail(slug: str) -> BlueprintTemplateDetailResponse:
    """Return the complete, validated server-owned template contract."""
    try:
        template = BlueprintLoader().load_template(slug)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Blueprint template not found",
        ) from exc
    return BlueprintTemplateDetailResponse.model_validate(
        {
            "slug": slug,
            "template_source": template.get("template_source", slug),
            "contract_version": template.get(
                "contract_version",
                (template.get("parameters") or {}).get("contract_version", 1),
            ),
            "constraints": template.get("constraints", {}),
            "coverage": template.get("coverage", {}),
            **template,
        }
    )


def _canonical_template_steps(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        BlueprintStepDefinition.model_validate(step).model_dump(mode="json")
        for step in template["steps"]
    ]


def _merge_template_parameters(
    template: Dict[str, Any], requested: Dict[str, Any]
) -> Dict[str, Any]:
    defaults = deepcopy(template.get("parameters") or {})
    unknown = set(requested) - set(defaults)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Template parameters contain undeclared values",
        )
    if "contract_version" in requested and requested[
        "contract_version"
    ] != defaults.get("contract_version"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Template contract version is server-owned",
        )

    values = deepcopy(requested)
    values.pop("contract_version", None)
    defaults.update(values)
    if template.get("template_source") == "daily_research_brief":
        providers = defaults.get("providers")
        if not isinstance(providers, list) or any(
            not isinstance(provider, str) for provider in providers
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Daily Brief providers are invalid",
            )
        try:
            defaults["providers"] = list(
                normalize_connector_selection(providers, daily_brief_only=True)
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Daily Brief providers are invalid",
            ) from exc
        limit = defaults.get("limit_per_provider")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Daily Brief provider limit is invalid",
            )
    return defaults


@router.post(
    "/projects/{project_id}",
    response_model=BlueprintResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_blueprint(
    project_id: UUID,
    body: BlueprintCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BlueprintResponse:
    """Create a blueprint for a project."""
    project = await require_research_project(
        db, project_id, current_user.id, ResearchAction.EDIT
    )

    template_source: str | None = None
    concrete_steps = [step.model_dump(mode="json") for step in body.steps]
    concrete_parameters = deepcopy(body.parameters)
    if body.template_source:
        try:
            template = BlueprintLoader().load_template(body.template_source)
        except FileNotFoundError as exc:
            if body.template_source == DAILY_RESEARCH_BRIEF_TEMPLATE:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Blueprint template not found",
                ) from exc
            if not concrete_steps:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Custom blueprints require at least one step",
                ) from exc
        except ValueError:
            if not concrete_steps:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Custom blueprints require at least one step",
                )
        else:
            expected_steps = _canonical_template_steps(template)
            if concrete_steps and concrete_steps != expected_steps:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Template topology does not match server contract",
                )
            template_source = body.template_source
            concrete_steps = deepcopy(expected_steps)
            concrete_parameters = _merge_template_parameters(template, body.parameters)

    blueprint = ResearchBlueprint(
        project_id=project.id,
        name=body.name,
        template_source=template_source,
        steps=concrete_steps,
        parameters=concrete_parameters,
    )
    db.add(blueprint)
    await db.commit()
    await db.refresh(blueprint)
    return BlueprintResponse.model_validate(
        {
            **blueprint.__dict__,
            "project_id": project_id,
            "research_engine_project_id": project.id,
        }
    )


@router.get(
    "/{blueprint_id}",
    response_model=BlueprintResponse,
)
async def get_blueprint(
    blueprint_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BlueprintResponse:
    """Get a single blueprint with ownership verification in one query."""
    blueprint = await require_blueprint(
        db, blueprint_id, current_user.id, ResearchAction.VIEW
    )
    context = await resolve_engine_project_context(
        db, blueprint.project_id, current_user.id, ResearchAction.VIEW
    )
    assert context.engine is not None
    return BlueprintResponse.model_validate(
        {
            **blueprint.__dict__,
            "project_id": context.collection.id,
            "research_engine_project_id": context.engine.id,
        }
    )
