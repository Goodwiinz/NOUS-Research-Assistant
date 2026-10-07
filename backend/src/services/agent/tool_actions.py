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
request it (``REQUIRED_SCOPE_FOR``, checked per action by ``scope_allows`` on
request, and again on the live grant and consent before the effect).
``AUTO_RUN_ACTIONS`` are the reversible library changes a ``library:write``
grant runs without a per-action decision (``runs_without_approval``):
``request_action`` stores them approved by the grant's own consent and runs
them at once through the same claim the drain uses. Every target comes from
the grant's binding; a selector argument only chooses among the Collections
its live scope reaches (``_resolve_target``).

``DETACHED_ACTIONS`` (the arXiv ingest) cannot share the receipt's
transaction: the ingest stores papers through sessions of its own and object
storage. It always waits for a decision, runs from the same worker drain once
claimed, and its receipt is written afterwards and says so. It runs for at most
``DETACHED_TIMEOUT``, and a drain starts one only while that whole timeout
still fits in its own ``DRAIN_BUDGET``.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, CursorResult, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.collection import Collection, CollectionDocument
from src.models.document import Document
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.tool_action import IntegrationToolAction
from src.models.user import User
from src.models.workspace import Workspace
from src.schemas.chat import CollectionCreate, CollectionUpdate
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_tools import ToolInvocation, ToolResult
from src.schemas.tool_actions import ActionActor, ActionReview, ActionStatus
from src.services.agent.tool_helpers import _reject_invalid_arxiv_ids
from src.services.documents import file_metadata_service
from src.services.integrations.context import (
    IntegrationAccessDenied,
    authorized_scope_filter,
    live,
)
from src.services.threads import collection_service

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
# Claimed and authorised like any action, but the effect commits through
# sessions of its own (per-paper persistence, object storage), so it runs
# outside the transaction that writes the receipt. Never auto-run.
DETACHED_ACTIONS = frozenset({"ingest_arxiv_papers"})
# The longest a detached effect may run: the agent graph's bound for the same
# tool (_nodes_tools._SLOW_TOOL_TIMEOUT_SECONDS). It stays under DRAIN_BUDGET
# and STALE_EXECUTION, so the worker records how an ingest ended, not Celery's
# time limit or the sweeper.
DETACHED_TIMEOUT = timedelta(seconds=120)
# How long one drain keeps starting rows. The drain task's Celery time limits
# (integration_action_tasks) sit above it, and a drain starts an ingest only
# while the whole DETACHED_TIMEOUT still fits inside it.
DRAIN_BUDGET = timedelta(seconds=240)
GRANT_DENIED = "grant no longer authorizes this action"
# Stable reasons a library effect records instead of a service's own text.
TARGET_NOT_FOUND = "target not found"
INSUFFICIENT_PERMISSIONS = "insufficient permissions"
INGEST_FAILED = "ingest failed"
INGEST_UNKNOWN = "ingest outcome unknown"
INGEST_ATOMICITY = (
    "Papers are stored and saved to the folder in transactions of their own "
    "before this receipt is written: those listed here stay in the library "
    "even when the action failed."
)
# The ingest tool's statuses (tools_impl.INGEST_STATUS_*). A missing or
# unknown status counts as a failure.
_INGEST_COMPLETE = "ingestion_complete"
_INGEST_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "ingestion_partial": "ingest partially failed",
        "ingestion_complete_link_failed": "papers ingested but not saved to the folder",
        "ingestion_failed": INGEST_FAILED,
    }
)


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


