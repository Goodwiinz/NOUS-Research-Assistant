"""Transport contract for owner-scoped research stage reviews."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user

ReviewClient = tuple[TestClient, SimpleNamespace, AsyncMock]


def _reviews_module() -> Any:
    return import_module("src.api.research_engine.reviews")


def _service_module() -> Any:
    return import_module("src.services.research_engine.review_service")


@pytest.fixture
def review_client(
    test_app: FastAPI, test_auth_headers: dict[str, str]
) -> Iterator[ReviewClient]:
    user = SimpleNamespace(
        id=uuid4(), organization_id=uuid4(), email="owner@example.test"
    )
    db = AsyncMock()
    test_app.dependency_overrides[get_current_user] = lambda: user
    test_app.dependency_overrides[get_db] = lambda: db

    @asynccontextmanager
    async def _no_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    original_lifespan = test_app.router.lifespan_context
    test_app.router.lifespan_context = _no_lifespan
    try:
        with TestClient(
            test_app, headers=test_auth_headers, raise_server_exceptions=False
        ) as client:
            yield client, user, db
    finally:
        test_app.router.lifespan_context = original_lifespan
        test_app.dependency_overrides.pop(get_current_user, None)
        test_app.dependency_overrides.pop(get_db, None)


def _descriptor(run_id: UUID, *, output_hash: str = "a" * 64) -> dict[str, object]:
    return {
        "run_id": run_id,
        "step_index": 1,
        "stage_type": "screen",
        "review_kind": "screening",
        "contract_version": 1,
        "output_hash": output_hash,
        "status": "pending",
        "created_at": datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc),
    }


def _review_response(
    run_id: UUID, review_id: UUID | None = None, *, replay: bool = False
) -> dict[str, Any]:
    return {
        "id": review_id or uuid4(),
        "run_id": run_id,
        "step_index": 1,
        "stage_type": "screen",
        "review_kind": "screening",
        "reviewer_id": uuid4(),
        "output_hash": "a" * 64,
        "decision": "approve",
        "decision_payload": {
            "items": [
                {
                    "source_id": "source-a",
                    "part_id": "p0001",
                    "decision": "include",
                }
            ]
        },
        "note": None,
        "created_at": datetime(2026, 9, 27, 12, 5, tzinfo=timezone.utc),
        "replay": replay,
    }


def test_pending_review_route_returns_bounded_owned_stage_output(
    review_client: ReviewClient,
) -> None:
    client, user, db = review_client
    run_id = uuid4()
    stage_output = {
        "contract_version": 1,
        "stage_type": "screen",
        "usage": {"model_calls": 1, "total_tokens": 5, "batches": []},
        "screening": [
            {
                "source_id": "source-a",
                "part_id": "p0001",
                "included": True,
                "reason": "relevant",
            }
        ],
        "included_source_ids": ["source-a"],
        "processing_coverage": {},
    }
    get_pending = AsyncMock(
        return_value={
            "pending": True,
            "descriptor": _descriptor(run_id),
            "stage_output": stage_output,
            "accepted_review": None,
            "validation": {
                "item_decisions": ["include", "exclude", "unresolved"],
                "reason_required_for": ["exclude"],
            },
        }
    )

    with patch.object(
        _reviews_module().ResearchReviewService,
        "get_pending_review",
        get_pending,
    ):
        response = client.get(f"/api/v1/research-engine/runs/{run_id}/reviews/pending")

    assert response.status_code == 200
    assert response.json()["stage_output"] == stage_output
    get_pending.assert_awaited_once_with(run_id=run_id, owner_id=user.id)
    assert get_pending.await_args is not None
    assert get_pending.await_args.kwargs["owner_id"] != user.organization_id
    assert db is not None


def test_submit_review_route_passes_authenticated_owner_and_audit_organization(
    review_client: ReviewClient,
) -> None:
    client, user, _db = review_client
    run_id = uuid4()
    accepted = _review_response(run_id)
    submit = AsyncMock(return_value=accepted)

    with patch.object(
        _reviews_module().ResearchReviewService,
        "submit_review",
        submit,
    ):
        response = client.post(
            f"/api/v1/research-engine/runs/{run_id}/reviews/1",
            json={
                "review_kind": "screening",
                "output_hash": "a" * 64,
                "decision": "approve",
                "decision_payload": {
                    "items": [
                        {
                            "source_id": "source-a",
                            "part_id": "p0001",
                            "decision": "include",
                        }
                    ]
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["id"] == str(accepted["id"])
    assert response.json()["replay"] is False
    submit.assert_awaited_once()
    awaited = submit.await_args
    assert awaited is not None
    kwargs = awaited.kwargs
    assert kwargs["run_id"] == run_id
    assert kwargs["step_index"] == 1
    assert kwargs["owner_id"] == user.id
    assert kwargs["reviewer_id"] == user.id
    assert kwargs["organization_id"] == user.organization_id
    assert kwargs["request"].review_kind.value == "screening"


@pytest.mark.parametrize("path_kind", ["pending", "submit"])
def test_absent_and_inaccessible_reviews_share_safe_404(
    review_client: ReviewClient, path_kind: str
) -> None:
    client, _user, _db = review_client
    run_id = uuid4()
    error = _service_module().ResearchReviewError(
        status_code=404,
        code="review_not_found",
        message="Run not found",
    )
    method = "get_pending_review" if path_kind == "pending" else "submit_review"

    with patch.object(
        _reviews_module().ResearchReviewService,
        method,
        AsyncMock(side_effect=error),
    ):
        if path_kind == "pending":
            response = client.get(
                f"/api/v1/research-engine/runs/{run_id}/reviews/pending"
            )
        else:
            response = client.post(
                f"/api/v1/research-engine/runs/{run_id}/reviews/1",
                json={
                    "review_kind": "screening",
                    "output_hash": "a" * 64,
                    "decision": "approve",
                    "decision_payload": {
                        "items": [
                            {
                                "source_id": "source-a",
                                "part_id": "p0001",
                                "decision": "include",
                            }
                        ]
                    },
                },
            )

    assert response.status_code == 404
    serialized = response.text
    assert "source-a" not in serialized
    assert "abstract" not in serialized
    assert "Run not found" in serialized


def test_stale_review_route_returns_current_content_free_descriptor(
    review_client: ReviewClient,
) -> None:
    client, _user, _db = review_client
    run_id = uuid4()
    descriptor = _descriptor(run_id, output_hash="b" * 64)
    error = _service_module().ResearchReviewError(
        status_code=409,
        code="review_output_stale",
        message="Review output is stale",
        descriptor=descriptor,
    )

    with patch.object(
        _reviews_module().ResearchReviewService,
        "submit_review",
        AsyncMock(side_effect=error),
    ):
        response = client.post(
            f"/api/v1/research-engine/runs/{run_id}/reviews/1",
            json={
                "review_kind": "screening",
                "output_hash": "a" * 64,
                "decision": "approve",
                "decision_payload": {
                    "items": [
                        {
                            "source_id": "source-a",
                            "part_id": "p0001",
                            "decision": "include",
                        }
                    ]
                },
            },
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "review_output_stale",
            "message": "Review output is stale",
            "descriptor": {
                **{
                    key: (str(value) if key == "run_id" else value)
                    for key, value in descriptor.items()
                    if key != "created_at"
                },
                "created_at": "2026-09-27T12:00:00Z",
            },
        }
    }
    assert "source-a" not in response.text


def test_review_request_rejects_client_supplied_content_before_service(
    review_client: ReviewClient,
) -> None:
    client, _user, _db = review_client
    run_id = uuid4()
    submit = AsyncMock()

    with patch.object(
        _reviews_module().ResearchReviewService,
        "submit_review",
        submit,
    ):
        response = client.post(
            f"/api/v1/research-engine/runs/{run_id}/reviews/1",
            json={
                "review_kind": "screening",
                "output_hash": "a" * 64,
                "decision": "approve",
                "decision_payload": {
                    "items": [
                        {
                            "source_id": "source-a",
                            "part_id": "p0001",
                            "decision": "include",
                            "citation": {"doi": "10.fake/injected"},
                        }
                    ]
                },
            },
        )

    assert response.status_code == 422
    submit.assert_not_awaited()


def test_review_validation_redacts_nested_input_from_response_and_logs(
    review_client: ReviewClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _user, _db = review_client
    run_id = uuid4()
    secret = "review-secret-7f6b5d4c"
    caplog.set_level(logging.WARNING, logger="src.main")

    response = client.post(
        f"/api/v1/research-engine/runs/{run_id}/reviews/1",
        json={
            "review_kind": "screening",
            "output_hash": "a" * 64,
            "decision": "approve",
            "note": secret + ("n" * 2001),
            "decision_payload": {
                "items": [
                    {
                        "source_id": "source-a",
                        "part_id": "p0001",
                        "decision": "include",
                        "citation": {"text": secret, "evidence_id": secret},
                        "reason": secret,
                        secret: "secret supplied as a field name",
                    }
                ]
            },
        },
    )

    assert response.status_code == 422
    assert secret not in response.text
    assert secret not in caplog.text
    details = response.json()["error"]["details"]
    assert details
    assert all(set(error) == {"loc", "msg", "type"} for error in details)
    assert all(error["loc"] and error["msg"] and error["type"] for error in details)
