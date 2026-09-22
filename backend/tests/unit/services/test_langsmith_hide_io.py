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
