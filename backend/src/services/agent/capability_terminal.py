"""Deterministic, non-executable terminal output for unavailable capabilities."""

from __future__ import annotations

from typing import Any, Mapping

from langchain_core.messages import AIMessage

from src.services.agent.execution_evidence import result_evidence_state
from src.services.agent.tool_registry import ToolEffectMode
from src.services.agent.tools import TOOL_REGISTRY

_BRANCH_LABELS = {
    "main": "main",
    "research": "research",
    "writing": "writing",
    "data": "data",
}


def make_capability_terminal_message(state: Mapping[str, Any]) -> AIMessage:
    """Render only server-owned labels and successful execution evidence."""
    limitation = state.get("capability_limitation")
    limitation = limitation if isinstance(limitation, Mapping) else {}
    branch_key = limitation.get("branch")
    branch_name = branch_key if isinstance(branch_key, str) else ""
    branch = _BRANCH_LABELS.get(branch_name, "selected")
    unavailable = limitation.get("unavailable_tools")
    unavailable_names = (
        sorted(
            {
                name
                for name in unavailable
                if isinstance(name, str) and TOOL_REGISTRY.descriptor(name) is not None
            }
        )[:8]
        if isinstance(unavailable, list)
        else []
    )

    if branch_key == "runtime" or limitation.get("kind") == "runtime":
        text = (
            "The saved tool runtime for this confirmation is unavailable, "
            "so I cannot continue this request."
        )
    elif limitation.get("kind") == "plan":
        text = f"The {branch} workflow cannot represent this combination of operations."
        if unavailable_names:
            text += " Unavailable operations: " + ", ".join(unavailable_names) + "."
    elif unavailable_names:
        text = (
            f"The {branch} workflow cannot perform these unavailable operations: "
            + ", ".join(unavailable_names)
            + "."
        )
    else:
        text = f"The {branch} workflow cannot complete this operation."

    if limitation.get("kind") == "execution":
        text += " The rejected batch did not run."

    completed: list[str] = []
    executions = state.get("tool_executions")
    if isinstance(executions, list):
        for execution in executions:
            if (
                not isinstance(execution, Mapping)
                or execution.get("status") != "success"
            ):
                continue
            name = execution.get("tool_name")
            if not isinstance(name, str) or name in completed:
                continue
            descriptor = TOOL_REGISTRY.descriptor(name)
            if descriptor is None:
                continue
            evidence = result_evidence_state(
                execution.get("result"),
                structured_status_required=(
                    descriptor.effect_mode == ToolEffectMode.EXTERNAL
                ),
            )
            if evidence == "completed":
                completed.append(name)
            if len(completed) == 5:
                break
    if completed:
        text += " Already completed operations: " + ", ".join(completed) + "."

    return AIMessage(content=text)
