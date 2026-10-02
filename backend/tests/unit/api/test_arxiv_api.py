"""Unit tests for the public and protected ArXiv API surface."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.dependencies import get_current_user
from src.services.arxiv.arxiv_service import IngestionError


def test_public_arxiv_search_does_not_require_auth(test_client) -> None:
    """Searching arXiv should remain available on the public discovery page."""
    paper = {
        "id": "1706.03762",
        "title": "Attention Is All You Need",
        "authors": ["Ashish Vaswani"],
        "abstract": "Transformer architecture paper.",
        "published": "2017-06-12T00:00:00Z",
        "updated": "2017-06-12T00:00:00Z",
        "categories": ["cs.CL", "cs.LG"],
        "primary_category": "cs.CL",
        "comment": None,
        "journal_ref": None,
        "links": {
            "pdf": "https://arxiv.org/pdf/1706.03762",
            "doi": None,
        },
    }

    with patch("src.api.arxiv.core.ArXivIngestionService") as mock_service_cls:
        mock_service = AsyncMock()
        mock_service.search_papers = AsyncMock(return_value=[paper])
        mock_service_cls.return_value.__aenter__.return_value = mock_service
        mock_service_cls.return_value.__aexit__.return_value = None

        response = test_client.post(
            "/api/v1/arxiv/search",
            json={"query": "transformer interpretability", "max_results": 5},
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == "1706.03762"
    assert body[0]["title"] == "Attention Is All You Need"
    mock_service.search_papers.assert_awaited_once()


def test_tracking_surfaces_upstream_rate_limits_as_service_unavailable(
    test_app, test_client
) -> None:
    """Category scans should return a controlled error when arXiv rate limits us."""
    # get_current_user returns a User object with an organization; tracking now
    # resolves the org from it (and 403s if absent), so the override must carry
    # an organization_id — not a bare dict.
    test_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="user-1", organization_id=str(uuid.uuid4()), organization=None
    )

    try:
        # GOO-289: the route consumes a shared admission slot first.
        with (
            patch(
                "src.services.expensive_work_admission.admit_expensive_work",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "src.api.arxiv.arxiv_change_tracking.change_tracker.track_category_changes",
                new=AsyncMock(
                    side_effect=IngestionError("ArXiv rate limit reached after retries")
                ),
            ),
        ):
            response = test_client.post(
                "/api/v1/arxiv/tracking/track-categories",
                json={
                    "categories": ["cs.AI", "cs.LG"],
                    "days_back": 1,
                    "update_database": True,
                },
            )
    finally:
        test_app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 503
    assert (
        response.json()["error"]["message"]
        == "ArXiv is temporarily rate limiting category scans. Retry in about a minute or scan fewer categories."
    )
