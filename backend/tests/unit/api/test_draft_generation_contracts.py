"""Public draft-generation validation and conflict transport contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.api.research import drafts as api
from src.models.user import User

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_selection_validation_is_a_generic_400_without_source_details() -> None:
    service = MagicMock()
    service.generate_draft = AsyncMock(
        side_effect=ValueError("Selected document 83b4 is inactive in project 91af")
    )

    with (
        patch.object(api, "_validate_project_ownership", new=AsyncMock()),
        patch.object(api, "DraftGenerationService", return_value=service),
        pytest.raises(HTTPException) as raised,
    ):
        await api.generate_draft(
            project_id=uuid4(),
            themes=["topic"],
            document_ids=[],
            style="academic",
            max_sections=5,
            include_abstract=True,
            current_user=cast(User, SimpleNamespace(id=uuid4())),
            db=MagicMock(),
        )

    assert raised.value.status_code == 400
    assert raised.value.detail == "Invalid draft generation request."
    assert "83b4" not in str(raised.value.detail)
    assert "91af" not in str(raised.value.detail)


async def test_active_generation_conflict_is_stable_409() -> None:
    service = MagicMock()
    service.generate_draft = AsyncMock(
        return_value={"error_category": "draft_generation_conflict"}
    )

    with (
        patch.object(api, "_validate_project_ownership", new=AsyncMock()),
        patch.object(api, "DraftGenerationService", return_value=service),
        pytest.raises(HTTPException) as raised,
    ):
        await api.generate_draft(
            project_id=uuid4(),
            themes=["topic"],
            document_ids=None,
            style="academic",
            max_sections=5,
            include_abstract=True,
            current_user=cast(User, SimpleNamespace(id=uuid4())),
            db=MagicMock(),
        )

    assert raised.value.status_code == 409
    assert (
        raised.value.detail == "A draft generation is already active for this project."
    )
