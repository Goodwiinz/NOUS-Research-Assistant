"""The retrieval helper requires an explicit caller tenant scope.

The deprecated threads SSE endpoint and its StreamService were removed; the
remaining test pins the helper contract used by the active research chat path.
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


def test_retrieve_context_scope_params_are_keyword_only():
    from src.api.research.chat import retrieve_context

    sig = inspect.signature(retrieve_context)
    for name in ("organization_id", "user_id"):
        assert (
            sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        ), f"{name} must stay keyword-only so callers cannot mis-bind the scope"
