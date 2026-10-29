import pytest

pytestmark = pytest.mark.unit


def test_chat_context_lives_in_services_and_is_reexported() -> None:
    from src.api.agent import harness_streaming
    from src.services.harness import chat_context

    assert harness_streaming.create_chat_context is chat_context.create_chat_context
    assert harness_streaming._run_consent_kind is chat_context._run_consent_kind
