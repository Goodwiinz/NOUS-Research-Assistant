"""Source-selection and connector-filter contracts at existing tool seams."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.services.agent.tools_impl import _tool_search_external_database


class _Connector:
    def __init__(self, name: str, supported: set[str]) -> None:
        self.info = MagicMock(name=name)
        self.info.name = name
        self.is_available = MagicMock(return_value=True)
        self.supported_filter_keys = frozenset(supported)
        self.search = AsyncMock(return_value=[])

    def validate_search_filters(self, filters: dict[str, Any]) -> dict[str, Any]:
        unknown = set(filters) - self.supported_filter_keys
        if unknown:
            raise ValueError(f"unsupported filters: {', '.join(sorted(unknown))}")
        return dict(filters)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_forwards_declared_filters_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.connectors import connector_registry

    connector = _Connector("uniprot", {"organism"})
    monkeypatch.setattr(connector_registry, "get", lambda _name: connector)

    result = await _tool_search_external_database(
        {
            "query": "kinase",
            "connector": "uniprot",
            "filters": {"organism": "Homo sapiens"},
        }
    )

    assert result["total_results"] == 0
    connector.search.assert_awaited_once_with(
        "kinase", max_results=5, filters={"organism": "Homo sapiens"}
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("connector_name", "filters"),
    [
        ("uniprot", {"organism": "Homo sapiens"}),
        (
            "clinical_trials",
            {"status": "RECRUITING", "phase": "PHASE3", "condition": "Cancer"},
        ),
        (
            "sec_edgar",
            {"start_date": "2025-01-01", "end_date": "2025-12-31", "form_type": "10-K"},
        ),
        ("fred", {"order_by": "popularity", "frequency": "Annual"}),
        ("bioservices", {"service": "kegg"}),
    ],
)
@pytest.mark.asyncio
async def test_each_declared_adapter_mapping_reaches_search(
    monkeypatch: pytest.MonkeyPatch,
    connector_name: str,
    filters: dict[str, Any],
) -> None:
    from src.services.connectors import connector_registry
    from src.services.connectors.bioservices_bridge import BioServicesBridgeConnector
    from src.services.connectors.clinical_trials import ClinicalTrialsConnector
    from src.services.connectors.fred import FREDConnector
    from src.services.connectors.sec_edgar import SECEdgarConnector
    from src.services.connectors.uniprot import UniProtConnector

    adapter_types = {
        "uniprot": UniProtConnector,
        "clinical_trials": ClinicalTrialsConnector,
        "sec_edgar": SECEdgarConnector,
        "fred": FREDConnector,
        "bioservices": BioServicesBridgeConnector,
    }
    connector_factory: Any = adapter_types[connector_name]
    connector = cast(Any, connector_factory())
    monkeypatch.setattr(connector, "is_available", lambda: True)
    connector.search = AsyncMock(return_value=[])
    monkeypatch.setattr(connector_registry, "get", lambda _name: connector)

    result = await _tool_search_external_database(
        {"query": "term", "connector": connector_name, "filters": filters}
    )

    assert result["total_results"] == 0
    connector.search.assert_awaited_once_with("term", max_results=5, filters=filters)


@pytest.mark.unit
def test_adapter_filter_validators_reject_unsafe_or_invalid_values() -> None:
    from src.services.connectors.bioservices_bridge import BioServicesBridgeConnector
    from src.services.connectors.sec_edgar import SECEdgarConnector
    from src.services.connectors.uniprot import UniProtConnector

    with pytest.raises(ValueError, match="query syntax"):
        UniProtConnector().validate_search_filters({"organism": 'x" OR *:*'})
    with pytest.raises(ValueError, match="start_date must not be after end_date"):
        SECEdgarConnector().validate_search_filters(
            {"start_date": "2025-12-31", "end_date": "2025-01-01"}
        )
    with pytest.raises(ValueError, match="not a supported"):
        BioServicesBridgeConnector().validate_search_filters({"service": "unknown"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsupported_filter_fails_before_connector_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.connectors import connector_registry

    connector = _Connector("uniprot", {"organism"})
    monkeypatch.setattr(connector_registry, "get", lambda _name: connector)

    result = await _tool_search_external_database(
        {"query": "kinase", "connector": "uniprot", "filters": {"status": "active"}}
    )

    assert result["error_category"] == "unsupported_connector_filter"
    assert "status" in result["error"]
    connector.search.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_heterogeneous_fanout_prevalidates_every_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.connectors import connector_registry

    uniprot = _Connector("uniprot", {"organism"})
    pubmed = _Connector("pubmed", set())
    monkeypatch.setattr(connector_registry, "list_available", lambda: [uniprot, pubmed])

    result = await _tool_search_external_database(
        {"query": "kinase", "filters": {"organism": "Homo sapiens"}}
    )

    assert result["error_category"] == "unsupported_connector_filter"
    assert "pubmed" in result["error"]
    uniprot.search.assert_not_awaited()
    pubmed.search.assert_not_awaited()


@pytest.mark.unit
def test_explicit_empty_document_selection_query_does_not_widen() -> None:
    from src.services.research.draft_generation_service import DraftGenerationService

    query = DraftGenerationService._build_project_documents_query(uuid4(), [])
    compiled = str(query.compile(compile_kwargs={"literal_binds": True})).lower()

    assert "false" in compiled or "in (null)" in compiled


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_selection_is_rejected_before_generation_dispatch() -> None:
    from unittest.mock import patch

    from src.services.research.draft_generation_service import DraftGenerationService

    with patch.object(
        DraftGenerationService, "_init_openai_client", return_value=(None, "")
    ):
        service = DraftGenerationService(db=MagicMock())

    class _UnscheduledTask:
        def add_done_callback(self, _callback: Any) -> None:
            return None

        def cancel(self) -> None:
            return None

    def close_and_return_task(coro: Any) -> _UnscheduledTask:
        coro.close()
        return _UnscheduledTask()

    with patch.object(
        DraftGenerationService,
        "_fire_and_forget",
        side_effect=close_and_return_task,
    ) as fire:
        with pytest.raises(ValueError, match="document selection"):
            await service.generate_draft(
                project_id=uuid4(),
                user_id=uuid4(),
                themes=["topic"],
                document_ids=[],
            )

    fire.assert_not_called()