def _clock() -> float:
    """Monotonic seconds, for the drain's budget."""
    return time.monotonic()


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
    """Record the intent once. Same id + same payload replays; a different payload conflicts.

    In order: the action's own validator, its own scope (``scope_allows``),
    then, for a new row, its target against the grant's live scope
    (``_resolve_target``). An action the grant may run without a decision
    (``runs_without_approval``) is stored approved by the grant's consent and
    run at once through the claim the drain uses; any other waits for one.
    Raises ``ToolActionArgumentError`` (422), ``IntegrationAccessDenied``
    (403) or ``ActionConflict`` (409).
    """
    arguments = validate_arguments(invocation.tool_name, invocation.arguments)
    if not scope_allows(invocation.tool_name, actor.scopes):
        raise IntegrationAccessDenied()
    digest = canonical_hash(invocation.tool_name, arguments)
    existing = await db.scalar(_scoped(actor, invocation.invocation_id))
    if existing is None:
        target = await _resolve_target(db, actor, invocation.tool_name, arguments)
        auto_run = runs_without_approval(invocation.tool_name, actor.scopes)
        row = IntegrationToolAction(
            organization_id=actor.organization_id,
            user_id=actor.user_id,
            # The Collection the action aims at (None: the workspace itself),
            # and the binding of the grant that asked for it.
            project_id=target,
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
        if auto_run:
            # The user agreed to these when they granted library:write: the
            # grant's own consent stands in for the per-action decision.
            row.state = "approved"
            row.approved = True
            row.decided_by = actor.user_id
            row.decided_at = _now()
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
            if not auto_run:
                return _status(row)
            ran = await _execute_row(db, row.id)
            if ran is not None:
                return ran
            # The drain claimed it first: report whatever the row says now.
            current = await _current_status(db, row.id)
            return current if current is not None else _status(row)
    if existing is None:
        # Same invocation id under a different consent: never disclose it.
        raise ActionConflict()
    if existing.argument_hash != digest or not _same_target(existing, actor):
        raise ActionConflict()
    return _status(existing)


async def _resolve_target(
    db: AsyncSession, actor: ActionActor, tool_name: str, arguments: dict[str, Any]
) -> UUID | None:
    """The Collection a new action aims at, checked against the grant's live scope.

    A project grant reaches its own Collection only: a selector naming any
    other is refused, and it cannot create a folder beside it. A workspace
    grant's selectors must name live Collections of its workspace that the
    user can still reach; ``create_folder`` aims at the workspace itself
    (None), ``update_document_metadata`` at a folder of that scope holding the
    document (the first by name when several do), and ``ingest_arxiv_papers``
    must name its folder. Out of scope raises ``IntegrationAccessDenied``,
    opaque, so a foreign Collection reads like a missing one.
    """
    if (actor.project_id is None) == (actor.workspace_id is None):
        raise IntegrationAccessDenied()  # a grant binds exactly one of them
    selected = {UUID(arguments[key]) for key in SELECTOR_KEYS if key in arguments}
    target: UUID | None = None
    if actor.project_id is not None:
        if tool_name == "create_folder" or selected - {actor.project_id}:
            raise IntegrationAccessDenied()
        target = actor.project_id
    elif tool_name == "create_project_note":
        # A note lands in the grant's own project and takes no selector.
        raise ToolActionArgumentError(
            "workspace connections cannot request this action"
        )
    elif tool_name == "move_papers_between_folders":
        target = UUID(arguments["from_project_id"])
    elif "project_id" in arguments:
        target = UUID(arguments["project_id"])
    elif tool_name == "ingest_arxiv_papers":
        # A workspace holds many folders: an ingest must say which one.
        raise ToolActionArgumentError("project_id is required")
    scope = await _scope_condition(db, actor)
    if tool_name == "update_document_metadata":
        folders = await _folders_holding(
            db, UUID(arguments["document_id"]), actor.organization_id, scope
        )
        if not folders:
            raise IntegrationAccessDenied()
        return folders[0]
    wanted = selected | ({target} if target is not None else set())
    if wanted:
        reachable = set(
            (
                await db.scalars(
                    select(Collection.id).where(
                        Collection.id.in_(wanted),
                        scope,
                        Collection.is_deleted.is_(False),
                    )
                )
            ).all()
        )
        if not wanted <= reachable:
            raise IntegrationAccessDenied()
    return target


async def _scope_condition(db: AsyncSession, actor: ActionActor) -> ColumnElement[bool]:
    """The actor's binding as a condition on ``Collection``, after the per-call
    access re-check ``authorized_scope_filter`` makes."""
    context = IntegrationContext(
        user_id=actor.user_id,
        organization_id=actor.organization_id,
        project_id=actor.project_id,
        workspace_id=actor.workspace_id,
        # Only identity and binding are read; a native actor has no grant.
        grant_id=actor.grant_id or uuid4(),
        scopes=actor.scopes,
    )
    return await authorized_scope_filter(db, context)


async def _folders_holding(
    db: AsyncSession,
    document_id: UUID,
    organization_id: UUID,
    scope: ColumnElement[bool],
) -> list[UUID]:
    """Live Collections within ``scope`` holding the live document, by name.

    Documents are organization scoped: one of another organization is never
    found, whichever shared workspace it sits in.
    """
    found = await db.scalars(
        select(Collection.id)
        .join(CollectionDocument, CollectionDocument.collection_id == Collection.id)
        .join(Document, Document.id == CollectionDocument.document_id)
        .where(
            CollectionDocument.document_id == document_id,
            CollectionDocument.is_deleted.is_(False),
            Collection.is_deleted.is_(False),
            Document.organization_id == organization_id,
            Document.is_deleted.is_(False),
            scope,
        )
        .order_by(Collection.name, Collection.id)
    )
    return [UUID(str(value)) for value in found.all()]


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
    """The requester's own action with its stored target, for the decision page.

    ``summary`` is one sentence from the action's template (``_summary``);
    ``arguments`` are the stored arguments as they will run. The note fields
    (title, content, tags) are filled for a note only.
    """
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
    arguments = dict(row.arguments or {})
    folders = await _selected_folder_names(db, row)
    if row.project_id is not None and project_label is not None:
        folders[row.project_id] = str(project_label)
    names = _ReviewNames(
        folders=folders,
        papers=await _paper_titles(db, row),
        workspace=None if workspace_label is None else str(workspace_label),
    )
    note = row.tool_name == "create_project_note"
    return ActionReview(
        invocation_id=row.invocation_id,
        state=cast(Any, row.state),
        tool_name=row.tool_name,
        summary=_summary(row, names),
        arguments=arguments,
        project_id=row.project_id,
        project_label=None if row.project_id is None else str(project_label),
        workspace_id=row.workspace_id,
        workspace_label=None if row.workspace_id is None else str(workspace_label),
        # Kept visible so it can still be denied; approval would fail closed.
        project_available=not (project_deleted or workspace_deleted),
        title=str(arguments.get("title", "")) if note else "",
        content=str(arguments.get("content", "")) if note else "",
        tags=[str(t) for t in arguments.get("tags", [])] if note else [],
        requested_at=row.created_at,
        decided_at=row.decided_at,
        result=ToolResult.model_validate(row.result) if row.result else None,
        last_error=row.last_error,
    )


def _review_id(value: Any) -> UUID | None:
    """A stored id as a UUID; None for anything else, so a review never fails."""
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _review_ids(values: Iterable[Any]) -> set[UUID]:
    return {found for found in map(_review_id, values) if found is not None}


async def _selected_folder_names(
    db: AsyncSession, row: IntegrationToolAction
) -> dict[UUID, str]:
    """Names of the Collections the row's selectors name besides its target.

    Only within the row's own workspace: request_action let a workspace grant
    select its own workspace's Collections and a project grant only its
    project, whose name the review's own join already carries. A soft-deleted
    Collection keeps its name, as the target's label does.
    """
    if row.workspace_id is None:
        return {}
    arguments = row.arguments or {}
    wanted = _review_ids(arguments[key] for key in SELECTOR_KEYS if key in arguments)
    wanted -= {row.project_id}
    if not wanted:
        return {}
    found = await db.execute(
        select(Collection.id, Collection.name).where(
            Collection.id.in_(wanted), Collection.workspace_id == row.workspace_id
        )
    )
    return {UUID(str(folder_id)): str(name) for folder_id, name in found.all()}


async def _paper_titles(
    db: AsyncSession, row: IntegrationToolAction
) -> dict[UUID, str]:
    """Titles of the live documents of the row's organization its arguments
    name. Any other id (another organization's, a deleted one) stays unnamed:
    a harness may put any id in a request, and the page must not read out a
    title the requester could not open."""
    arguments = row.arguments or {}
    listed = arguments.get("document_ids")
    wanted = _review_ids(listed if isinstance(listed, list) else [])
    if arguments.get("document_id") is not None:
        wanted |= _review_ids([arguments["document_id"]])
    if not wanted:
        return {}
    found = await db.execute(
        select(Document.id, Document.title).where(
            Document.id.in_(wanted),
            Document.organization_id == row.organization_id,
            Document.is_deleted.is_(False),
        )
    )
    return {UUID(str(document_id)): str(title) for document_id, title in found.all()}


def _quoted(text: Any) -> str:
    return f"“{text}”"


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _listing(sentence: str, items: Iterable[str]) -> str:
    listed = ", ".join(items)
    return f"{sentence}: {listed}" if listed else sentence


@dataclass(frozen=True)
class _ReviewNames:
    """What a review may call the folders and papers its row points at."""

    folders: Mapping[UUID, str]
    papers: Mapping[UUID, str]
    workspace: str | None

    def folder(self, value: Any, noun: str = "folder") -> str:
        """``folder “Name”``; the stored id when the name is not known."""
        key = _review_id(value)
        name = self.folders.get(key) if key is not None else None
        return f"{noun} {value}" if name is None else f"{noun} {_quoted(name)}"

    def paper(self, value: Any) -> str:
        """``“Title”``; the stored id of a paper the review may not name."""
        key = _review_id(value)
        title = self.papers.get(key) if key is not None else None
        return str(value) if title is None else _quoted(title)

    def in_workspace(self) -> str:
        """`` in workspace “Name”``, or nothing when the row names none."""
        if self.workspace is None:
            return ""
        return f" in workspace {_quoted(self.workspace)}"


def _metadata_summary(arguments: Mapping[str, Any], names: _ReviewNames) -> str:
    paper = f"paper {names.paper(arguments.get('document_id'))}"
    tags = arguments.get("tags")
    listed = ", ".join(_quoted(t) for t in tags) if isinstance(tags, list) else ""
    if "title" in arguments:
        renamed = f"Rename {paper} to {_quoted(arguments['title'])}"
        if "tags" not in arguments:
            return renamed
        if listed:
            return f"{renamed} and set its tags to {listed}"
        return f"{renamed} and remove every tag"
    if listed:
        return f"Set the tags of {paper} to {listed}"
    if "tags" in arguments:
        return f"Remove every tag from {paper}"
    return f"Edit {paper}"


def _summary(row: IntegrationToolAction, names: _ReviewNames) -> str:
    """One sentence for the review page: the action's template, filled in.

    The folder acted on is the stored target, not an argument: a project
    grant's ingest may leave it implicit. Papers are listed in the order asked.
    """
    arguments = row.arguments or {}
    tool_name = row.tool_name
    asked = arguments.get("document_ids")
    document_ids = asked if isinstance(asked, list) else []
    papers = [names.paper(document_id) for document_id in document_ids]
    count = _counted(len(document_ids), "paper")
    target = names.folder(row.project_id)
    if tool_name == "create_project_note":
        title = _quoted(arguments.get("title", ""))
        if row.project_id is not None:
            return f"Create note {title} in {names.folder(row.project_id, 'project')}"
        return f"Create note {title}{names.in_workspace()}"
    if tool_name == "save_papers_to_folder":
        return _listing(f"Save {count} to {target}", papers)
    if tool_name == "remove_papers_from_folder":
        return _listing(f"Remove {count} from {target} (documents are kept)", papers)
    if tool_name == "move_papers_between_folders":
        destination = names.folder(arguments.get("to_project_id"))
        return _listing(f"Move {count} from {target} to {destination}", papers)
    if tool_name == "create_folder":
        name = _quoted(arguments.get("name", ""))
        return f"Create folder {name}{names.in_workspace()}"
    if tool_name == "rename_folder":
        return f"Rename {target} to {_quoted(arguments.get('name', ''))}"
    if tool_name == "delete_folder":
        return f"Delete {target} (documents are kept)"
    if tool_name == "update_document_metadata":
        return _metadata_summary(arguments, names)
    if tool_name == "ingest_arxiv_papers":
        asked = arguments.get("paper_ids")
        paper_ids = [str(p) for p in asked] if isinstance(asked, list) else []
        ingest = f"Ingest {_counted(len(paper_ids), 'arXiv paper')} into {target}"
        return _listing(ingest, paper_ids)
    return f"Run the action {_quoted(tool_name)}"


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


def _targets(row: IntegrationToolAction) -> set[UUID]:
    """Every Collection the stored action touches: its target and each selector."""
    arguments = row.arguments or {}
    targets = {
        UUID(str(arguments[key]))
        for key in SELECTOR_KEYS
        if arguments.get(key) is not None
    }
    if row.project_id is not None:
        targets.add(row.project_id)
    return targets


async def _target_reason(
    db: AsyncSession, row: IntegrationToolAction, grant: IntegrationGrant
) -> str | None:
    """A stable reason when the grant no longer reaches what the action
    touches, else None.

    Both bindings get the per-call access re-check every read makes
    (``authorized_scope_filter``): the binding's project, or its workspace,
    is not deleted, the user still owns the workspace or is a live member, and
    the user and both organizations are active. Matching grant and consent
    rows prove none of that, and most library effects only scope their own
    lookup. A row bound to one project was compared by that binding, so a
    selector may only name the same Collection. For a workspace-bound row,
    comparing only the binding would let the grant authorise whatever the row
    names, so every Collection it touches (a move's destination too) must
    still be one of the workspace's live Collections; a workspace-level action
    (``create_folder``) needs the workspace itself.
    """
    targets = _targets(row)
    project_bound = row.workspace_id is None
    if project_bound and row.project_id is None:
        return GRANT_DENIED  # bound to nothing: never authorisable
    context = IntegrationContext(
        user_id=row.user_id,
        organization_id=row.organization_id,
        project_id=row.project_id if project_bound else None,
        workspace_id=row.workspace_id,
        grant_id=grant.id,
    )
    try:
        scope = await authorized_scope_filter(db, context)
    except IntegrationAccessDenied:
        return GRANT_DENIED
    if project_bound:
        return None if targets <= {row.project_id} else GRANT_DENIED
    if not targets:
        return None
    reachable = set(
        (
            await db.scalars(
                select(Collection.id).where(
                    Collection.id.in_(targets),
                    scope,
                    Collection.is_deleted.is_(False),
                )
            )
        ).all()
    )
    return None if reachable == targets else GRANT_DENIED


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
        # The same per-action scope request_action checked: library:write
        # never runs a note, a deletion or an ingest.
        or not scope_allows(row.tool_name, grant.scopes)
    ):
        return GRANT_DENIED
    if grant.request_id is None:
        # No consent chain: the token itself must still be live.
        if grant.revoked_at is not None or not live(grant.expires_at):
            return "grant revoked or expired"
        return await _target_reason(db, row, grant)
    if row.consent_id is not None and row.consent_id != grant.request_id:
        return GRANT_DENIED
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
    if consent is None or not scope_allows(row.tool_name, consent.scopes):
        return "consent revoked"
    if grant.device_id is not None and consent.device_id != grant.device_id:
        return "consent revoked"
    return await _target_reason(db, row, grant)


