"""Durable, tenant-scoped claims for agent business mutations.

The functions in this module participate in a caller-owned transaction and
never commit it. Identity is scoped to an authenticated actor, owned thread,
checkpointed user turn, and provider call ID. Tool name and argument hash are
compared fingerprints, not identity components.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_tool_receipt import AgentToolOperation

OperationState = Literal["claimed", "dispatched", "completed", "unknown"]
ClaimStatus = Literal["claimed", "completed", "pending", "unknown", "conflict"]


def _canonical_json(value: Any, *, depth: int = 0) -> str:
    """Serialize strict JSON without changing strings, nulls, or list order."""
    if depth > 64:
        raise ValueError("tool arguments exceed the maximum JSON nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, float):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, list):
        return (
            "["
            + ",".join(_canonical_json(item, depth=depth + 1) for item in value)
            + "]"
        )
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("tool argument object keys must be strings")
        fields = (
            json.dumps(key, ensure_ascii=False)
            + ":"
            + _canonical_json(value[key], depth=depth + 1)
            for key in sorted(value)
        )
        return "{" + ",".join(fields) + "}"
    raise ValueError(f"tool arguments must be JSON values, got {type(value).__name__}")


def arguments_hash(arguments: dict[str, Any]) -> str:
    """Hash effective validated arguments using exact JSON semantics."""
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be a JSON object")
    return hashlib.sha256(_canonical_json(arguments).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolOperationKey:
    """Immutable identity and effective-argument fingerprint for one call."""

    organization_id: uuid.UUID | None
    user_id: uuid.UUID
    thread_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    args_hash: str

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("thread_id", self.thread_id, 128),
            ("turn_id", self.turn_id, 128),
            ("tool_call_id", self.tool_call_id, 128),
            ("tool_name", self.tool_name, 64),
        ):
            if not isinstance(value, str) or not value or len(value) > maximum:
                raise ValueError(f"{label} must contain 1 to {maximum} characters")
        if len(self.args_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.args_hash
        ):
            raise ValueError("args_hash must be a lowercase SHA-256 hex digest")

    @property
    def operation_id(self) -> str:
        """Versioned digest of actor/thread/turn/provider-call identity."""
        payload = [
            1,
            str(self.organization_id) if self.organization_id is not None else None,
            str(self.user_id),
            self.thread_id,
            self.turn_id,
            self.tool_call_id,
        ]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def from_context(
        cls,
        *,
        organization_id: str | uuid.UUID | None,
        user_id: str | uuid.UUID,
        thread_id: str,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> "ToolOperationKey":
        """Construct a key from trusted context and effective arguments."""
        organization_uuid = uuid.UUID(str(organization_id)) if organization_id else None
        return cls(
            organization_id=organization_uuid,
            user_id=uuid.UUID(str(user_id)),
            thread_id=thread_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args_hash=arguments_hash(arguments),
        )


@dataclass(frozen=True)
class OperationClaim:
    """Result of inspecting or winning an operation claim."""

    status: ClaimStatus
    operation_id: str
    owner_token: uuid.UUID | None = None
    result: dict[str, Any] | None = None
    same_identity: bool = True


def _insert_for_session(db: AsyncSession) -> Any:
    bind = db.get_bind()
    if bind.dialect.name == "sqlite":
        return sqlite_insert(AgentToolOperation)
    return pg_insert(AgentToolOperation)


def _key_values(key: ToolOperationKey) -> dict[str, Any]:
    return {
        "operation_id": key.operation_id,
        "organization_id": key.organization_id,
        "user_id": key.user_id,
        "thread_id": key.thread_id,
        "turn_id": key.turn_id,
        "tool_call_id": key.tool_call_id,
        "tool_name": key.tool_name,
        "args_hash": key.args_hash,
    }


def _identity_matches(row: AgentToolOperation, key: ToolOperationKey) -> bool:
    return (
        cast(uuid.UUID | None, row.organization_id) == key.organization_id
        and cast(uuid.UUID, row.user_id) == key.user_id
        and cast(str, row.thread_id) == key.thread_id
        and cast(str, row.turn_id) == key.turn_id
        and cast(str, row.tool_call_id) == key.tool_call_id
    )


def _json_result(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


async def _set_bounded_lock_wait(db: AsyncSession) -> None:
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(select(func.set_config("lock_timeout", "2000ms", True)))


async def _lock_external_fingerprint(db: AsyncSession, key: ToolOperationKey) -> None:
    """Serialize same-turn same-tool argument checks on PostgreSQL."""
    if db.get_bind().dialect.name != "postgresql":
        return
    scope = [
        1,
        str(key.organization_id) if key.organization_id is not None else None,
        str(key.user_id),
        key.thread_id,
        key.turn_id,
        key.tool_name,
        key.args_hash,
    ]
    digest = hashlib.sha256(
        json.dumps(scope, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(digest, 0)))
    )


async def claim_operation(
    db: AsyncSession,
    key: ToolOperationKey,
    *,
    external: bool = False,
) -> OperationClaim:
    """Claim once or return the existing safe observation; never commit.

    External claims additionally serialize an identical same-turn fingerprint
    across fresh provider call IDs and conservatively block uncertain prior
    dispatches.
    """
    await _set_bounded_lock_wait(db)
    if external:
        await _lock_external_fingerprint(db, key)

    identity = _key_values(key)
    found = await db.execute(
        select(AgentToolOperation).where(
            AgentToolOperation.operation_id == key.operation_id
        )
    )
    winner = found.scalar_one_or_none()
    if winner is not None:
        if not _identity_matches(winner, key):
            return OperationClaim("conflict", key.operation_id)
        if winner.tool_name != key.tool_name or winner.args_hash != key.args_hash:
            return OperationClaim("conflict", key.operation_id)
        return _claim_from_row(winner, same_identity=True)

    if external:
        rows_result = await db.execute(
            select(AgentToolOperation)
            .where(
                (
                    AgentToolOperation.organization_id.is_(None)
                    if key.organization_id is None
                    else AgentToolOperation.organization_id == key.organization_id
                ),
                AgentToolOperation.user_id == key.user_id,
                AgentToolOperation.thread_id == key.thread_id,
                AgentToolOperation.turn_id == key.turn_id,
                AgentToolOperation.tool_name == key.tool_name,
                AgentToolOperation.args_hash == key.args_hash,
                AgentToolOperation.operation_id != key.operation_id,
            )
            .order_by(AgentToolOperation.created_at.desc())
        )
        for prior in rows_result.scalars():
            claim = _external_barrier_claim(prior)
            if claim is not None:
                return claim

    owner_token = uuid.uuid4()
    statement = _insert_for_session(db).values(
        **identity,
        state="claimed",
        owner_token=owner_token,
        result=None,
    )
    result = await db.execute(
        statement.on_conflict_do_nothing(index_elements=["operation_id"])
    )
    if cast(CursorResult[Any], result).rowcount == 1:
        return OperationClaim(
            "claimed", key.operation_id, owner_token=owner_token, same_identity=True
        )

    # ON CONFLICT waits for an in-flight winner; read its committed row and
    # compare every scope/fingerprint field before returning any result.
    found = await db.execute(
        select(AgentToolOperation).where(
            AgentToolOperation.operation_id == key.operation_id
        )
    )
    winner = found.scalar_one_or_none()
    if winner is None:
        raise RuntimeError("operation claim conflict winner could not be read")
    if not _identity_matches(winner, key):
        return OperationClaim("conflict", key.operation_id)
    if winner.tool_name != key.tool_name or winner.args_hash != key.args_hash:
        return OperationClaim("conflict", key.operation_id)
    return _claim_from_row(winner, same_identity=True)


def _external_barrier_claim(row: AgentToolOperation) -> OperationClaim | None:
    state = cast(str, row.state)
    operation_id = cast(str, row.operation_id)
    owner_token = cast(uuid.UUID | None, row.owner_token)
    if state in {"claimed", "dispatched", "unknown"}:
        status: ClaimStatus = "pending" if state == "dispatched" else "unknown"
        return OperationClaim(
            status,
            operation_id,
            owner_token=owner_token,
            result=_json_result(row.result) if status == "pending" else None,
            same_identity=False,
        )
    result = _json_result(row.result)
    if state == "completed" and result is not None and "error" in result:
        return OperationClaim(
            "completed",
            operation_id,
            result=result,
            same_identity=False,
        )
    return None


def _claim_from_row(row: AgentToolOperation, *, same_identity: bool) -> OperationClaim:
    state = cast(str, row.state)
    operation_id = cast(str, row.operation_id)
    owner_token = cast(uuid.UUID | None, row.owner_token)
    if state == "completed":
        return OperationClaim(
            "completed",
            operation_id,
            owner_token=owner_token,
            result=_json_result(row.result),
            same_identity=same_identity,
        )
    if state == "dispatched":
        return OperationClaim(
            "pending",
            operation_id,
            owner_token=owner_token,
            result=_json_result(row.result),
            same_identity=same_identity,
        )
    if state == "unknown":
        return OperationClaim("unknown", operation_id, same_identity=same_identity)
    return OperationClaim("pending", operation_id, same_identity=same_identity)


async def _cas_operation(
    db: AsyncSession,
    key: ToolOperationKey,
    owner_token: uuid.UUID,
    *,
    state: OperationState,
    result: dict[str, Any] | None,
    allowed_states: tuple[str, ...],
) -> None:
    statement = (
        update(AgentToolOperation)
        .where(
            AgentToolOperation.operation_id == key.operation_id,
            (
                AgentToolOperation.organization_id.is_(None)
                if key.organization_id is None
                else AgentToolOperation.organization_id == key.organization_id
            ),
            AgentToolOperation.user_id == key.user_id,
            AgentToolOperation.thread_id == key.thread_id,
            AgentToolOperation.turn_id == key.turn_id,
            AgentToolOperation.tool_call_id == key.tool_call_id,
            AgentToolOperation.tool_name == key.tool_name,
            AgentToolOperation.args_hash == key.args_hash,
            AgentToolOperation.owner_token == owner_token,
            AgentToolOperation.state.in_(allowed_states),
        )
        .values(state=state, result=result, updated_at=func.now())
    )
    result_proxy = await db.execute(statement)
    if cast(CursorResult[Any], result_proxy).rowcount != 1:
        raise RuntimeError("operation state update lost its claim or fingerprint")


async def complete_operation(
    db: AsyncSession,
    key: ToolOperationKey,
    owner_token: uuid.UUID,
    result: dict[str, Any],
) -> None:
    """Store the exact bounded result under the winning claim; never commit."""
    _canonical_json(result)
    await _cas_operation(
        db,
        key,
        owner_token,
        state="completed",
        result=result,
        allowed_states=("claimed", "dispatched"),
    )


async def record_dispatch(
    db: AsyncSession,
    key: ToolOperationKey,
    owner_token: uuid.UUID,
    result: dict[str, Any],
) -> None:
    """Record a recoverable external task identity before waiting; no commit."""
    _canonical_json(result)
    await _cas_operation(
        db,
        key,
        owner_token,
        state="dispatched",
        result=result,
        allowed_states=("claimed",),
    )


async def mark_unknown(
    db: AsyncSession,
    key: ToolOperationKey,
    owner_token: uuid.UUID,
) -> None:
    """Make an uncertain claim permanently non-replayable; never commit."""
    await _cas_operation(
        db,
        key,
        owner_token,
        state="unknown",
        result=None,
        allowed_states=("claimed", "dispatched"),
    )


__all__ = [
    "ClaimStatus",
    "OperationClaim",
    "ToolOperationKey",
    "arguments_hash",
    "claim_operation",
    "complete_operation",
    "mark_unknown",
    "record_dispatch",
]
