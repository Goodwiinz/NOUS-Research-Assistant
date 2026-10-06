"""Shared fixtures for backend/tests/unit/.

Resets the module-level compiled-graph cache (introduced for first-token
latency wins) between every unit test so cross-test pollution from
``src.api.agent.streaming._COMPILED_GRAPH`` cannot bleed assertions
between unrelated test modules.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest


def _clear_shared_app_middleware() -> None:
    """Drop stateful middleware without importing or starting the application."""
    main = sys.modules.get("src.main")
    app = getattr(main, "app", None)
    if app is not None:
        app.middleware_stack = None


@pytest.fixture(autouse=True)
def _reset_app_middleware_stack_unit() -> Iterator[None]:
    """Rebuild real middleware per test so unrelated tests do not share quotas.

    Limits remain enforced across requests within a test. Resetting both
    boundaries also covers tests that import the shared app after setup.
    """
    _clear_shared_app_middleware()
    try:
        yield
    finally:
        _clear_shared_app_middleware()


@pytest.fixture(autouse=True)
def _reset_compiled_graph_cache_unit():
    """Clear cached compiled graph between every unit test."""
    import src.api.agent.streaming as mod

    mod._COMPILED_GRAPH = None
    yield
    mod._COMPILED_GRAPH = None