async def _run_effect(
    db: AsyncSession, row: IntegrationToolAction, user: User
) -> dict[str, Any]:
    """The effect, in the caller's transaction: ``_finish`` commits it with
    the receipt, and a rollback discards both.

    Targets come from the stored row. A service that finds nothing to change,
    or refuses the user, yields a payload with a stable ``error``; anything
    else raises.
    """
    if row.tool_name == "create_project_note":
        from src.services.agent.tools_impl import _tool_create_project_note

        if row.project_id is None:
            # Never str() a missing project: the adapter resolves any
            # non-UUID, "None" included, as a project NAME.
            raise ToolActionError("action has no target project")
        # Project comes from the stored binding; the adapter re-checks edit
        # authority.
        return await _tool_create_project_note(
            {**row.arguments, "project_id": str(row.project_id)},
            db,
            user,
            commit=False,
        )
    effect = _LIBRARY_EFFECTS.get(row.tool_name)
    if effect is None:
        # A detached action (the ingest) never runs inside this transaction.
        raise ToolActionError("action has no transactional effect")
    try:
        return await effect(db, row, user)
    except PermissionError:
        return _refused(row, INSUFFICIENT_PERMISSIONS)


def _receipt(
    row: IntegrationToolAction, project_id: Any, **fields: Any
) -> dict[str, Any]:
    """What a library action did, in fixed fields, never a service's own text."""
    return {
        "ok": True,
        "tool_name": row.tool_name,
        "project_id": str(project_id),
        **fields,
    }


