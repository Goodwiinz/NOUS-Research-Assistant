"""Authenticated transport for the observed PDF chat history contract."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.agent import execute
from src.core.database import AsyncSessionLocal
from src.core.dependencies import get_current_user
from src.models.user import User
from src.schemas.alphaxiv_history import (
    AlphaXivChatSummary,
    AlphaXivContextWindowUsage,
    AlphaXivHistoryMessage,
    AlphaXivSelection,
)
from src.services.compat import alphaxiv_history as history
from src.services.compat.alphaxiv import CompatibilityError

router = APIRouter(prefix="/assistant/v2", tags=["alphaXiv compatibility"])


async def _history_actor(current_user: User = Depends(get_current_user)) -> User:
    await execute._enforce_rate_limit(
        execute._agent_read_rate_limiter,
        str(current_user.id),
        execute._AGENT_READ_PREFIX,
    )
    return current_user


@router.get("", response_model=list[AlphaXivChatSummary])
async def list_chats(
    variant: Literal["paper"] = Query(...),
    paper_version: UUID = Query(..., alias="paperVersion"),
    current_user: User = Depends(_history_actor),
) -> list[AlphaXivChatSummary]:
    try:
        async with AsyncSessionLocal() as db:
            return await history.list_paper_chats(
                db, actor_id=current_user.id, paper_id=paper_version
            )
    except CompatibilityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("/{chat_id}/messages", response_model=list[AlphaXivHistoryMessage])
async def get_messages(
    chat_id: UUID,
    current_user: User = Depends(_history_actor),
) -> list[AlphaXivHistoryMessage]:
    try:
        async with AsyncSessionLocal() as db:
            return await history.get_chat_messages(
                db, actor_id=current_user.id, chat_id=chat_id
            )
    except CompatibilityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post(
    "/{chat_id}/messages/{message_id}/select", response_model=AlphaXivSelection
)
async def select_message(
    chat_id: UUID,
    message_id: UUID,
    current_user: User = Depends(_history_actor),
) -> AlphaXivSelection:
    try:
        async with AsyncSessionLocal() as db:
            return await history.select_chat_message(
                db, actor_id=current_user.id, chat_id=chat_id, message_id=message_id
            )
    except CompatibilityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.get(
    "/{chat_id}/context-window-usage",
    response_model=AlphaXivContextWindowUsage | None,
)
async def context_window_usage(
    chat_id: UUID,
    current_user: User = Depends(_history_actor),
) -> AlphaXivContextWindowUsage | None:
    try:
        async with AsyncSessionLocal() as db:
            return await history.get_context_window_usage(
                db, actor_id=current_user.id, chat_id=chat_id
            )
    except CompatibilityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
