"""Audit S2-M14 regression: ``hide_io`` was computed from LANGSMITH_HIDE_IO
but enforced with ``setdefault`` under *different* names, so a stale injected
``LANGCHAIN_HIDE_INPUTS=false`` outvoted the PII guard while the log line
claimed hide_io=True. When the guard decides to hide, it must force-set all
four SDK names (the same force-not-setdefault discipline the project name
uses two lines above); LANGSMITH_HIDE_IO stays the single decision knob.
"""

import os

import pytest

from src.services.agent.observability import configure_langsmith

_LANGSMITH_ENV_VARS = (
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGCHAIN_PROJECT",
    "LANGSMITH_PROJECT_OVERRIDE",
    "LANGCHAIN_TRACING_V2",
    "LANGSMITH_TRACING",
    "LANGSMITH_HIDE_IO",
    "LANGCHAIN_HIDE_INPUTS",
    "LANGCHAIN_HIDE_OUTPUTS",
    "LANGSMITH_HIDE_INPUTS",
    "LANGSMITH_HIDE_OUTPUTS",
    "DEPLOY_ENV",
    "ENVIRONMENT",
)


def _configure_env(monkeypatch: pytest.MonkeyPatch, *, deploy_env: str) -> None:
    # delenv tracks every key monkeypatch will restore, so the mutations
    # configure_langsmith() makes directly on os.environ are undone on teardown.
    for name in _LANGSMITH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Bypass the ordinary-pytest early return (same gate the perf harness uses).
    monkeypatch.setenv("RUN_PERF_HARNESS", "1")
    monkeypatch.setenv("DEPLOY_ENV", deploy_env)
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")


def test_injected_false_cannot_defeat_pii_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_env(monkeypatch, deploy_env="production")
    monkeypatch.setenv("LANGCHAIN_HIDE_INPUTS", "false")  # the stale injection

    configure_langsmith()

    assert os.environ["LANGCHAIN_HIDE_INPUTS"] == "true"
    assert os.environ["LANGCHAIN_HIDE_OUTPUTS"] == "true"
    assert os.environ["LANGSMITH_HIDE_INPUTS"] == "true"
    assert os.environ["LANGSMITH_HIDE_OUTPUTS"] == "true"


def test_explicit_langsmith_hide_io_false_opts_back_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_env(monkeypatch, deploy_env="production")
    monkeypatch.setenv("LANGSMITH_HIDE_IO", "false")

    configure_langsmith()

    # Opt-out means no hiding is enforced; the names stay untouched.
    assert "LANGCHAIN_HIDE_INPUTS" not in os.environ
    assert "LANGSMITH_HIDE_INPUTS" not in os.environ


def test_dev_hides_io_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """R7-M6/M8: ``dev`` is a live deployment serving real users, not a laptop."""
    _configure_env(monkeypatch, deploy_env="development")

    configure_langsmith()

    assert os.environ["LANGCHAIN_HIDE_INPUTS"] == "true"
    assert os.environ["LANGSMITH_HIDE_OUTPUTS"] == "true"


def test_local_keeps_io_visible_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch, deploy_env="local")

    configure_langsmith()

    assert "LANGCHAIN_HIDE_INPUTS" not in os.environ
    assert "LANGSMITH_HIDE_INPUTS" not in os.environ


def test_unset_environment_hides_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: a deployment that forgot DEPLOY_ENV must not leak I/O."""
    _configure_env(monkeypatch, deploy_env="")

    configure_langsmith()

    assert os.environ["LANGCHAIN_HIDE_INPUTS"] == "true"


def test_non_dev_hides_io_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch, deploy_env="staging")

    configure_langsmith()

    assert os.environ["LANGCHAIN_HIDE_INPUTS"] == "true"
    assert os.environ["LANGSMITH_HIDE_OUTPUTS"] == "true"


@pytest.fixture
def _isolated_langsmith_client(monkeypatch: pytest.MonkeyPatch):
    """Give the test its own process-global LangSmith client slot and env cache."""
    from langsmith import Client, run_trees, schemas
    from langsmith import utils as ls_utils

    monkeypatch.setattr(run_trees, "_CLIENT", None)
    # The batch-tracing thread probes GET /info on construction; stub it so
    # nothing here reaches the network.
    monkeypatch.setattr(Client, "info", property(lambda _self: schemas.LangSmithInfo()))
    ls_utils.get_env_var.cache_clear()
    yield run_trees
    ls_utils.get_env_var.cache_clear()


def test_client_built_before_configure_still_hides_io(
    monkeypatch: pytest.MonkeyPatch, _isolated_langsmith_client
) -> None:
    """R8-C8: langsmith caches ``get_env_var`` (lru_cache) and reads the hide
    flags once in ``Client.__init__``. In a Celery worker another traced task
    can build the shared client before ``configure_langsmith`` writes the env,
    after which every agent turn uploads prompts and tool I/O.
    """
    from langchain_core.tracers.langchain import get_client

    run_trees = _isolated_langsmith_client
    _configure_env(monkeypatch, deploy_env="production")
    early = run_trees.get_cached_client()
    assert early._hide_inputs is False  # precondition: built before the guard

    configure_langsmith()

    client = get_client()
    assert client._hide_inputs is True
    assert client._hide_outputs is True


def test_configure_keeps_an_already_hiding_client(
    monkeypatch: pytest.MonkeyPatch, _isolated_langsmith_client
) -> None:
    """configure_langsmith runs on every agent turn; it must not churn clients."""
    run_trees = _isolated_langsmith_client
    _configure_env(monkeypatch, deploy_env="production")

    configure_langsmith()
    first = run_trees.get_cached_client()
    configure_langsmith()

    assert run_trees.get_cached_client() is first
    assert first._hide_inputs is True


def test_stale_env_cache_cannot_defeat_a_client_built_later(
    monkeypatch: pytest.MonkeyPatch, _isolated_langsmith_client
) -> None:
    """No client yet, but the hide flag was already read (and cached) unset."""
    from langchain_core.tracers.langchain import get_client
    from langsmith import utils as ls_utils

    _configure_env(monkeypatch, deploy_env="production")
    assert ls_utils.get_env_var("HIDE_INPUTS") is None  # primes the lru_cache

    configure_langsmith()

    client = get_client()
    assert client._hide_inputs is True
    assert client._hide_outputs is True
