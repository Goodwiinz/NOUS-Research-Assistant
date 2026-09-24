"""Shared Bedrock KB retrieval must not return cross-tenant chunks."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from src.services.bedrock_retrieval import retrieve_chunks


def test_bedrock_retrieve_filters_and_verifies_metadata() -> None:
    org_id = uuid4()
    document_id = uuid4()
    calls: list[dict[str, Any]] = []

    def retrieve(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "retrievalResults": [
                {
                    "content": {"text": "authorized"},
                    "score": 0.9,
                    "metadata": {
                        "organization_id": str(org_id),
                        "document_id": str(document_id),
                    },
                },
                {
                    "content": {"text": "foreign"},
                    "metadata": {
                        "organization_id": str(uuid4()),
                        "document_id": str(uuid4()),
                    },
                },
                {
                    "content": {"text": "unattributed"},
                    "metadata": {"organization_id": str(org_id)},
                },
            ]
        }

    document_filter = {"equals": {"key": "document_id", "value": str(document_id)}}
    with patch("boto3.client", return_value=SimpleNamespace(retrieve=retrieve)):
        chunks = retrieve_chunks("GGYNMOGZAH", "research", org_id, 8, document_filter)

    assert [chunk["text"] for chunk in chunks] == ["authorized"]
    assert calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"][
        "filter"
    ] == {
        "andAll": [
            {"equals": {"key": "organization_id", "value": str(org_id)}},
            document_filter,
        ]
    }
    try:
        retrieve_chunks("GGYNMOGZAH", "research", None, 8)
    except ValueError:
        pass
    else:
        raise AssertionError("a shared-KB query without an organization must fail")
