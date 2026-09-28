# mypy: ignore-errors
# These standalone probes intentionally use dynamic mocks and ad-hoc payloads;
# production modules remain covered by the normal type-checking gate.

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.services.agent.subgraphs.data_agent import data_llm_node
from src.services.agent.subgraphs.research_agent import (
    _direct_arxiv_search_message,
    research_llm_node,
)
from src.services.agent.subgraphs.writing_agent import writing_llm_node
from src.services.agent.tools import TOOL_REGISTRY
from src.services.agent.tools_impl import _tool_search_external_database


async def main():
    uid = str(uuid4())
    pid = str(uuid4())
    did = str(uuid4())
    state = {
        "messages": [HumanMessage(content="Find papers related to this project")],
        "page_context": {
            "type": "project",
            "project_id": pid,
            "project_name": "UNIQUE_PROJECT_NAME",
            "paper_id": did,
            "paper_title": "UNIQUE_PAPER_TITLE",
            "metadata": {"description": "UNIQUE_PROJECT_DESCRIPTION"},
        },
        "retrieved_contexts": [],
        "tool_executions": [],
        "user_memories": [{"content": "UNIQUE_USER_MEMORY"}],
        "plan": [],
        "tool_loop_count": 0,
        "user_id": uid,
        "model": "UNIQUE_MODEL_DEPLOYMENT",
    }
    for name, node in [
        ("research", research_llm_node),
        ("writing", writing_llm_node),
        ("data", data_llm_node),
    ]:
        captured = []

        async def invoke(msgs, config=None):
            captured.extend(msgs)
            return AIMessage(content="mock response")

        llm = MagicMock()
        llm.bind_tools.return_value.ainvoke = invoke
        with (
            patch("src.services.agent.graph._build_llm", return_value=llm),
            patch(
                "src.services.agent.llm_factory.build_synthesis_llm", return_value=llm
            ),
        ):
            await node(state, {"configurable": {"user_id": uid}})
        content = "\n".join(
            str(m.content) for m in captured if isinstance(m, SystemMessage)
        )
        print(
            name,
            json.dumps(
                {
                    "project_name_present": "UNIQUE_PROJECT_NAME" in content,
                    "project_id_present": pid in content,
                    "document_id_present": did in content,
                    "memory_present": "UNIQUE_USER_MEMORY" in content,
                    "runtime_model_present": "UNIQUE_MODEL_DEPLOYMENT" in content,
                }
            ),
        )
    for query in [
        "Search arxiv for 20 papers on quantum computing",
        "Using skill systematic-review, search arxiv for transformers",
        "Do not search arxiv; list my project papers instead",
    ]:
        print(
            "DIRECT_SEARCH",
            query,
            _direct_arxiv_search_message([HumanMessage(content=query)]).tool_calls,
        )
    connector = SimpleNamespace(
        info=SimpleNamespace(name="uniprot"),
        is_available=lambda: True,
        search=AsyncMock(return_value=[]),
    )
    with patch(
        "src.services.connectors.connector_registry.get", return_value=connector
    ):
        result = await _tool_search_external_database(
            {
                "query": "kinase",
                "connector": "uniprot",
                "max_results": 3,
                "filters": {"organism": "human"},
            }
        )
    print("EXTERNAL_FILTER_CALL", connector.search.call_args, "RESULT", result)
    schema = TOOL_REGISTRY.descriptor("add_document_to_project").tool.args_schema
    try:
        schema.model_validate({"document_ids": [did], "project_id": pid})
    except Exception as exc:
        print(
            "PROMPT_RECOMMENDED_ARGS_REJECTED",
            type(exc).__name__,
            str(exc).splitlines()[1],
        )


asyncio.run(main())
