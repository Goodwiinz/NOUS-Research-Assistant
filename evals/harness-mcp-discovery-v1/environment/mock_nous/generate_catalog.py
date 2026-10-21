#!/usr/bin/env python3
"""Regenerate the mock's tool catalog and connector inventory from backend source.

The mock NOUS backend must advertise exactly what the real gateway advertises
to a project-scoped ``tools:read`` grant, so these files are generated here
from the backend's own functions and constants, never written by hand:

- ``catalog.json``: ``list_read_tools(["tools:read"], workspace_bound=False)``
  from ``backend/src/services/integrations/read_tools.py``, serialized the way
  ``GET /api/v1/integrations/tools`` emits it (``ToolDescriptorDTO`` dumps).
- ``connectors.json``: the ``list_external_databases`` payload that
  ``_tool_list_external_databases({})`` builds from the real connector
  registry, with every key-gated connector's API-key variable unset (the
  benchmark provisions no upstream credentials).
- ``arxiv_query.json``: ``tools_impl._ARXIV_STOPWORDS`` and
  ``_ARXIV_VENUE_STOPWORDS`` plus ``arxiv_service._ARXIV_QUERY_SYNTAX``, which
  the mock uses to echo ``search_arxiv``'s rewritten query.

After writing (or, with ``--check``, after comparing) the files, the mock's
ported arXiv query rewrite is compared with the backend helpers on fixed
sample queries.

Output is deterministic: the backend's own list and key order is preserved
for the model-visible files, stopword sets are sorted, with fixed indentation
and a trailing newline.

Usage (repository root, backend venv):

    PYTHONPATH=backend backend/.venv/bin/python \
        evals/harness-mcp-discovery-v1/environment/mock_nous/generate_catalog.py
    ... generate_catalog.py --check

Exit codes: 0 match, 1 drift (a file or the arXiv rewrite differs from
backend source), 2 the generator itself failed (import, settings, count).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
BACKEND = REPO_ROOT / "backend"
CATALOG_PATH = HERE / "catalog.json"
CONNECTORS_PATH = HERE / "connectors.json"
ARXIV_QUERY_PATH = HERE / "arxiv_query.json"
# A project grant holding only tools:read (no library:read, no workspace
# selector) is what the eval's bridge credential represents.
GRANT_SCOPES = ["tools:read"]
EXPECTED_TOOL_COUNT = 13
# Throwaway settings that make backend Settings importable offline. Assigned
# unconditionally: an ambient ENVIRONMENT or DATABASE_URL in the caller's
# shell would otherwise fail Settings validation.
IMPORT_ENVIRONMENT = {
    "ENVIRONMENT": "testing",
    "DATABASE_URL": "sqlite:///:memory:",
    "AZURE_OPENAI_CHAT_ENDPOINT": "https://example.invalid",
    "AZURE_OPENAI_CHAT_API_KEY": "catalog-generation-only",
    "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME": "catalog-generation",
    "AZURE_OPENAI_CHAT_API_VERSION": "2024-02-01",
}
ARXIV_PARITY_QUERIES = (
    "recent retrieval-augmented generation for clinical QA",
    '"retrieval augmented generation" clinical',
    "retrieval augmented generation clinical question answering",
    "RAG rag clinical RAG",
    "ti:retrieval AND cat:cs.CL",
    "arxiv preprint NeurIPS retrieval medical",
    "recent papers",
    "(clinical OR medical) retrieval",
)


class GeneratorError(RuntimeError):
    """The generator cannot produce a trustworthy result (exit 2)."""


def _prepare_import_environment() -> None:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    os.environ.update(IMPORT_ENVIRONMENT)


def _render(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def build_catalog() -> list[dict[str, Any]]:
    from src.services.integrations.read_tools import list_read_tools

    tools = list_read_tools(GRANT_SCOPES, workspace_bound=False)
    catalog = [tool.model_dump(mode="json") for tool in tools]
    if len(catalog) != EXPECTED_TOOL_COUNT:
        raise GeneratorError(
            f"expected {EXPECTED_TOOL_COUNT} tools for a project tools:read grant, "
            f"backend lists {len(catalog)}: {[tool['name'] for tool in catalog]}"
        )
    return catalog


def build_connector_inventory() -> dict[str, Any]:
    from src.services.agent.tools_impl import _tool_list_external_databases
    from src.services.connectors import connector_registry

    for connector in connector_registry.list_all():
        if connector.info.requires_api_key and connector.info.api_key_env_var:
            os.environ.pop(connector.info.api_key_env_var, None)
    payload: dict[str, Any] = asyncio.run(_tool_list_external_databases({}))
    if "error" in payload:
        raise GeneratorError(f"connector inventory failed: {payload['error']}")
    return payload


def build_arxiv_query_constants() -> dict[str, Any]:
    from src.services.agent.tools_impl import _ARXIV_STOPWORDS, _ARXIV_VENUE_STOPWORDS
    from src.services.arxiv.arxiv_service import _ARXIV_QUERY_SYNTAX

    return {
        "stopwords": sorted(_ARXIV_STOPWORDS),
        "venue_stopwords": sorted(_ARXIV_VENUE_STOPWORDS),
        "query_syntax_pattern": _ARXIV_QUERY_SYNTAX.pattern,
    }


def _backend_arxiv_query(query: str) -> str:
    """The query composition at the top of tools_impl._tool_search_arxiv
    (recency window excluded: it depends on the clock)."""
    from src.services.agent.tools_impl import (
        _ARXIV_STOPWORDS,
        _field_arxiv_query,
        _sanitize_arxiv_query,
    )

    query = _sanitize_arxiv_query(query)
    if any(
        re.sub(r"[^a-z]", "", token.lower()) not in _ARXIV_STOPWORDS
        for token in query.split()
    ):
        query = _field_arxiv_query(query)
    return query


def arxiv_rewrite_mismatches() -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "mock_nous_server", HERE / "server.py"
    )
    if spec is None or spec.loader is None:
        raise GeneratorError("cannot load mock server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    mismatches = []
    for query in ARXIV_PARITY_QUERIES:
        mock, real = server.rewritten_arxiv_query(query, 0), _backend_arxiv_query(query)
        if mock != real:
            mismatches.append(f"{query!r}: mock {mock!r} != backend {real!r}")
    return mismatches


def run(check: bool) -> int:
    _prepare_import_environment()
    rendered = {
        CATALOG_PATH: _render(build_catalog()),
        CONNECTORS_PATH: _render(build_connector_inventory()),
        ARXIV_QUERY_PATH: _render(build_arxiv_query_constants()),
    }
    if check:
        drifted = [
            path.name
            for path, text in rendered.items()
            if not path.exists() or path.read_text(encoding="utf-8") != text
        ]
        if drifted:
            print(
                "generated mock files drifted from backend source: "
                + ", ".join(drifted)
                + " (rerun generate_catalog.py without --check)",
                file=sys.stderr,
            )
            return 1
    else:
        for path, text in rendered.items():
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(REPO_ROOT)}")
    mismatches = arxiv_rewrite_mismatches()
    if mismatches:
        print(
            "mock search_arxiv query rewrite drifted from backend helpers "
            "(update server.py):\n  " + "\n  ".join(mismatches),
            file=sys.stderr,
        )
        return 1
    print(
        "catalog.json, connectors.json and arxiv_query.json match backend source; "
        "arXiv query rewrite matches"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare against the committed files instead of writing them",
    )
    args = parser.parse_args()
    try:
        return run(args.check)
    except BaseException as error:  # noqa: BLE001 - exit 1 must mean drift only
        if isinstance(error, KeyboardInterrupt):
            raise
        detail = (
            str(error) if isinstance(error, GeneratorError) else traceback.format_exc()
        )
        print(
            f"generate_catalog.py failed (not a drift verdict): {detail}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