def _refused(row: IntegrationToolAction, reason: str) -> dict[str, Any]:
    return {"ok": False, "tool_name": row.tool_name, "error": reason}


async def _live_members(
    db: AsyncSession, collection_id: UUID, document_ids: list[str]
) -> set[str]:
    """Which of ``document_ids`` are live members of the Collection."""
    if not document_ids:
        return set()
    found = await db.scalars(
        select(CollectionDocument.document_id).where(
            CollectionDocument.collection_id == collection_id,
            CollectionDocument.document_id.in_([UUID(d) for d in document_ids]),
            CollectionDocument.is_deleted.is_(False),
        )
    )
    return {str(value) for value in found.all()}


# The collection services act as the row's user (``row.user_id``, whom
# _execute_row just verified active in the row's organization) and take
# ``workspace_id`` to scope their lookup to that parent: a workspace-bound row
# names its grant's workspace there, a project-bound one None (its binding is
# the Collection itself).
async def _save_papers(
    db: AsyncSession, row: IntegrationToolAction, user: User
) -> dict[str, Any]:
    folder = UUID(row.arguments["project_id"])
    requested = list(row.arguments["document_ids"])
    saved = await collection_service.add_documents_to_collection(
        db,
        folder,
        [UUID(d) for d in requested],
        row.user_id,
        workspace_id=row.workspace_id,
    )
    if saved is None:
        return _refused(row, TARGET_NOT_FOUND)
    # The service skips a document that is missing or not the organization's.
    held = await _live_members(db, folder, requested)
    return _receipt(
        row,
        folder,
        document_ids=[d for d in requested if d in held],
        skipped_document_ids=[d for d in requested if d not in held],
    )


