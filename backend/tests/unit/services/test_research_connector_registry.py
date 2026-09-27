"""Contract tests for bounded research connector selection and projection."""

from typing import Any, cast

import pytest

from src.services.research_engine.connectors import registry


async def _rag_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
    del args, kwargs
    return {"results": []}


def test_registry_declares_canonical_connectors_and_current_features() -> None:
    """Registry metadata must describe what connector implementations do today."""
    capabilities = {item.connector_id: item for item in registry.CONNECTOR_CAPABILITIES}

    assert set(capabilities) == {
        "arxiv",
        "crossref",
        "openalex",
        "pubmed",
        "rag_store",
        "semantic_scholar",
    }
    assert capabilities["semantic_scholar"].aliases == ("web",)
    assert capabilities["rag_store"].full_text is True
    assert capabilities["rag_store"].daily_brief_eligible is False
    assert capabilities["openalex"].cursor is True
    assert all(item.date_filter is False for item in capabilities.values())
    assert all(
        item.full_text is False
        for name, item in capabilities.items()
        if name != "rag_store"
    )


def test_safe_projection_has_only_public_fields_and_hides_aliases() -> None:
    """Capability projection cannot expose aliases, secrets, URLs, or config."""
    projection = cast(list[dict[str, Any]], registry.safe_capability_projection())

    assert [item["id"] for item in projection] == sorted(
        item["id"] for item in projection
    )
    assert "web" not in {item["id"] for item in projection}
    for item in projection:
        assert set(item) == {
            "id",
            "label",
            "daily_brief_eligible",
            "available",
            "features",
        }
        assert set(item["features"]) == {"full_text", "date_filter", "cursor"}
        assert all(isinstance(value, bool) for value in item["features"].values())

    serialized = repr(projection).lower()
    for forbidden in (
        "aliases",
        "api_key",
        "mailto",
        "base_url",
        "http://",
        "https://",
    ):
        assert forbidden not in serialized


def test_legacy_alias_normalizes_only_outside_daily_brief() -> None:
    """The web alias remains readable for legacy execution but is not selectable."""
    assert registry.normalize_connector_selection(["web"], daily_brief_only=False) == (
        "semantic_scholar",
    )
    with pytest.raises(ValueError, match="alias"):
        registry.normalize_connector_selection(["web"], daily_brief_only=True)


@pytest.mark.parametrize(
    ("connector_ids", "message"),
    [
        ([], "between 1 and 4"),
        (["openalex"] * 2, "duplicate"),
        (["semantic_scholar", "web"], "duplicate"),
        (["rag_store"], "not eligible"),
        (["unknown"], "unknown"),
        (
            ["arxiv", "crossref", "openalex", "pubmed", "semantic_scholar"],
            "between 1 and 4",
        ),
    ],
)
def test_daily_brief_selection_rejects_unsafe_connector_sets(
    connector_ids: list[str], message: str
) -> None:
    """Invalid provider sets fail before connector construction or paid work."""
    with pytest.raises(ValueError, match=message):
        registry.normalize_connector_selection(connector_ids, daily_brief_only=True)


def test_build_connectors_constructs_only_requested_canonical_connectors() -> None:
    """Explicit selection cannot silently add the legacy web alias or extra sources."""
    connectors = registry.build_connectors(_rag_search, ["crossref", "openalex"])

    assert set(connectors) == {"crossref", "openalex"}


def test_build_connectors_preserves_legacy_default_registry() -> None:
    """Existing callers without a selection retain the internal web alias."""
    connectors = registry.build_connectors(_rag_search)

    assert connectors["web"] is connectors["semantic_scholar"]
    assert "rag_store" in connectors
