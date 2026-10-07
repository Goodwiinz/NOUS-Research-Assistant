"""Shared fail-closed action guard for mutations requested through a harness.

Lifecycle: ``request_action`` records the intent (idempotent by invocation id
and canonical payload). ``decide_action`` binds one interactive decision to
the stored target. ``execute_action`` claims an approved row with a
conditional UPDATE, commits the claim, revalidates authority, then runs the
effect and the receipt in one transaction. A commit that fails ambiguously
leaves the row ``executing``; ``sweep_stale_actions`` moves it to
``outcome_unknown`` and nothing ever re-runs it.

Authority is bound to the consent chain (the consumed grant request), not to
one grant token: grants expire in minutes and renewal revokes the old token,
while an approval is human-paced. A grant issued without a consent request
(trusted internal issuance) is checked as itself.

The catalogue: every action in ``ALLOWED_ACTIONS`` has its own argument
validator (``validate_arguments``) and names the narrowest scope that may
request it (``REQUIRED_SCOPE_FOR``, checked per action by ``scope_allows``).
``AUTO_RUN_ACTIONS`` are the reversible library changes a ``library:write``
grant may run without a per-action decision (``runs_without_approval``).
"""

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.collection import Collection
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.tool_action import IntegrationToolAction
from src.models.user import User
from src.models.workspace import Workspace
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_tools import ToolInvocation, ToolResult
from src.schemas.tool_actions import ActionActor, ActionReview, ActionStatus
from src.services.agent.tool_helpers import _reject_invalid_arxiv_ids
from src.services.integrations.context import (
    IntegrationAccessDenied,
    authorized_scope_filter,
    live,
)

logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = frozenset(
    {
        "create_project_note",
        "save_papers_to_folder",
        "remove_papers_from_folder",
        "move_papers_between_folders",
        "create_folder",
        "rename_folder",
        "delete_folder",
        "update_document_metadata",
        "ingest_arxiv_papers",
    }
)
# Reversible library changes: a library:write grant may run them without a
# per-action decision. A note, a deletion and an ingest always wait for one.
AUTO_RUN_ACTIONS = frozenset(
    {
        "save_papers_to_folder",
        "remove_papers_from_folder",
        "move_papers_between_folders",
        "create_folder",
        "rename_folder",
        "update_document_metadata",
    }
)
REQUIRED_SCOPE = "tools:write"
LIBRARY_SCOPE = "library:write"
# The narrowest scope that may request each action. Checked per action, never
# per route: library:write reaches only the reversible library actions, so a
# note, a deletion and an ingest always need tools:write (scope_allows).
REQUIRED_SCOPE_FOR: Mapping[str, str] = MappingProxyType(
    {
        "create_project_note": REQUIRED_SCOPE,
        "delete_folder": REQUIRED_SCOPE,
        "ingest_arxiv_papers": REQUIRED_SCOPE,
        **{name: LIBRARY_SCOPE for name in AUTO_RUN_ACTIONS},
    }
)
# Who acts, and under which grant, comes from the session binding, never from
# the model's arguments.
IDENTITY_KEYS = frozenset(
    {
        "user_id",
        "organization_id",
        "workspace_id",
        "thread_id",
        "run_id",
        "grant_id",
        "consent_id",
    }
)
# Which Collection an action aims at is the one thing an argument may choose.
# Each validator only parses the selectors its action takes; the request path
# must check them against the grant's authorized scope (a project grant: its
# own project only) before it stores a row.
SELECTOR_KEYS = frozenset({"project_id", "from_project_id", "to_project_id"})
STALE_EXECUTION = timedelta(minutes=5)
MAX_TITLE = 255
MAX_CONTENT = 200_000
MAX_TAG = 64
MAX_TAGS = 20
MAX_DESCRIPTION = 2000
MAX_DOCUMENT_IDS = 20
MAX_PAPER_IDS = 10
# The actions request_action can bind to a target and run. The library actions
# validate, but stay refused there until their target resolution and effects
# exist: a row stored now would wait for an approval that nothing could run.
_REQUESTABLE_ACTIONS = frozenset({"create_project_note"})


