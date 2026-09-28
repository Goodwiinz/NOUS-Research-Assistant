"""Versioned research question, protocol, approval, and conformance APIs."""

from typing import List, cast
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.research_project_role import ResearchProjectRole
from src.models.research_protocol import (
    ProtocolDeviation,
    ProtocolRegistrationOperation,
    ResearchProtocol,
    ResearchProtocolVersion,
    ResearchQuestion,
    ResearchQuestionVersion,
)
from src.models.user import User
from src.models.workspace import WorkspaceRole
from src.schemas.research_engine import (
    ProtocolApprovalRequest,
    ProtocolApprovalResponse,
    ProtocolDeviationCreate,
    ProtocolDeviationResponse,
    ProtocolRegistrationCreate,
    ProtocolRegistrationResponse,
    ResearchProtocolCreate,
    ResearchProtocolResponse,
    ResearchProtocolVersionCreate,
    ResearchProtocolVersionResponse,
    ResearchQuestionCreate,
    ResearchQuestionResponse,
    ResearchQuestionVersionCreate,
    ResearchQuestionVersionResponse,
)
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from src.services.research_engine.protocol_service import (
    add_protocol_version,
    add_question_version,
    approve_protocol_version,
    create_protocol,
    create_question,
    record_deviation,
    record_registration,
    require_protocol,
    require_protocol_version,
)

router = APIRouter(prefix="/research-engine", tags=["research-engine-protocols"])


def _question_version_response(
    version: ResearchQuestionVersion,
) -> ResearchQuestionVersionResponse:
    return cast(
        ResearchQuestionVersionResponse,
        ResearchQuestionVersionResponse.model_validate(version, from_attributes=True),
    )


async def _question_response(
    db: AsyncSession, question: ResearchQuestion
) -> ResearchQuestionResponse:
    versions = list(
        (
            await db.execute(
                select(ResearchQuestionVersion)
                .where(ResearchQuestionVersion.question_id == question.id)
                .order_by(ResearchQuestionVersion.version.asc())
            )
        )
        .scalars()
        .all()
    )
    current = next(v for v in versions if v.id == question.current_version_id)
    return ResearchQuestionResponse(
        id=question.id,
        project_id=question.collection_id,
        current_version_id=current.id,
        current_version=_question_version_response(current),
        versions=[_question_version_response(version) for version in versions],
        created_at=question.created_at,
    )


def _protocol_version_response(
    version: ResearchProtocolVersion,
) -> ResearchProtocolVersionResponse:
    return cast(
        ResearchProtocolVersionResponse,
        ResearchProtocolVersionResponse.model_validate(version, from_attributes=True),
    )


async def _protocol_response(
    db: AsyncSession,
    protocol: ResearchProtocol,
    context: ProjectContext,
    actor_user_id: UUID,
) -> ResearchProtocolResponse:
    versions = list(
        (
            await db.execute(
                select(ResearchProtocolVersion)
                .where(ResearchProtocolVersion.protocol_id == protocol.id)
                .order_by(ResearchProtocolVersion.version.asc())
            )
        )
        .scalars()
        .all()
    )
    active = (
        not context.workspace.is_archived
        and context.collection.research_status != "archived"
    )
    can_edit = active and context.workspace_role in {
        WorkspaceRole.OWNER,
        WorkspaceRole.ADMIN,
        WorkspaceRole.EDITOR,
    }
    can_manage = active and context.workspace_role in {
        WorkspaceRole.OWNER,
        WorkspaceRole.ADMIN,
    }
    return ResearchProtocolResponse(
        id=protocol.id,
        project_id=protocol.collection_id,
        name=protocol.name,
        current_draft_version_id=protocol.current_draft_version_id,
        current_approved_version_id=protocol.current_approved_version_id,
        versions=[
            _protocol_version_response(version).model_copy(
                update={
                    "can_approve": (
                        active
                        and version.id == protocol.current_draft_version_id
                        and version.status == "draft"
                        and version.author_user_id != actor_user_id
                        and ResearchProjectRole.SUPERVISOR in context.effective_roles
                    )
                }
            )
            for version in versions
        ],
        can_edit=can_edit,
        can_manage=can_manage,
        can_approve=(
            active
            and ResearchProjectRole.SUPERVISOR in context.effective_roles
            and any(
                version.id == protocol.current_draft_version_id
                and version.status == "draft"
                and version.author_user_id != actor_user_id
                for version in versions
            )
        ),
        created_at=protocol.created_at,
        updated_at=protocol.updated_at,
    )


