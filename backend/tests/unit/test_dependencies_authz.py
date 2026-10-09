"""R4-L12 / GOO-410: access helpers in core/dependencies.py.

GOO-410: ``can_access_document`` applies the organization-wide document read
boundary (the caller's organization, not soft-deleted), the rule every
document, file and citation read route applies. R4-L18 had narrowed it to
uploader-or-admin for private documents, which contradicted the live read
routes; ``is_public`` is now a label only. A denial is the same 404 as a
missing document, and an org-less caller is denied without a query.

R4-L12: ``get_current_user_optional`` had an unreachable ``if not token_data:
return None`` branch, since its dependency chain always either returns a
``TokenData`` or raises (see ``core/security.py``'s module-level
``HTTPBearer()``, ``auto_error=True``). Confirmed zero callers repo-wide, so
the branch was deleted rather than reworked into genuine optional auth.
"""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from src.core.dependencies import can_access_document, get_current_user_optional

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _db_with_document(document: Optional[Mock]) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = document
    db.execute = AsyncMock(return_value=result)
    return db


def _user(org_id: Optional[UUID]) -> Mock:
    u = Mock()
    u.id = uuid4()
    u.organization_id = org_id
    u.has_permission = Mock(return_value=False)
    return u


def _document(org_id: Optional[UUID], uploader_id: object, is_public: bool) -> Mock:
    doc = Mock()
    doc.organization_id = org_id
    doc.uploaded_by_user_id = uploader_id
    doc.is_public = is_public
    return doc


def _where_sql(db: AsyncMock) -> str:
    stmt = db.execute.await_args.args[0]
    sql = str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    return sql.split("WHERE", 1)[1].replace("-", "")


# --- GOO-410: can_access_document is the org-wide read boundary -------------


@pytest.mark.parametrize("is_public", [False, True])
async def test_same_org_colleague_reads_document_whatever_is_public(
    is_public: bool,
) -> None:
    org = uuid4()
    user = _user(org)
    doc = _document(org_id=org, uploader_id=uuid4(), is_public=is_public)
    db = _db_with_document(doc)

    result_user, result_doc = await can_access_document(str(uuid4()), user, db)

    assert result_user is user
    assert result_doc is doc
    user.has_permission.assert_not_called()  # no uploader/role narrowing


async def test_lookup_is_scoped_to_caller_org_and_not_deleted() -> None:
    org = uuid4()
    user = _user(org)
    db = _db_with_document(_document(org_id=org, uploader_id=None, is_public=False))

    await can_access_document(str(uuid4()), user, db)

    where = _where_sql(db)
    assert f"documents.organization_id = '{org.hex}'" in where
    assert "documents.is_deleted" in where
    assert "is_public" not in where
    assert "uploaded_by_user_id" not in where


async def test_foreign_org_or_missing_document_is_404() -> None:
    user = _user(uuid4())
    db = _db_with_document(None)  # the org-scoped query matched nothing

    with pytest.raises(HTTPException) as exc:
        await can_access_document(str(uuid4()), user, db)

    assert exc.value.status_code == 404


async def test_orgless_caller_is_404_without_a_query() -> None:
    user = _user(None)
    db = _db_with_document(_document(org_id=None, uploader_id=user.id, is_public=True))

    with pytest.raises(HTTPException) as exc:
        await can_access_document(str(uuid4()), user, db)

    assert exc.value.status_code == 404
    db.execute.assert_not_awaited()


# --- R4-L12: get_current_user_optional dead-branch removal ------------------


async def test_get_current_user_optional_returns_user_for_valid_token() -> None:
    from types import SimpleNamespace

    token_data: Any = SimpleNamespace(user_id=str(uuid4()))
    fake_user = Mock()
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = fake_user
    db.execute = AsyncMock(return_value=result)

    out = await get_current_user_optional(token_data=token_data, db=db)

    assert out is fake_user


async def test_get_current_user_optional_returns_none_on_db_error() -> None:
    from types import SimpleNamespace

    token_data: Any = SimpleNamespace(user_id=str(uuid4()))
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))

    out = await get_current_user_optional(token_data=token_data, db=db)

    assert out is None