async def _remove_papers(
    db: AsyncSession, row: IntegrationToolAction, user: User
) -> dict[str, Any]:
    folder = UUID(row.arguments["project_id"])
    requested = list(row.arguments["document_ids"])
    held = await _live_members(db, folder, requested)
    removed = await collection_service.remove_documents_from_collection(
        db,
        folder,
        [UUID(d) for d in requested],
        row.user_id,
        workspace_id=row.workspace_id,
    )
    if removed is None:
        return _refused(row, TARGET_NOT_FOUND)
    return _receipt(
        row,
        folder,
        document_ids=[d for d in requested if d in held],
        skipped_document_ids=[d for d in requested if d not in held],
    )


async def _move_papers(
    db: AsyncSession, row: IntegrationToolAction, user: User
) -> dict[str, Any]:
    """Unlink from the source, then link into the destination, in this one
    transaction: ``_finish`` commits both halves with the receipt and any
    failure rolls both back. Only papers that are in the source move."""
    source = UUID(row.arguments["from_project_id"])
    destination = UUID(row.arguments["to_project_id"])
    requested = list(row.arguments["document_ids"])
    held = await _live_members(db, source, requested)
    moving = [d for d in requested if d in held]
    ids = [UUID(d) for d in moving]
    unlinked = await collection_service.remove_documents_from_collection(
        db, source, ids, row.user_id, workspace_id=row.workspace_id
    )
    if unlinked is None:
        return _refused(row, TARGET_NOT_FOUND)
    linked = await collection_service.add_documents_to_collection(
        db, destination, ids, row.user_id, workspace_id=row.workspace_id
    )
    if linked is None or await _live_members(db, destination, moving) != set(moving):
        # A paper left the source but could not be linked (a deleted
        # document, say): refusing rolls both halves back.
        return _refused(row, TARGET_NOT_FOUND)
    return _receipt(
        row,
        source,
        from_project_id=str(source),
        to_project_id=str(destination),
        document_ids=moving,
        skipped_document_ids=[d for d in requested if d not in held],
    )