@router.get(
    "/projects/{project_id}/questions", response_model=List[ResearchQuestionResponse]
)
async def list_questions(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[ResearchQuestionResponse]:
    await resolve_project(db, project_id, current_user.id, ResearchAction.VIEW)
    questions = list(
        (
            await db.execute(
                select(ResearchQuestion).where(
                    ResearchQuestion.collection_id == project_id,
                    ResearchQuestion.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    return [await _question_response(db, question) for question in questions]


@router.post(
    "/projects/{project_id}/questions",
    response_model=ResearchQuestionResponse,
    status_code=201,
)
async def create_question_route(
    project_id: UUID,
    body: ResearchQuestionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchQuestionResponse:
    context = await resolve_project(
        db, project_id, current_user.id, ResearchAction.EDIT
    )
    question = await create_question(db, context, current_user.id, body)
    return await _question_response(db, question)


@router.post(
    "/questions/{question_id}/versions",
    response_model=ResearchQuestionResponse,
    status_code=201,
)
async def create_question_version_route(
    question_id: UUID,
    body: ResearchQuestionVersionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchQuestionResponse:
    question = (
        await db.execute(
            select(ResearchQuestion).where(
                ResearchQuestion.id == question_id,
                ResearchQuestion.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if question is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Question not found")
    await resolve_project(
        db, question.collection_id, current_user.id, ResearchAction.EDIT
    )
    question = await add_question_version(
        db, question_id, question.collection_id, current_user.id, body
    )
    return await _question_response(db, question)


@router.get(
    "/projects/{project_id}/protocols", response_model=List[ResearchProtocolResponse]
)
async def list_protocols(
    project_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[ResearchProtocolResponse]:
    context = await resolve_project(
        db, project_id, current_user.id, ResearchAction.VIEW
    )
    protocols = list(
        (
            await db.execute(
                select(ResearchProtocol)
                .where(
                    ResearchProtocol.collection_id == project_id,
                    ResearchProtocol.is_deleted.is_(False),
                )
                .order_by(ResearchProtocol.created_at.asc(), ResearchProtocol.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        await _protocol_response(db, protocol, context, current_user.id)
        for protocol in protocols
    ]


@router.post(
    "/projects/{project_id}/protocols",
    response_model=ResearchProtocolResponse,
    status_code=201,
)
async def create_protocol_route(
    project_id: UUID,
    body: ResearchProtocolCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchProtocolResponse:
    context = await resolve_project(
        db, project_id, current_user.id, ResearchAction.EDIT
    )
    protocol = await create_protocol(db, context, current_user.id, body)
    return await _protocol_response(db, protocol, context, current_user.id)


async def _authorized_protocol(
    db: AsyncSession,
    protocol_id: UUID,
    user_id: UUID,
    action: ResearchAction,
) -> tuple[ResearchProtocol, ProjectContext]:
    protocol = (
        await db.execute(
            select(ResearchProtocol).where(
                ResearchProtocol.id == protocol_id,
                ResearchProtocol.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if protocol is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Protocol not found")
    context = await resolve_project(db, protocol.collection_id, user_id, action)
    return protocol, context


@router.get("/protocols/{protocol_id}", response_model=ResearchProtocolResponse)
async def get_protocol(
    protocol_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchProtocolResponse:
    protocol, context = await _authorized_protocol(
        db, protocol_id, current_user.id, ResearchAction.VIEW
    )
    return await _protocol_response(db, protocol, context, current_user.id)


@router.post(
    "/protocols/{protocol_id}/versions",
    response_model=ResearchProtocolResponse,
    status_code=201,
)
async def create_protocol_version_route(
    protocol_id: UUID,
    body: ResearchProtocolVersionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchProtocolResponse:
    protocol, context = await _authorized_protocol(
        db, protocol_id, current_user.id, ResearchAction.EDIT
    )
    protocol = await add_protocol_version(
        db, protocol.id, protocol.collection_id, current_user.id, body
    )
    return await _protocol_response(db, protocol, context, current_user.id)


@router.post(
    "/protocols/{protocol_id}/versions/{version_id}/approve",
    response_model=ProtocolApprovalResponse,
)
async def approve_protocol_version_route(
    protocol_id: UUID,
    version_id: UUID,
    body: ProtocolApprovalRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProtocolApprovalResponse:
    protocol, context = await _authorized_protocol(
        db, protocol_id, current_user.id, ResearchAction.SUPERVISE
    )
    event = await approve_protocol_version(
        db, context, protocol.id, version_id, current_user.id, body
    )
    return ProtocolApprovalResponse(
        decision_id=event.id,
        protocol_id=protocol.id,
        protocol_version_id=cast(UUID, event.subject_version_id),
        content_hash=cast(str, event.subject_hash),
        actor_user_id=cast(UUID, event.actor_user_id),
        actor_role=cast(str, event.actor_role),
        reason=event.reason,
        approved_at=event.occurred_at,
    )


@router.get(
    "/protocols/{protocol_id}/registrations",
    response_model=List[ProtocolRegistrationResponse],
)
async def list_registrations(
    protocol_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[ProtocolRegistrationResponse]:
    protocol, _ = await _authorized_protocol(
        db, protocol_id, current_user.id, ResearchAction.VIEW
    )
    rows = list(
        (
            await db.execute(
                select(ProtocolRegistrationOperation)
                .join(
                    ResearchProtocolVersion,
                    ResearchProtocolVersion.id
                    == ProtocolRegistrationOperation.protocol_version_id,
                )
                .where(ResearchProtocolVersion.protocol_id == protocol.id)
                .order_by(ProtocolRegistrationOperation.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        ProtocolRegistrationResponse.model_validate(row, from_attributes=True)
        for row in rows
    ]


@router.post(
    "/protocols/{protocol_id}/registrations",
    response_model=ProtocolRegistrationResponse,
    status_code=201,
)
async def create_registration(
    protocol_id: UUID,
    body: ProtocolRegistrationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProtocolRegistrationResponse:
    protocol, _ = await _authorized_protocol(
        db, protocol_id, current_user.id, ResearchAction.MANAGE
    )
    row = await record_registration(
        db, protocol.id, protocol.collection_id, current_user.id, body
    )
    return cast(
        ProtocolRegistrationResponse,
        ProtocolRegistrationResponse.model_validate(row, from_attributes=True),
    )


@router.get(
    "/protocols/{protocol_id}/deviations",
    response_model=List[ProtocolDeviationResponse],
)
async def list_deviations(
    protocol_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[ProtocolDeviationResponse]:
    protocol, _ = await _authorized_protocol(
        db, protocol_id, current_user.id, ResearchAction.VIEW
    )
    rows = list(
        (
            await db.execute(
                select(ProtocolDeviation)
                .join(
                    ResearchProtocolVersion,
                    ResearchProtocolVersion.id == ProtocolDeviation.protocol_version_id,
                )
                .where(ResearchProtocolVersion.protocol_id == protocol.id)
                .order_by(ProtocolDeviation.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        ProtocolDeviationResponse.model_validate(row, from_attributes=True)
        for row in rows
    ]


@router.post(
    "/protocols/{protocol_id}/deviations",
    response_model=ProtocolDeviationResponse,
    status_code=201,
)
async def create_deviation(
    protocol_id: UUID,
    body: ProtocolDeviationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProtocolDeviationResponse:
    protocol, _ = await _authorized_protocol(
        db, protocol_id, current_user.id, ResearchAction.EDIT
    )
    row = await record_deviation(
        db, protocol.id, protocol.collection_id, current_user.id, body
    )
    return cast(
        ProtocolDeviationResponse,
        ProtocolDeviationResponse.model_validate(row, from_attributes=True),
    )
