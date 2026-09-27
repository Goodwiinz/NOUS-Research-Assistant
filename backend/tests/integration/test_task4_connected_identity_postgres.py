"""Connected Task 4 identity, compaction, checkpoint, and scope proof."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_tool_receipt import AgentToolOperation
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.organization import Organization
from src.models.project_note import ProjectNote
from src.services.agent import _nodes_classify, _nodes_llm, _nodes_tools, compactor
from src.services.agent.compactor import _COMPACTED_FLAG
from src.services.agent.tools import TOOL_REGISTRY
from tests.integration.test_draft_source_scope_postgres import _postgres_draft_schema

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_postgres,
    pytest.mark.asyncio,
]


class _PromptModel:
    def __init__(self, response: AIMessage) -> None:
        self.response = response
        self.calls: list[list[Any]] = []

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> _PromptModel:
        del tools, kwargs
        return self

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        del kwargs
        self.calls.append(list(messages))
        return self.response


class _CompactionModel:
    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        del messages, kwargs
        return AIMessage(content="Compacted fixture search results.")


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "name": name, "args": args, "type": "tool_call"}


def _tool_json(message: ToolMessage) -> dict[str, Any]:
    content = message.content
    if not isinstance(content, str):
        raise AssertionError("tool result content must be serialized JSON text")
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise AssertionError("tool result must be a JSON object")
    return payload


def _merge_update(state: dict[str, Any], update: dict[str, Any]) -> None:
    from langgraph.graph.message import add_messages

    if "messages" in update:
        state["messages"] = add_messages(state.get("messages", []), update["messages"])
    state.update({key: value for key, value in update.items() if key != "messages"})


def _checkpoint_state() -> dict[str, Any]:
    metadata = TOOL_REGISTRY.metadata_snapshot()
    return {
        "messages": [],
        "page_context": {},
        "retrieved_contexts": [],
        "attachment_ids": [],
        "attachment_status": [],
        "tool_executions": [],
        "thread_id": "task4-connected-identity",
        "thread_persistence": "ephemeral",
        "turn_index": 0,
        "tool_loop_count": 0,
        "error_count": 0,
        "last_error": "",
        "pending_confirmation": {},
        "user_confirmed": False,
        "intent": "research",
        "user_memories": [],
        "project_memories": [],
        "plan": [],
        "plan_reasoning": "",
        "reflection_count": 0,
        "compaction_count": 0,
        "intent_confidence": 1.0,
        "last_error_info": {},
        "user_id": "",
        "current_project_id": "",
        "model": "",
        "use_rag": False,
        "runtime_snapshot_id": "",
        "runtime_tool_names": list(TOOL_REGISTRY.available_descriptor_names()),
        "tool_registry_hash": metadata["hash"],
        "tool_registry_version": metadata["version"],
        "runtime_projection_unavailable": False,
        "project_skill_catalog": [],
        "loaded_skill_versions": [],
        "capability_limitation": {},
        "_reflection_result": None,
        "_force_synthesis_fired": False,
        "tools_all_deduped": False,
        "tool_operation_protocol_version": 1,
        "tool_operation_turn_id": "t0",
        "identity_ledger": {"version": 1, "records": [], "processed": []},
        "identity_current_references": [],
    }


async def test_search_identity_survives_real_compaction_checkpoint_and_scoped_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    async with _postgres_draft_schema(dsn) as database:
        async_engine = database.factory.kw["bind"]
        async with async_engine.begin() as schema_connection:
            await schema_connection.run_sync(
                lambda connection: ProjectNote.__table__.create(connection)
            )
            await schema_connection.run_sync(
                lambda connection: AgentToolOperation.__table__.create(connection)
            )

        foreign_id = uuid4()
        deleted_id = database.document_ids[2]
        foreign_org_id = uuid4()
        bulk_document_ids: list[str] = []
        async with database.factory() as setup:
            await setup.execute(
                text("""INSERT INTO organizations (
                        id, created_at, updated_at, is_deleted, name, storage_tier,
                        storage_used_bytes, storage_limit_bytes, is_active
                    ) VALUES (
                        :id, now(), now(), false, 'foreign test organization',
                        'FREE', 0, 10737418240, true
                    )"""),
                {"id": foreign_org_id},
            )
            setup.add(
                Document(
                    id=foreign_id,
                    title="Foreign tenant fixture source",
                    filename="foreign.txt",
                    file_path="/unused/foreign.txt",
                    file_size_bytes=1,
                    mime_type="text/plain",
                    document_type=DocumentType.TEXT,
                    processing_status=ProcessingStatus.COMPLETED,
                    organization_id=foreign_org_id,
                    is_deleted=False,
                )
            )
            deleted = await setup.get(Document, deleted_id)
            assert deleted is not None
            setattr(deleted, "is_deleted", True)
            now = datetime.now(timezone.utc)
            for index in range(50):
                label = (
                    "R6 middle-only target identity"
                    if index == 34
                    else f"R6 scoped evidence result {index:02}"
                )
                if index == 34:
                    title = (
                        "R6 middle-only target identity alpha beta gamma delta "
                        "epsilon zeta"
                    )
                else:
                    title_prefix = f"{label}; alpha beta gamma delta epsilon zeta; "
                    title = (
                        title_prefix
                        + ("x" * max(0, 128 - len(title_prefix)))
                        + ("α" * 372)
                    )[:500]
                document_id = uuid4()
                bulk_document_ids.append(str(document_id))
                setup.add(
                    Document(
                        id=document_id,
                        title=title,
                        filename=f"r6-{index:02}.txt",
                        file_path=f"/unused/r6-{index:02}.txt",
                        file_size_bytes=len(title),
                        mime_type="text/plain",
                        document_type=DocumentType.TEXT,
                        processing_status=ProcessingStatus.COMPLETED,
                        content_text="fixture text",
                        content_summary="fixture text",
                        organization_id=database.organization_id,
                        is_deleted=False,
                        created_at=now + timedelta(seconds=index),
                    )
                )
            await setup.commit()

        from src.services.agent import tool_session

        @asynccontextmanager
        async def scoped_tool_session() -> AsyncIterator[AsyncSession]:
            async with database.factory() as session:
                yield session

        monkeypatch.setattr(tool_session, "tool_session", scoped_tool_session)
        target_id = bulk_document_ids[34]

        state = _checkpoint_state()
        state["user_id"] = str(database.user_id)
        state["messages"] = [
            HumanMessage(
                id="t0",
                content="Search external records for the named local source.",
            ),
        ]
        config = {
            "configurable": {
                "thread_id": "task4-r6-thread",
                "user_id": str(database.user_id),
                "organization_id": str(database.organization_id),
            }
        }
        queries = ["alpha", "beta", "gamma", "delta", "epsilon"]
        initial_calls = [
            _tool_call(
                f"c{index}",
                "search_documents",
                {"query": query, "max_results": 50},
            )
            for index, query in enumerate(queries)
        ]
        state["messages"].append(AIMessage(content="", tool_calls=initial_calls))
        _merge_update(state, await _nodes_tools.tool_node(state, config))
        first_results = [
            message for message in state["messages"] if isinstance(message, ToolMessage)
        ]
        assert len(first_results) == len(queries)
        first_payload = _tool_json(first_results[0])
        assert "_tool_result_bounds" in first_payload, first_payload
        bounds = first_payload["_tool_result_bounds"]["identity_entries"]
        retained_target = [entry for entry in bounds if entry["id"] == target_id]
        assert len(retained_target) == 1, {
            "target_id": target_id,
            "target_insert_index": 34,
            "bound_count": len(bounds),
            "paths": [entry.get("path") for entry in bounds],
            "ids": [entry.get("id") for entry in bounds],
        }
        retained_index = bounds.index(retained_target[0])
        assert retained_index == 15
        assert (
            retained_target[0]
            .get("label", "")
            .startswith("R6 middle-only target identity")
        )
        assert retained_target[0].get("path") == "/documents/15"

        state["messages"].append(
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "c-end",
                        "search_documents",
                        {"query": "zeta", "max_results": 50},
                    )
                ],
            )
        )
        _merge_update(state, await _nodes_tools.tool_node(state, config))
        monkeypatch.setattr(compactor, "_build_compactor_llm", _CompactionModel)
        compact_update = await compactor.make_compactor_node()(state, config)
        _merge_update(state, compact_update)
        assert state["compaction_count"] == 1
        assert any(
            message.additional_kwargs.get(_COMPACTED_FLAG)
            for message in state["messages"]
            if isinstance(message, ToolMessage)
        )

        # Put the actual bounded node state into a LangGraph checkpoint, then
        # transfer the saved checkpoint through the production serializer.
        serde = JsonPlusSerializer()
        saver_before = MemorySaver(serde=serde)
        saver_after = MemorySaver(serde=JsonPlusSerializer())
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = state
        versions: dict[str, str | int | float] = {name: 1 for name in state}
        checkpoint["channel_versions"] = versions
        checkpoint["versions_seen"] = {}
        checkpoint_config = cast(
            RunnableConfig,
            {
                "configurable": {
                    "thread_id": "task4-r6-checkpoint",
                    "checkpoint_ns": "",
                }
            },
        )
        metadata = cast(
            CheckpointMetadata,
            {"source": "update", "step": 1, "writes": {}, "parents": {}},
        )
        saved_config = await saver_before.aput(
            checkpoint_config, checkpoint, metadata, versions
        )
        saved = await saver_before.aget_tuple(saved_config)
        assert saved is not None
        serialized = serde.dumps_typed(saved.checkpoint)
        restored_checkpoint = JsonPlusSerializer().loads_typed(serialized)
        restored_metadata = JsonPlusSerializer().loads_typed(
            JsonPlusSerializer().dumps_typed(saved.metadata)
        )
        reloaded_config = await saver_after.aput(
            saved.config,
            restored_checkpoint,
            restored_metadata,
            restored_checkpoint["channel_versions"],
        )
        reloaded = await saver_after.aget_tuple(reloaded_config)
        assert reloaded is not None
        resumed_state = reloaded.checkpoint["channel_values"]
        assert resumed_state["identity_ledger"]["records"]
        assert any(
            record["id"] == target_id
            for record in resumed_state["identity_ledger"]["records"]
        )

        # Start a named follow-up through the real preprocessing and shared
        # prompt renderer. Only its RAG/classification/memory providers are
        # fixed so this test never accesses external services.
        async def empty_rag(*args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            return {"retrieved_contexts": []}

        async def research_classification(*args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            return {"intent": "general", "intent_confidence": 1.0}

        async def empty_memory(*args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            return {"user_memories": []}

        monkeypatch.setattr(_nodes_classify, "rag_node", empty_rag)
        monkeypatch.setattr(_nodes_classify, "_classify_core", research_classification)
        monkeypatch.setattr(_nodes_classify, "memory_retrieval_node", empty_memory)
        followup = HumanMessage(
            id="t1",
            content=(
                'Retrieve evidence from "R6 middle-only target identity alpha beta '
                'gamma delta epsilon zeta".'
            ),
        )
        resumed_state["messages"].append(followup)
        _merge_update(
            resumed_state,
            await _nodes_classify.preprocessing_node(resumed_state, config),
        )
        assert target_id in resumed_state["identity_current_references"]
        model = _PromptModel(
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "r6-authorized",
                        "do_kb_retrieve",
                        {"query": "target", "document_ids": [target_id]},
                    ),
                    _tool_call(
                        "r6-foreign",
                        "do_kb_retrieve",
                        {"query": "foreign", "document_ids": [str(foreign_id)]},
                    ),
                    _tool_call(
                        "r6-deleted",
                        "do_kb_retrieve",
                        {"query": "deleted", "document_ids": [str(deleted_id)]},
                    ),
                ],
            )
        )
        from src.services.agent import graph as graph_module
        from src.services.agent import llm_factory

        monkeypatch.setattr(
            graph_module, "_build_llm", lambda model_override=None: model
        )
        monkeypatch.setattr(
            llm_factory, "resolve_chat_deployment", lambda *_args: "offline-test-model"
        )
        monkeypatch.setattr(
            llm_factory, "get_synthesis_model_name", lambda: "offline-test-model"
        )
        monkeypatch.setattr(llm_factory, "build_synthesis_llm", lambda **_kwargs: model)
        prompt_update = await _nodes_llm.llm_node(resumed_state, config)
        _merge_update(resumed_state, prompt_update)
        captured_system = "\n".join(
            str(message.content)
            for message in model.calls[0]
            if isinstance(message, SystemMessage)
        )
        assert target_id in captured_system
        assert "R6 middle-only target identity" in captured_system

        from src.core.config import settings

        monkeypatch.setattr(settings, "DO_KB_ENABLED", False)
        monkeypatch.setattr(settings, "BEDROCK_KB_ID", "")
        _merge_update(
            resumed_state, await _nodes_tools.tool_node(resumed_state, config)
        )
        followup_results = {
            message.tool_call_id: _tool_json(message)
            for message in resumed_state["messages"]
            if isinstance(message, ToolMessage)
            and message.tool_call_id in {"r6-authorized", "r6-foreign", "r6-deleted"}
        }
        assert (
            followup_results["r6-authorized"]["reason"]
            == "scoped_retrieval_unavailable"
        )
        assert (
            followup_results["r6-foreign"]["reason"]
            == "requested_documents_unavailable"
        )
        assert (
            followup_results["r6-deleted"]["reason"]
            == "requested_documents_unavailable"
        )

        # Execute an actual registered mutation through the writing specialist
        # node. The tool dispatcher creates the PostgreSQL note and persists its
        # completed operation result before the graph checkpoint is written.
        # Reusing the exact provider call identity must return that receipt
        # without creating another note, while the ledger recognizes the same
        # tool observation only once.
        specialist_state = dict(resumed_state)
        specialist_state["intent"] = "writing"
        specialist_state["tool_operation_protocol_version"] = 1
        note_call = _tool_call(
            "r6-note-mutation",
            "create_project_note",
            {
                "title": "Task4 connected receipt note",
                "content": "Created through the real writing tool node.",
                "project_id": str(database.project_id),
            },
        )
        specialist_state["messages"] = [
            *specialist_state["messages"],
            AIMessage(content="", tool_calls=[note_call]),
        ]
        writing_tool_node = _nodes_tools.make_filtered_tool_node(branch="writing")
        first_note_update = await writing_tool_node(specialist_state, config)
        _merge_update(specialist_state, first_note_update)
        first_note_message = next(
            message
            for message in reversed(specialist_state["messages"])
            if isinstance(message, ToolMessage)
            and message.tool_call_id == "r6-note-mutation"
        )
        first_note_result = _tool_json(first_note_message)
        assert first_note_result["status"] == "success", first_note_result
        assert first_note_result["title"] == "Task4 connected receipt note"
        assert first_note_result["project_id"] == str(database.project_id)
        first_ledger = specialist_state["identity_ledger"]
        note_identity = next(
            record
            for record in first_ledger["records"]
            if record["kind"] == "note" and record["id"] == first_note_result["note_id"]
        )
        assert note_identity["name"] == "Task4 connected receipt note"
        assert note_identity["related"] == {"project_id": str(database.project_id)}

        async with database.factory() as verify_first:
            note_rows = (
                await verify_first.execute(
                    text(
                        "SELECT id FROM project_notes WHERE title = :title "
                        "AND project_id = :project_id"
                    ),
                    {
                        "title": "Task4 connected receipt note",
                        "project_id": database.project_id,
                    },
                )
            ).all()
            operation_rows = (
                (
                    await verify_first.execute(
                        text(
                            "SELECT state, result FROM agent_tool_operations "
                            "WHERE tool_call_id = :call_id"
                        ),
                        {"call_id": "r6-note-mutation"},
                    )
                )
                .mappings()
                .all()
            )
        assert len(note_rows) == 1
        assert len(operation_rows) == 1
        assert operation_rows[0]["state"] == "completed"
        assert operation_rows[0]["result"] == first_note_result

        specialist_state["messages"].append(
            AIMessage(content="", tool_calls=[note_call])
        )
        replay_update = await writing_tool_node(specialist_state, config)
        _merge_update(specialist_state, replay_update)
        replay_message = next(
            message
            for message in reversed(specialist_state["messages"])
            if isinstance(message, ToolMessage)
            and message.tool_call_id == "r6-note-mutation"
        )
        assert _tool_json(replay_message) == first_note_result
        assert specialist_state["identity_ledger"] == first_ledger
        async with database.factory() as verify_replay:
            replayed_note_rows = (
                await verify_replay.execute(
                    text(
                        "SELECT id FROM project_notes WHERE title = :title "
                        "AND project_id = :project_id"
                    ),
                    {
                        "title": "Task4 connected receipt note",
                        "project_id": database.project_id,
                    },
                )
            ).all()
            replayed_operation_count = (
                await verify_replay.execute(
                    text(
                        "SELECT count(*) FROM agent_tool_operations "
                        "WHERE tool_call_id = :call_id AND state = 'completed'"
                    ),
                    {"call_id": "r6-note-mutation"},
                )
            ).scalar_one()
        assert len(replayed_note_rows) == 1
        assert replayed_operation_count == 1