class ToolActionError(Exception):
    pass


class ToolActionArgumentError(ToolActionError):
    pass


class ActionConflict(ToolActionError):
    pass


class ActionNotFound(ToolActionError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_hash(tool_name: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        {"tool_name": tool_name, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def scope_allows(tool_name: str, scopes: Iterable[str]) -> bool:
    """Whether a grant holding ``scopes`` may request ``tool_name``.

    ``tools:write`` may request any action, for a per-action decision.
    ``library:write`` may request only the actions whose narrowest scope it is
    (``REQUIRED_SCOPE_FOR``), which it then runs without one. Not an action:
    False, whatever the scopes.
    """
    required = REQUIRED_SCOPE_FOR.get(tool_name)
    if required is None:
        return False
    held = frozenset(scopes)
    return required in held or REQUIRED_SCOPE in held


def runs_without_approval(tool_name: str, scopes: Iterable[str]) -> bool:
    """Whether ``tool_name`` may skip the per-action decision under ``scopes``."""
    return tool_name in AUTO_RUN_ACTIONS and LIBRARY_SCOPE in frozenset(scopes)


def _validate_note_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    # A note always lands in the grant's own project, so a selector is
    # refused like identity.
    if (IDENTITY_KEYS | SELECTOR_KEYS) & arguments.keys():
        raise ToolActionArgumentError("identity arguments are not accepted")
    if arguments.keys() - {"title", "content", "tags"}:
        raise ToolActionArgumentError("only title, content and tags are accepted")
    title, content = arguments.get("title"), arguments.get("content")
    if not isinstance(title, str) or not title.strip():
        raise ToolActionArgumentError("title is required")
    if len(title) > MAX_TITLE:
        raise ToolActionArgumentError("title must be at most 255 characters")
    if not isinstance(content, str) or not content.strip():
        raise ToolActionArgumentError("content is required")
    if len(content) > MAX_CONTENT:
        raise ToolActionArgumentError("content must be at most 200000 characters")
    tags = arguments.get("tags", [])
    if not isinstance(tags, list) or not all(
        isinstance(t, str) and 0 < len(t) <= MAX_TAG for t in tags
    ):
        raise ToolActionArgumentError("tags must be strings of 1-64 characters")
    if len(tags) > MAX_TAGS:
        raise ToolActionArgumentError("at most 20 tags are accepted")
    return {"title": title, "content": content, "tags": tags}


def _accepted(keys: tuple[str, ...]) -> str:
    if len(keys) == 1:
        return f"only {keys[0]} is accepted"
    return f"only {', '.join(keys[:-1])} and {keys[-1]} are accepted"


def _only(arguments: dict[str, Any], *keys: str) -> None:
    """Refuse identity first, then any key (a selector included) not in keys."""
    if IDENTITY_KEYS & arguments.keys():
        raise ToolActionArgumentError("identity arguments are not accepted")
    if arguments.keys() - set(keys):
        raise ToolActionArgumentError(_accepted(keys))


def _uuid(value: Any, field: str) -> str:
    """One UUID string in canonical form.

    Never a name: the agent helpers resolve any other string as a project NAME.
    """
    if value is None:
        raise ToolActionArgumentError(f"{field} is required")
    if not isinstance(value, str):
        raise ToolActionArgumentError(f"{field} must be a UUID")
    try:
        return str(UUID(value))
    except ValueError as error:
        raise ToolActionArgumentError(f"{field} must be a UUID") from error


def _uuid_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_DOCUMENT_IDS:
        raise ToolActionArgumentError(
            f"{field} must contain 1-{MAX_DOCUMENT_IDS} UUIDs"
        )
    if not all(isinstance(item, str) for item in value):
        raise ToolActionArgumentError(f"{field} must be UUIDs")
    try:
        ids = [str(UUID(item)) for item in value]
    except ValueError as error:
        raise ToolActionArgumentError(f"{field} must be UUIDs") from error
    if len(set(ids)) != len(ids):
        raise ToolActionArgumentError(f"{field} must not repeat an id")
    return ids


def _name(value: Any, field: str) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name or len(name) > MAX_TITLE:
        raise ToolActionArgumentError(f"{field} must be 1-{MAX_TITLE} characters")
    return name


def _tags(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > MAX_TAGS
        or not all(isinstance(t, str) and 0 < len(t) <= MAX_TAG for t in value)
    ):
        raise ToolActionArgumentError(
            f"tags must be at most {MAX_TAGS} strings of 1-{MAX_TAG} characters"
        )
    return list(value)


def _validate_folder_documents(arguments: dict[str, Any]) -> dict[str, Any]:
    """save_papers_to_folder and remove_papers_from_folder."""
    _only(arguments, "document_ids", "project_id")
    return {
        "document_ids": _uuid_list(arguments.get("document_ids"), "document_ids"),
        "project_id": _uuid(arguments.get("project_id"), "project_id"),
    }


def _validate_move(arguments: dict[str, Any]) -> dict[str, Any]:
    _only(arguments, "document_ids", "from_project_id", "to_project_id")
    document_ids = _uuid_list(arguments.get("document_ids"), "document_ids")
    source = _uuid(arguments.get("from_project_id"), "from_project_id")
    destination = _uuid(arguments.get("to_project_id"), "to_project_id")
    if source == destination:
        raise ToolActionArgumentError("from_project_id and to_project_id must differ")
    return {
        "document_ids": document_ids,
        "from_project_id": source,
        "to_project_id": destination,
    }


def _validate_create_folder(arguments: dict[str, Any]) -> dict[str, Any]:
    _only(arguments, "name", "description")
    validated: dict[str, Any] = {"name": _name(arguments.get("name"), "name")}
    description = arguments.get("description")
    if description is not None:
        if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
            raise ToolActionArgumentError(
                f"description must be at most {MAX_DESCRIPTION} characters"
            )
        validated["description"] = description
    return validated


def _validate_rename_folder(arguments: dict[str, Any]) -> dict[str, Any]:
    _only(arguments, "project_id", "name")
    return {
        "project_id": _uuid(arguments.get("project_id"), "project_id"),
        "name": _name(arguments.get("name"), "name"),
    }


def _validate_delete_folder(arguments: dict[str, Any]) -> dict[str, Any]:
    _only(arguments, "project_id")
    return {"project_id": _uuid(arguments.get("project_id"), "project_id")}


def _validate_document_metadata(arguments: dict[str, Any]) -> dict[str, Any]:
    """No selector: the target follows from the document's own Collections."""
    _only(arguments, "document_id", "title", "tags")
    validated: dict[str, Any] = {
        "document_id": _uuid(arguments.get("document_id"), "document_id")
    }
    if "title" in arguments:
        validated["title"] = _name(arguments["title"], "title")
    if "tags" in arguments:
        validated["tags"] = _tags(arguments["tags"])  # [] clears them
    if len(validated) == 1:
        raise ToolActionArgumentError("title or tags is required")
    return validated


def _validate_arxiv_ingest(arguments: dict[str, Any]) -> dict[str, Any]:
    """The optional project_id selector is only parsed here; whether a grant
    needs one (workspace) or may not name another (project) is the target
    check's call."""
    _only(arguments, "paper_ids", "project_id")
    raw = arguments.get("paper_ids")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PAPER_IDS:
        raise ToolActionArgumentError(
            f"paper_ids must contain 1-{MAX_PAPER_IDS} arXiv ids"
        )
    # Strings only (str(2401.00001) would pass the grammar), stripped because
    # the stored ids are what the ingest puts into the arXiv URL.
    if not all(isinstance(paper_id, str) for paper_id in raw):
        raise ToolActionArgumentError("paper_ids contains invalid arXiv ids")
    paper_ids = [paper_id.strip() for paper_id in raw]
    if _reject_invalid_arxiv_ids(paper_ids) is not None:
        # Its payload quotes the rejected ids; the public reason never does.
        raise ToolActionArgumentError("paper_ids contains invalid arXiv ids")
    if len(set(paper_ids)) != len(paper_ids):
        raise ToolActionArgumentError("paper_ids must not repeat an id")
    validated: dict[str, Any] = {"paper_ids": paper_ids}
    if arguments.get("project_id") is not None:
        validated["project_id"] = _uuid(arguments["project_id"], "project_id")
    return validated


_VALIDATORS: Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]] = (
    MappingProxyType(
        {
            "create_project_note": _validate_note_arguments,
            "save_papers_to_folder": _validate_folder_documents,
            "remove_papers_from_folder": _validate_folder_documents,
            "move_papers_between_folders": _validate_move,
            "create_folder": _validate_create_folder,
            "rename_folder": _validate_rename_folder,
            "delete_folder": _validate_delete_folder,
            "update_document_metadata": _validate_document_metadata,
            "ingest_arxiv_papers": _validate_arxiv_ingest,
        }
    )
)


def validate_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The canonical arguments of one action: what is stored and hashed.

    Identity is refused on every action. Selectors are parsed into canonical
    UUID strings, never resolved here. Raises ``ToolActionArgumentError`` with
    a fixed reason, a public 422 detail that never echoes the caller's values.
    """
    if tool_name not in ALLOWED_ACTIONS:
        raise ToolActionArgumentError("tool is not available as an action")
    return _VALIDATORS[tool_name](arguments)


def approval_url(invocation_id: UUID) -> str | None:
    # Same origin rule as the CLI login link (api/auth/cli_auth.py), but a
    # missing origin yields no link instead of an error after the commit.
    base = settings.FRONTEND_BASE_URL or next(iter(settings.cors_origins_list), "")
    if not base.strip():
        return None
    return f"{base.rstrip('/')}/integrations/actions/{invocation_id}"


def _status(row: IntegrationToolAction) -> ActionStatus:
    return ActionStatus(
        invocation_id=row.invocation_id,
        state=cast(Any, row.state),
        tool_name=row.tool_name,
        result=ToolResult.model_validate(row.result) if row.result else None,
        approval_url=(
            approval_url(row.invocation_id)
            if row.state == "awaiting_approval"
            else None
        ),
    )


def _scoped(actor: ActionActor, invocation_id: UUID) -> Any:
    query = select(IntegrationToolAction).where(
        IntegrationToolAction.organization_id == actor.organization_id,
        IntegrationToolAction.user_id == actor.user_id,
        IntegrationToolAction.invocation_id == invocation_id,
        IntegrationToolAction.is_deleted.is_(False),
    )
    if actor.consent_id is not None:
        # Any grant renewed under the same consent sees the consent's actions.
        query = query.where(IntegrationToolAction.consent_id == actor.consent_id)
    elif actor.grant_id is not None:
        query = query.where(IntegrationToolAction.grant_id == actor.grant_id)
    return query.execution_options(populate_existing=True)


async def request_action(
    db: AsyncSession, actor: ActionActor, invocation: ToolInvocation
) -> ActionStatus:
    """Record the intent once. Same id + same payload replays; a different payload conflicts."""
    if invocation.tool_name not in ALLOWED_ACTIONS:
        raise ToolActionArgumentError("tool is not available as an action")
    if actor.project_id is None:
        # Every action row names a project, and none of these actions can
        # select one: refuse a workspace connection rather than store a row
        # whose target cannot be resolved.
        raise ToolActionArgumentError(
            "workspace connections cannot request this action"
        )
    arguments = validate_arguments(invocation.tool_name, invocation.arguments)
    if invocation.tool_name not in _REQUESTABLE_ACTIONS:
        raise ToolActionArgumentError("tool is not available as an action")
    digest = canonical_hash(invocation.tool_name, arguments)
    existing = await db.scalar(_scoped(actor, invocation.invocation_id))
    if existing is None:
        row = IntegrationToolAction(
            organization_id=actor.organization_id,
            user_id=actor.user_id,
            project_id=actor.project_id,
            workspace_id=actor.workspace_id,
            thread_id=actor.thread_id,
            run_id=str(actor.run_id) if actor.run_id else None,
            grant_id=actor.grant_id,
            consent_id=actor.consent_id,
            invocation_id=invocation.invocation_id,
            tool_name=invocation.tool_name,
            arguments=arguments,
            argument_hash=digest,
            state="awaiting_approval",
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            # Concurrent identical request, or the same id under another
            # consent of this user: the first writer wins, re-read it. The
            # rollback already discards the pending row.
            await db.rollback()
            logger.info(
                "integration action insert lost a race",
                extra={"invocation_id": str(invocation.invocation_id)},
            )
            existing = await db.scalar(_scoped(actor, invocation.invocation_id))
        else:
            return _status(row)
    if existing is None:
        # Same invocation id under a different consent: never disclose it.
        raise ActionConflict()
    if existing.argument_hash != digest or not _same_target(existing, actor):
        raise ActionConflict()
    return _status(existing)


def _same_target(row: IntegrationToolAction, actor: ActionActor) -> bool:
    """A replay must name the stored target; the hash excludes server bindings."""
    if row.workspace_id is not None:
        # The row names its grant's workspace, and its project_id, when set, is
        # the Collection the action aims at. A workspace grant's actor carries
        # no project, so only the workspace is compared (the hash covers the
        # arguments that choose the target); an actor that does name a project
        # must name that one.
        same_binding = row.workspace_id == actor.workspace_id and (
            actor.project_id is None or actor.project_id == row.project_id
        )
    else:
        same_binding = row.project_id == actor.project_id and actor.workspace_id is None
    return bool(
        same_binding
        and row.thread_id == actor.thread_id
        and row.run_id == (str(actor.run_id) if actor.run_id else None)
    )


async def decide_action(
    db: AsyncSession, user: User, invocation_id: UUID, *, approved: bool
) -> ActionStatus:
    """Bind one decision from the interactive requester to the stored target."""
    if user.organization_id is None:
        raise ActionNotFound()
    row = await db.scalar(
        select(IntegrationToolAction)
        .where(
            IntegrationToolAction.organization_id == user.organization_id,
            IntegrationToolAction.user_id == user.id,
            IntegrationToolAction.invocation_id == invocation_id,
            IntegrationToolAction.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ActionNotFound()
    decided = await db.execute(
        update(IntegrationToolAction)
        .where(
            IntegrationToolAction.id == row.id,
            IntegrationToolAction.state == "awaiting_approval",
        )
        .values(
            state="approved" if approved else "failed",
            approved=approved,
            decided_by=user.id,
            decided_at=_now(),
            last_error=None if approved else "denied by user",
            updated_at=_now(),
        )
    )
    await db.commit()
    if cast(CursorResult[Any], decided).rowcount != 1:
        raise ActionConflict()  # already decided or already running
    await db.refresh(row)
    return _status(row)


async def get_action_status(
    db: AsyncSession, actor: ActionActor, invocation_id: UUID
) -> ActionStatus:
    row = await db.scalar(_scoped(actor, invocation_id))
    if row is None:
        raise ActionNotFound()
    return _status(row)


async def get_action_for_review(
    db: AsyncSession, user: User, invocation_id: UUID
) -> ActionReview:
    """The requester's own action with its stored target, for the decision page."""
    if user.organization_id is None:
        raise ActionNotFound()
    found = (
        await db.execute(
            select(
                IntegrationToolAction,
                Collection.name,
                Collection.is_deleted,
                Workspace.name,
                Workspace.is_deleted,
            )
            # A workspace-level action has no project; a project action
            # reaches its workspace through the project.
            .outerjoin(Collection, Collection.id == IntegrationToolAction.project_id)
            .join(
                Workspace,
                Workspace.id
                == func.coalesce(
                    IntegrationToolAction.workspace_id, Collection.workspace_id
                ),
            )
            .where(
                IntegrationToolAction.organization_id == user.organization_id,
                IntegrationToolAction.user_id == user.id,
                IntegrationToolAction.invocation_id == invocation_id,
                IntegrationToolAction.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
    ).first()
    if found is None:
        raise ActionNotFound()
    row, project_label, project_deleted, workspace_label, workspace_deleted = found
    arguments = row.arguments or {}
    return ActionReview(
        invocation_id=row.invocation_id,
        state=cast(Any, row.state),
        tool_name=row.tool_name,
        project_id=row.project_id,
        project_label=None if row.project_id is None else str(project_label),
        workspace_id=row.workspace_id,
        workspace_label=None if row.workspace_id is None else str(workspace_label),
        # Kept visible so it can still be denied; approval would fail closed.
        project_available=not (project_deleted or workspace_deleted),
        title=str(arguments.get("title", "")),
        content=str(arguments.get("content", "")),
        tags=[str(t) for t in arguments.get("tags", [])],
        requested_at=row.created_at,
        decided_at=row.decided_at,
        result=ToolResult.model_validate(row.result) if row.result else None,
        last_error=row.last_error,
    )


def _binding_matches(
    project_id: UUID | None, workspace_id: UUID | None, row: IntegrationToolAction
) -> bool:
    """Whether a grant's (or its consent's) binding is the one the row needs.

    A row with a workspace_id was authorised by a workspace grant of that
    workspace, which carries no project: the row's own project_id, if any, is
    the Collection it aims at, and ``_target_reason`` checks that against the
    workspace. A row without one needs a grant on exactly its project and on no
    workspace. Comparing the row's project_id to the grant's would refuse every
    action a workspace grant aims at a Collection (grant NULL, row set).
    """
    if row.workspace_id is not None:
        return project_id is None and workspace_id == row.workspace_id
    return project_id == row.project_id and workspace_id is None


def _consent_binding(row: IntegrationToolAction) -> tuple[Any, Any]:
    """``_binding_matches`` as the conditions on a consent request's columns."""
    if row.workspace_id is not None:
        return (
            IntegrationGrantRequest.project_id.is_(None),
            IntegrationGrantRequest.workspace_id == row.workspace_id,
        )
    return (
        IntegrationGrantRequest.project_id == row.project_id,
        IntegrationGrantRequest.workspace_id.is_(None),
    )


async def _target_reason(
    db: AsyncSession, row: IntegrationToolAction, grant: IntegrationGrant
) -> str | None:
    """A stable reason when a workspace grant no longer reaches the Collection
    its action aims at, else None. A row that names no workspace, or no
    Collection, has nothing more to check here."""
    if row.workspace_id is None or row.project_id is None:
        return None
    # Comparing only the binding lets the grant authorise whatever project_id
    # the row holds, so the target must still be one of the workspace's live
    # Collections that the requesting user can reach: another workspace of
    # theirs, or a deleted Collection, is not.
    context = IntegrationContext(
        user_id=row.user_id,
        organization_id=row.organization_id,
        workspace_id=row.workspace_id,
        grant_id=grant.id,
    )
    try:
        scope = await authorized_scope_filter(db, context)
    except IntegrationAccessDenied:
        return "grant no longer authorizes this action"
    reachable = await db.scalar(
        select(Collection.id).where(
            Collection.id == row.project_id,
            scope,
            Collection.is_deleted.is_(False),
        )
    )
    return None if reachable is not None else "grant no longer authorizes this action"


async def _authority_intact(db: AsyncSession, row: IntegrationToolAction) -> str | None:
    """Return a stable reason when the request may no longer run."""
    if row.grant_id is None:
        return None  # trusted native request; the adapter still checks the project
    grant = await db.scalar(
        select(IntegrationGrant)
        .where(IntegrationGrant.id == row.grant_id)
        .execution_options(populate_existing=True)
    )
    if (
        grant is None
        or grant.is_deleted
        or grant.user_id != row.user_id
        or grant.organization_id != row.organization_id
        or not _binding_matches(grant.project_id, grant.workspace_id, row)
        or REQUIRED_SCOPE not in grant.scopes
    ):
        return "grant no longer authorizes this action"
    if grant.request_id is None:
        # No consent chain: the token itself must still be live.
        if grant.revoked_at is not None or not live(grant.expires_at):
            return "grant revoked or expired"
        return await _target_reason(db, row, grant)
    if row.consent_id is not None and row.consent_id != grant.request_id:
        return "grant no longer authorizes this action"
    consent = await db.scalar(
        select(IntegrationGrantRequest)
        .where(
            IntegrationGrantRequest.id == grant.request_id,
            IntegrationGrantRequest.status == "consumed",
            IntegrationGrantRequest.user_id == row.user_id,
            IntegrationGrantRequest.organization_id == row.organization_id,
            *_consent_binding(row),
            IntegrationGrantRequest.consent_revoked_at.is_(None),
            IntegrationGrantRequest.is_deleted.is_(False),
        )
        .execution_options(populate_existing=True)
    )
    if consent is None or REQUIRED_SCOPE not in consent.scopes:
        return "consent revoked"
    if grant.device_id is not None and consent.device_id != grant.device_id:
        return "consent revoked"
    return await _target_reason(db, row, grant)


async def _run_effect(
    db: AsyncSession, row: IntegrationToolAction, user: User
) -> dict[str, Any]:
    from src.services.agent.tools_impl import _tool_create_project_note

    if row.project_id is None:
        # Never str() a missing project: the adapter resolves any non-UUID,
        # "None" included, as a project NAME.
        raise ToolActionError("action has no target project")
    # Project comes from the stored binding; the adapter re-checks edit authority.
    return await _tool_create_project_note(
        {**row.arguments, "project_id": str(row.project_id)},
        db,
        user,
        commit=False,
    )


async def _finish(
    db: AsyncSession,
    row_id: UUID,
    *,
    state: str,
    last_error: str | None,
    result: dict[str, Any] | None = None,
) -> bool:
    """Write the receipt only while the row is still ours; commit with the effect."""
    written = await db.execute(
        update(IntegrationToolAction)
        .where(
            IntegrationToolAction.id == row_id,
            IntegrationToolAction.state == "executing",
        )
        .values(
            state=state,
            last_error=last_error,
            result=result,
            executed_at=_now(),
            updated_at=_now(),
        )
    )
    if cast(CursorResult[Any], written).rowcount != 1:
        # The sweeper already called this outcome unknown; discard the effect.
        await db.rollback()
        return False
    # Effect and receipt commit together: a rollback here leaves no note, and
    # an ambiguous commit leaves the row `executing` for the sweeper.
    await db.commit()
    return True


async def _fail_after_rollback(
    db: AsyncSession, row_id: UUID, reason: str, result: dict[str, Any] | None = None
) -> ActionStatus | None:
    await db.rollback()
    await _finish(db, row_id, state="failed", last_error=reason, result=result)
    row = await db.get(IntegrationToolAction, row_id, populate_existing=True)
    return _status(row) if row is not None else None


async def _execute_row(db: AsyncSession, row_id: UUID) -> ActionStatus | None:
    now = _now()
    claimed = await db.execute(
        update(IntegrationToolAction)
        .where(
            IntegrationToolAction.id == row_id,
            IntegrationToolAction.state == "approved",
        )
        .values(state="executing", claimed_at=now, updated_at=now)
    )
    # Commit the claim before any effect so a second worker can never run it.
    await db.commit()
    if cast(CursorResult[Any], claimed).rowcount != 1:
        return None
    row = await db.get(IntegrationToolAction, row_id, populate_existing=True)
    if row is None:
        return None
    invocation = {"invocation_id": str(row.invocation_id)}

    try:
        reason = await _authority_intact(db, row)
        user = None if reason else await db.get(User, row.user_id)
        if user is None or not user.is_active or user.is_deleted:
            reason = reason or "requesting user is not active"
        elif user.organization_id != row.organization_id:
            reason = "requesting user left the organization"
    except Exception as error:  # noqa: BLE001 - the outcome is known: nothing ran
        logger.error(
            "integration action authority check failed",
            extra=invocation,
            exc_info=error,
        )
        return await _fail_after_rollback(db, row_id, "authority check failed")
    if reason or user is None:
        logger.warning("integration action denied: %s", reason, extra=invocation)
        return await _fail_after_rollback(db, row_id, reason or "denied")

    try:
        payload = await _run_effect(db, row, user)
    except Exception as error:  # noqa: BLE001 - nothing was committed
        logger.error(
            "integration action effect failed", extra=invocation, exc_info=error
        )
        return await _fail_after_rollback(db, row_id, "effect failed before commit")

    if "error" in payload:
        # The adapter reports failures as a payload and may leave the session
        # poisoned; discard any partial write with the failed receipt.
        logger.warning("integration action effect rejected", extra=invocation)
        result = ToolResult(content=[payload], is_error=True, source_refs=[])
        return await _fail_after_rollback(
            db, row_id, str(payload.get("error")), result.model_dump(mode="json")
        )

    result = ToolResult(
        content=[payload],
        is_error=False,
        source_refs=[{"note_id": payload.get("note_id")}],
    )
    if not await _finish(
        db,
        row_id,
        state="succeeded",
        last_error=None,
        result=result.model_dump(mode="json"),
    ):
        logger.error(
            "integration action finished after being marked unknown", extra=invocation
        )
    row = await db.get(IntegrationToolAction, row_id, populate_existing=True)
    return _status(row) if row is not None else None


async def execute_action(db: AsyncSession, invocation_id: UUID) -> ActionStatus | None:
    """Run one approved action by invocation id. Returns None when nothing was claimed."""
    row_id = await db.scalar(
        select(IntegrationToolAction.id).where(
            IntegrationToolAction.invocation_id == invocation_id,
            IntegrationToolAction.state == "approved",
            IntegrationToolAction.is_deleted.is_(False),
        )
    )
    if row_id is None:
        return None
    return await _execute_row(db, row_id)


async def drain_integration_actions(db: AsyncSession, *, limit: int = 50) -> int:
    """Execute approved rows oldest first. Returns how many finished (any outcome).

    Gated by the same kill switch as new requests: with the flag off, approved
    rows wait instead of running.
    """
    if not settings.NOUS_MCP_ENABLED:
        return 0
    row_ids = (
        await db.scalars(
            select(IntegrationToolAction.id)
            .where(
                IntegrationToolAction.state == "approved",
                IntegrationToolAction.is_deleted.is_(False),
            )
            .order_by(IntegrationToolAction.decided_at)
            .limit(limit)
        )
    ).all()
    finished = 0
    for row_id in row_ids:
        if await _execute_row(db, row_id) is not None:
            finished += 1
    return finished


async def sweep_stale_actions(db: AsyncSession) -> int:
    """An `executing` row older than STALE_EXECUTION is uncertain, never retried."""
    cutoff = _now() - STALE_EXECUTION
    swept = await db.execute(
        update(IntegrationToolAction)
        .where(
            IntegrationToolAction.state == "executing",
            or_(
                IntegrationToolAction.claimed_at.is_(None),
                IntegrationToolAction.claimed_at < cutoff,
            ),
        )
        .values(
            state="outcome_unknown",
            last_error="worker did not confirm the outcome",
            updated_at=_now(),
        )
    )
    await db.commit()
    count = int(cast(CursorResult[Any], swept).rowcount)
    if count:
        logger.error("integration actions with unknown outcome", extra={"count": count})
    return count
