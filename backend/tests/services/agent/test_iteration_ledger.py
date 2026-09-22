"""Phase 9.B — per-turn iteration ledger writes JSON to disk."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


@pytest.fixture
def ledger_dir(tmp_path, monkeypatch):
    """Point settings.AGENT_LEDGER_DIR at a tmp dir.

    settings is a module-level singleton (not lru_cached), so we patch
    the attribute directly and let monkeypatch revert after the test.
    """
    from src.core.config import settings

    monkeypatch.setattr(settings, "AGENT_LEDGER_DIR", str(tmp_path))
    yield tmp_path


def _state_with_one_turn() -> dict:
    return {
        "messages": [
            HumanMessage(content="find papers on transformers"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "search_arxiv",
                        "args": {"query": "transformers"},
                    }
                ],
            ),
            ToolMessage(content='{"papers":[]}', tool_call_id="call_1"),
            AIMessage(content="I found 0 transformer papers."),
        ],
        "page_context": {"type": "chat", "project_id": None},
        "current_project_id": None,
        "model": "gpt-5",
        "intent": "research",
        "intent_confidence": 0.95,
        "tool_loop_count": 1,
        "error_count": 0,
        "tool_executions": [
            {
                "id": "call_1",
                "tool_name": "search_arxiv",
                "args": {"query": "transformers"},
                "status": "completed",
                "result": {"papers": []},
                "duration_ms": 100,
            }
        ],
        "retrieved_contexts": [],
        "user_memories": [{"key": "memk", "value": {"q": "x"}}],
        "_reflection_result": {"passed": True, "issues": [], "severity": "none"},
        "last_error": "",
        "last_error_info": {},
        "compaction_count": 0,
        "reflection_count": 0,
        "plan": [],
    }


@pytest.mark.unit
def test_write_iteration_creates_file(ledger_dir: Path):
    from src.services.agent.iteration_ledger import write_iteration

    path = write_iteration("thread-abc", _state_with_one_turn())
    assert path is not None
    assert path.exists()
    assert path.parent == ledger_dir / "thread-abc" / "iterations"
    assert path.name == "0001.json"

    record = json.loads(path.read_text())
    assert record["turn"] == 1
    assert record["summary"]["intent"] == "research"
    assert record["summary"]["user_query"] == "find papers on transformers"
    assert record["summary"]["tools_used"] == ["search_arxiv"]
    assert record["state_snapshot"]["page_context"]["type"] == "chat"


@pytest.mark.unit
def test_write_iteration_appends_with_increment(ledger_dir: Path):
    from src.services.agent.iteration_ledger import write_iteration

    state = _state_with_one_turn()
    write_iteration("thread-abc", state)
    write_iteration("thread-abc", state)
    path3 = write_iteration("thread-abc", state)
    assert path3 is not None
    assert path3.name == "0003.json"


@pytest.mark.unit
def test_next_turn_ignores_leaked_zero_byte_placeholder(ledger_dir: Path):
    """A 0-byte placeholder left by a crashed claim must not inflate the
    turn counter: the scan skips it, so the next real turn does not jump
    past it."""
    from src.services.agent.iteration_ledger import write_iteration

    # Simulate a leaked high-numbered placeholder with no real records yet.
    iter_dir = ledger_dir / "thread-ghost" / "iterations"
    iter_dir.mkdir(parents=True, exist_ok=True)
    (iter_dir / "0005.json").touch()
    assert (iter_dir / "0005.json").stat().st_size == 0

    # Without the skip, max(existing)=5 would push the next turn to 0006.
    p = write_iteration("thread-ghost", _state_with_one_turn())
    assert p is not None and p.name == "0001.json"
    assert p.stat().st_size > 0


@pytest.mark.unit
def test_write_iteration_disabled_when_dir_unset(monkeypatch, tmp_path):
    """No AGENT_LEDGER_DIR → write_iteration returns None silently."""
    from src.core.config import settings
    from src.services.agent.iteration_ledger import write_iteration

    monkeypatch.setattr(settings, "AGENT_LEDGER_DIR", None)

    path = write_iteration("thread-xyz", _state_with_one_turn())
    assert path is None
    # No directory created
    assert not (tmp_path / "thread-xyz").exists()


@pytest.mark.unit
def test_write_iteration_silent_on_bad_state(ledger_dir: Path):
    """Malformed state must not crash the agent — return None instead."""
    from src.services.agent.iteration_ledger import write_iteration

    path = write_iteration("thread-bad", {"messages": "not-a-list"})
    # Either succeeds with empty record or returns None — both safe.
    if path is not None:
        record = json.loads(path.read_text())
        assert "turn" in record


@pytest.mark.unit
def test_first_turn_writes_config_json(ledger_dir: Path):
    """Phase 9.C: first turn of a thread also writes config.json
    capturing the initial run setup. Subsequent turns leave it alone."""
    from src.services.agent.iteration_ledger import write_iteration

    state = _state_with_one_turn()
    state["thread_id"] = "thread-cfg"
    state["user_id"] = "user-1"
    write_iteration("thread-cfg", state)

    config_path = ledger_dir / "thread-cfg" / "config.json"
    assert config_path.exists()
    cfg = json.loads(config_path.read_text())
    assert cfg["thread_id"] == "thread-cfg"
    assert cfg["initial_query"] == "find papers on transformers"
    assert cfg["page_context"]["type"] == "chat"
    assert cfg["model"] == "gpt-5"
    first_started = cfg["started_at"]

    # Second turn must not rewrite config.json
    write_iteration("thread-cfg", state)
    cfg2 = json.loads(config_path.read_text())
    assert cfg2["started_at"] == first_started


@pytest.mark.unit
def test_every_turn_rewrites_final_json(ledger_dir: Path):
    """Phase 9.C: every turn rewrites final.json with the latest summary
    so dashboards/reports can read run state in one file."""
    from src.services.agent.iteration_ledger import write_iteration

    state = _state_with_one_turn()
    write_iteration("thread-final", state)
    final_path = ledger_dir / "thread-final" / "final.json"
    assert final_path.exists()

    final_v1 = json.loads(final_path.read_text())
    assert final_v1["latest_turn"] == 1
    assert final_v1["thread_id"] == "thread-final"
    assert final_v1["summary"]["intent"] == "research"
    assert final_v1["tool_executions_count"] == 1

    write_iteration("thread-final", state)
    final_v2 = json.loads(final_path.read_text())
    assert final_v2["latest_turn"] == 2


@pytest.mark.unit
def test_write_iteration_message_serialization_caps_length(ledger_dir: Path):
    """AI content capped at 4000 chars; human content at 2000 chars."""
    from src.services.agent.iteration_ledger import write_iteration

    huge = "x" * 10_000
    state = _state_with_one_turn()
    state["messages"] = [
        HumanMessage(content=huge),
        AIMessage(content=huge),
    ]
    path = write_iteration("thread-huge", state)
    assert path is not None
    record = json.loads(path.read_text())
    msgs = record["state_snapshot"]["messages"]
    user = next(m for m in msgs if m["role"] == "human")
    ai = next(m for m in msgs if m["role"] == "ai")
    assert len(user["content"]) <= 2000
    assert len(ai["content"]) <= 4000


@pytest.mark.unit
def test_ledger_redacts_pii_before_writing(ledger_dir: Path):
    """R7-L4: these are plain files on a shared volume that outlive the run."""
    from src.services.agent.iteration_ledger import write_iteration

    state = _state_with_one_turn()
    state["messages"] = [
        HumanMessage(content="email alice@example.com, ssn 123-45-6789"),
        AIMessage(content="I mailed alice@example.com"),
        ToolMessage(content='{"to":"alice@example.com"}', tool_call_id="call_1"),
    ]
    state["tool_executions"] = [
        {
            "id": "call_1",
            "tool_name": "send",
            "args": {"to": "alice@example.com"},
            "status": "completed",
        }
    ]

    path = write_iteration("thread-pii", state)
    assert path is not None
    raw = path.read_text()

    assert "alice@example.com" not in raw
    assert "123-45-6789" not in raw
    assert "<email>" in raw
    # The record is still useful: structure and non-PII fields survive.
    record = json.loads(raw)
    assert record["state_snapshot"]["tool_executions"][0]["tool_name"] == "send"


@pytest.mark.unit
def test_config_initial_query_is_redacted(ledger_dir: Path):
    from src.services.agent.iteration_ledger import write_iteration

    state = _state_with_one_turn()
    state["messages"] = [HumanMessage(content="I am at bob@example.com")]

    write_iteration("thread-cfg", state)

    config = json.loads((ledger_dir / "thread-cfg" / "config.json").read_text())
    assert "bob@example.com" not in config["initial_query"]