async def _create_folder(
    db: AsyncSession, row: IntegrationToolAction, user: User
) -> dict[str, Any]:
    if row.workspace_id is None:
        # Only a workspace grant may create one; its row names the workspace.
        raise ToolActionError("action has no target workspace")
    created = await collection_service.create_collection(
        db,
        CollectionCreate(
            workspace_id=row.workspace_id,
            name=row.arguments["name"],
            description=row.arguments.get("description"),
            color=None,
            icon=None,
        ),
        row.user_id,
    )
    if created is None:
        return _refused(row, TARGET_NOT_FOUND)
    return _receipt(
        row,
        created.id,
        workspace_id=str(row.workspace_id),
        name=str(created.name),
    )


async def _rename_folder(
    db: AsyncSession, row: IntegrationToolAction, user: User
) -> dict[str, Any]:
    folder = UUID(row.arguments["project_id"])
    renamed = await collection_service.update_collection(
        db,
        folder,
        CollectionUpdate(name=row.arguments["name"], color=None, icon=None),
        row.user_id,
        workspace_id=row.workspace_id,
    )
    if renamed is None:
        return _refused(row, TARGET_NOT_FOUND)
    return _receipt(row, folder, name=row.arguments["name"])


async def _delete_folder(
    db: AsyncSession, row: IntegrationToolAction, user: User
) -> dict[str, Any]:
    folder = UUID(row.arguments["project_id"])
    deleted = await collection_service.delete_collection(
        db, folder, row.user_id, workspace_id=row.workspace_id
    )
    if deleted is None:
        return _refused(row, TARGET_NOT_FOUND)
    return _receipt(row, folder)


