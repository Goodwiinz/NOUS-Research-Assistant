"""Tests for GET /api/v1/documents/{document_id}/figures.

Mirrors table_extraction.py's org-scoped lookup + 404 semantics. Covers
tenancy (wrong-org -> 404), the happy path payload shape, caption-only rows
(no storage_key -> image_url null), and S3-unconfigured degradation.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_organization, get_current_user
from src.main import app

pytestmark = pytest.mark.unit


def _make_mock_user():
    user = Mock()
    user.id = uuid.uuid4()
    user.email = "researcher@example.com"
    return user


def _make_mock_organization():
    org = Mock()
    org.id = uuid.uuid4()
    org.name = "Test Org"
    return org


def _make_mock_document(organization_id):
    doc = Mock()
    doc.id = uuid.uuid4()
    doc.organization_id = organization_id
    doc.is_deleted = False
    return doc


def _make_figure_row(**overrides):
    row = Mock()
    row.content_id = overrides.get("content_id", "figure-p3-x12")
    row.processed_content = overrides.get("processed_content", "A caption")
    row.content_metadata = overrides.get(
        "content_metadata",
        {
            "page": 3,
            "bbox": [1.0, 2.0, 3.0, 4.0],
            "storage_key": "figures/org/doc/figure-p3-x12.png",
            "figure_label": "Figure 1",
        },
    )
    row.media_dimensions = overrides.get(
        "media_dimensions", {"width": 640, "height": 480}
    )
    return row


@pytest.fixture()
def mock_user():
    return _make_mock_user()


@pytest.fixture()
def mock_organization():
    return _make_mock_organization()


@pytest.fixture()
def client(test_app, test_auth_headers, mock_user, mock_organization):
    @asynccontextmanager
    async def _no_lifespan(_app):
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan

    test_app.dependency_overrides[get_current_user] = lambda: mock_user
    test_app.dependency_overrides[get_current_organization] = lambda: mock_organization

    try:
        with TestClient(test_app, headers=test_auth_headers) as c:
            yield c
    finally:
        test_app.dependency_overrides.clear()
        test_app.router.lifespan_context = original_lifespan


def _override_db(mock_db):
    async def _fake_db():
        yield mock_db

    app.dependency_overrides[get_db] = _fake_db


def _doc_result(document):
    result = Mock()
    result.scalar_one_or_none = Mock(return_value=document)
    return result


def _rows_result(rows):
    result = Mock()
    scalars = Mock()
    scalars.all = Mock(return_value=rows)
    result.scalars = Mock(return_value=scalars)
    return result


class TestListFigures:
    def test_wrong_org_document_404(self, client, mock_organization):
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=_doc_result(None))
        _override_db(mock_db)

        response = client.get(f"/api/v1/documents/{uuid.uuid4()}/figures")

        assert response.status_code == 404

    def test_happy_path_payload_shape_ordered(
        self, client, mock_organization, monkeypatch
    ):
        document = _make_mock_document(mock_organization.id)
        row1 = _make_figure_row(content_id="figure-p1-x1")
        row2 = _make_figure_row(content_id="figure-p2-x2")

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[_doc_result(document), _rows_result([row1, row2])]
        )
        _override_db(mock_db)

        stub_helper = Mock()
        stub_helper.create_signed_url = Mock(return_value="https://signed.example/x")
        monkeypatch.setattr(
            "src.core.s3_client.S3StorageHelper", Mock(return_value=stub_helper)
        )

        response = client.get(f"/api/v1/documents/{document.id}/figures")

        assert response.status_code == 200
        data = response.json()
        assert data["document_id"] == str(document.id)
        assert data["figure_count"] == 2
        assert [f["content_id"] for f in data["figures"]] == [
            "figure-p1-x1",
            "figure-p2-x2",
        ]
        fig = data["figures"][0]
        assert fig["page"] == 3
        assert fig["bbox"] == [1.0, 2.0, 3.0, 4.0]
        assert fig["figure_label"] == "Figure 1"
        assert fig["caption"] == "A caption"
        assert fig["width"] == 640
        assert fig["height"] == 480
        assert fig["image_url"] == "https://signed.example/x"

    def test_caption_only_row_no_storage_key_image_url_null(
        self, client, mock_organization, monkeypatch
    ):
        document = _make_mock_document(mock_organization.id)
        row = _make_figure_row(
            content_metadata={
                "page": 5,
                "bbox": None,
                "storage_key": None,
                "figure_label": "Figure 2",
            },
            media_dimensions=None,
        )

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[_doc_result(document), _rows_result([row])]
        )
        _override_db(mock_db)

        stub_helper = Mock()
        stub_helper.create_signed_url = Mock(return_value="https://signed.example/x")
        monkeypatch.setattr(
            "src.core.s3_client.S3StorageHelper", Mock(return_value=stub_helper)
        )

        response = client.get(f"/api/v1/documents/{document.id}/figures")

        assert response.status_code == 200
        data = response.json()
        assert data["figures"][0]["image_url"] is None
        stub_helper.create_signed_url.assert_not_called()

    def test_s3_unavailable_image_url_null_for_all(
        self, client, mock_organization, monkeypatch
    ):
        document = _make_mock_document(mock_organization.id)
        row = _make_figure_row()

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[_doc_result(document), _rows_result([row])]
        )
        _override_db(mock_db)

        monkeypatch.setattr(
            "src.core.s3_client.S3StorageHelper",
            Mock(side_effect=RuntimeError("S3 client not available")),
        )

        response = client.get(f"/api/v1/documents/{document.id}/figures")

        assert response.status_code == 200
        data = response.json()
        assert data["figures"][0]["image_url"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
