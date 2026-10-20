"""GOO-423: an organization move starts a private, independently keyed chain."""

from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.integration_handoff import IntegrationHandoff
from src.models.user import User
from src.models.workspace import Workspace
from src.services.integrations.handoffs import HandoffConflict, read_latest, save
from tests.unit.services.integrations.test_handoffs import (
    ORG,
    OTHER_ORG,
    USER,
    WORKSPACE,
    _ctx,
    _payload,
)
from tests.unit.services.integrations.test_handoffs import db as db

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("reuse_handoff_id", [False, True])
async def test_owner_org_change_starts_a_new_chain_without_exposing_old_rows(
    db: AsyncSession, reuse_handoff_id: bool
) -> None:
    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(organization_id=None)
    )
    await db.commit()
    retry_id = uuid4()
    previous = await save(
        db, _ctx(), _payload(handoff_id=retry_id, results=[], goal="Old organization")
    )
    await db.execute(
        update(User).where(User.id == USER).values(organization_id=OTHER_ORG)
    )
    await db.commit()
    context = _ctx(org=OTHER_ORG)
    assert await read_latest(db, context) is None
    payload = _payload(
        handoff_id=retry_id if reuse_handoff_id else uuid4(),
        results=[],
        goal="New organization",
    )
    current = await save(db, context, payload)
    assert current.version == 1 and current.id != previous.id
    assert await read_latest(db, context) == current
    assert await read_latest(db, _ctx()) is None
    assert await save(db, context, payload) == current

    # The new tenant keeps normal optimistic-concurrency and replay behavior.
    with pytest.raises(HandoffConflict) as conflict:
        await save(db, context, _payload(results=[], goal="Stale first save"))
    assert conflict.value.latest == current
    rows = (await db.scalars(select(IntegrationHandoff))).all()
    assert {(row.organization_id, row.goal) for row in rows} == {
        (ORG, "Old organization"),
        (OTHER_ORG, "New organization"),
    }