async def _update_document_metadata(
    db: AsyncSession, row: IntegrationToolAction, user: User
) -> dict[str, Any]:
    """Edit the document itself; the target is the folder that put it in scope."""
    if row.project_id is None:
        raise ToolActionError("action has no target project")
    document_id = UUID(row.arguments["document_id"])
    scope = (
        Collection.workspace_id == row.workspace_id
        if row.workspace_id is not None
        else Collection.id == row.project_id
    )
    folders = await _folders_holding(db, document_id, row.organization_id, scope)
    if row.project_id not in folders:
        # It left the folder the action was aimed at.
        return _refused(row, TARGET_NOT_FOUND)
    updated = await file_metadata_service.update_file_metadata(
        db,
        document_id,
        user,
        title=row.arguments.get("title"),
        tags=row.arguments.get("tags"),
        commit=False,
    )
    if updated is None:
        return _refused(row, TARGET_NOT_FOUND)
    changed = {
        key: row.arguments[key] for key in ("title", "tags") if key in row.arguments
    }
    return _receipt(
        row,
        row.project_id,
        document_ids=[str(document_id)],
        # Every folder of the scope holding it, the target first by name.
        project_ids=[str(folder) for folder in folders],
        **changed,
    )


_LIBRARY_EFFECTS: Mapping[
    str,
    Callable[[AsyncSession, IntegrationToolAction, User], Awaitable[dict[str, Any]]],
] = MappingProxyType(
    {
        "save_papers_to_folder": _save_papers,
        "remove_papers_from_folder": _remove_papers,
        "move_papers_between_folders": _move_papers,
        "create_folder": _create_folder,
        "rename_folder": _rename_folder,
        "delete_folder": _delete_folder,
        "update_document_metadata": _update_document_metadata,
    }
)


def _source_refs(tool_name: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Observed identities only: the note created, or the documents touched."""
    if tool_name == "create_project_note":
        return [{"note_id": payload.get("note_id")}]
    return [{"document_id": d} for d in payload.get("document_ids", [])]


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


async def _current_status(db: AsyncSession, row_id: UUID) -> ActionStatus | None:
    row = await db.get(IntegrationToolAction, row_id, populate_existing=True)
    return _status(row) if row is not None else None


async def _fail_after_rollback(
    db: AsyncSession, row_id: UUID, reason: str, result: dict[str, Any] | None = None
) -> ActionStatus | None:
    await db.rollback()
    await _finish(db, row_id, state="failed", last_error=reason, result=result)
    return await _current_status(db, row_id)


async def _finish_unknown(db: AsyncSession, row_id: UUID) -> None:
    """An ingest that may have stored papers but returned no payload."""
    await db.rollback()
    await _finish(db, row_id, state="outcome_unknown", last_error=INGEST_UNKNOWN)


def _ingest_receipt(
    row: IntegrationToolAction, payload: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """The ingest's outcome in fixed fields, and its stable failure reason.

    Never the tool's messages: they quote exception text and per-paper causes.
    """
    status = payload.get("status")
    if status != _INGEST_COMPLETE and status not in _INGEST_REASONS:
        status = "ingestion_failed"
    reason = _INGEST_REASONS.get(str(status))
    requested = [str(paper_id) for paper_id in row.arguments["paper_ids"]]
    failed = {
        str(item.get("paper_id"))
        for item in payload.get("failed_papers") or []
        if isinstance(item, dict)
    }
    receipt: dict[str, Any] = {
        "ok": reason is None,
        "tool_name": row.tool_name,
        "project_id": str(row.project_id),
        "paper_ids": requested,
        "document_ids": [str(d) for d in payload.get("document_ids") or []],
        "failed_paper_ids": [p for p in requested if p in failed],
        "ingest_status": status,
        "atomicity": INGEST_ATOMICITY,
    }
    if reason is not None:
        receipt["error"] = reason
    return receipt, reason


async def _execute_detached(
    db: AsyncSession, row: IntegrationToolAction, user: User, log: dict[str, str]
) -> ActionStatus | None:
    """Run the arXiv ingest outside the action's transaction, then record it.

    Weaker atomicity than every other action, and the receipt says so: the
    ingest stores and links each paper in transactions of its own (and in
    object storage) before the receipt is written. The claim ``_execute_row``
    committed is its idempotency key: one worker ever starts it and nothing
    re-runs it. Papers that landed stay when the receipt reports a failure.

    The ingest gets ``DETACHED_TIMEOUT``. One that raises, outlasts it, or is
    cancelled with the drain (Celery's soft time limit, a worker shutdown)
    ends ``outcome_unknown`` (``INGEST_UNKNOWN``), recorded here rather than
    left ``executing`` for the sweeper; a cancellation is then re-raised.
    """
    from src.services.agent.tools_impl import _tool_ingest_arxiv

    row_id = row.id
    if row.project_id is None:
        # Never str() a missing project: the tool resolves names.
        return await _fail_after_rollback(db, row_id, "effect failed before commit")
    arguments = {
        "paper_ids": list(row.arguments["paper_ids"]),
        "project_id": str(row.project_id),
    }
    # End the authority check's read transaction: the ingest runs for minutes
    # and commits through sessions of its own.
    await db.commit()
    try:
        payload = await asyncio.wait_for(
            _tool_ingest_arxiv(arguments, str(user.id), db, user),
            timeout=DETACHED_TIMEOUT.total_seconds(),
        )
    except asyncio.CancelledError:
        # The drain itself is being cancelled: say what is known, then go.
        logger.error("integration ingest cancelled", extra=log)
        await _finish_unknown(db, row_id)
        raise
    except TimeoutError:
        logger.error("integration ingest timed out", extra=log)
        await _finish_unknown(db, row_id)
        return await _current_status(db, row_id)
    except Exception as error:  # noqa: BLE001 - papers may have landed: unknown
        logger.error("integration ingest raised", extra=log, exc_info=error)
        await _finish_unknown(db, row_id)
        return await _current_status(db, row_id)
    receipt, reason = _ingest_receipt(row, payload)
    result = ToolResult(
        content=[receipt],
        is_error=reason is not None,
        source_refs=_source_refs(row.tool_name, receipt),
    )
    if not await _finish(
        db,
        row_id,
        state="failed" if reason else "succeeded",
        last_error=reason,
        result=result.model_dump(mode="json"),
    ):
        logger.error(
            "integration action finished after being marked unknown", extra=log
        )
    return await _current_status(db, row_id)


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
        # The requester first: the authority re-check also refuses an
        # inactive or departed user, but only as a lost grant.
        user = await db.get(User, row.user_id)
        reason: str | None
        if user is None or not user.is_active or user.is_deleted:
            reason = "requesting user is not active"
        elif user.organization_id != row.organization_id:
            reason = "requesting user left the organization"
        else:
            reason = await _authority_intact(db, row)
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
    if row.tool_name in DETACHED_ACTIONS:
        return await _execute_detached(db, row, user, invocation)

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
        source_refs=_source_refs(row.tool_name, payload),
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
    return await _current_status(db, row_id)


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
    rows wait instead of running. One drain starts rows for ``DRAIN_BUDGET``
    at most, and an ingest only while its whole ``DETACHED_TIMEOUT`` still
    fits: rows run one after another, so an ingest claimed late in a busy
    drain would otherwise get whatever time the earlier rows left. A row it
    leaves stays approved, unclaimed, for a later drain (the beat starts one
    every two seconds).
    """
    if not settings.NOUS_MCP_ENABLED:
        return 0
    deadline = _clock() + DRAIN_BUDGET.total_seconds()
    rows = (
        await db.execute(
            select(IntegrationToolAction.id, IntegrationToolAction.tool_name)
            .where(
                IntegrationToolAction.state == "approved",
                IntegrationToolAction.is_deleted.is_(False),
            )
            .order_by(IntegrationToolAction.decided_at)
            .limit(limit)
        )
    ).all()
    finished = 0
    for row_id, tool_name in rows:
        left = deadline - _clock()
        if left <= 0:
            break
        if tool_name in DETACHED_ACTIONS and left < DETACHED_TIMEOUT.total_seconds():
            continue
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
