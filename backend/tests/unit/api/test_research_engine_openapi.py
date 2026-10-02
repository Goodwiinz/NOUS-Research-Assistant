"""Public contract coverage for the Daily Research Brief API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

OPENAPI_PATH = Path(__file__).resolve().parents[3] / "openapi.json"


def _spec() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(OPENAPI_PATH.read_text(encoding="utf-8")))


def _json_response(operation: dict[str, Any]) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        operation["responses"]["200"]["content"]["application/json"]["schema"],
    )


def test_daily_brief_routes_publish_their_generated_request_and_response_models() -> (
    None
):
    spec = _spec()
    paths = spec["paths"]

    assert (
        _json_response(paths["/api/v1/research-engine/capabilities"]["get"])["items"][
            "$ref"
        ]
        == "#/components/schemas/ConnectorCapabilityResponse"
    )
    assert (
        _json_response(
            paths["/api/v1/research-engine/blueprints/templates/{slug}"]["get"]
        )["$ref"]
        == "#/components/schemas/BlueprintTemplateDetailResponse"
    )
    assert (
        _json_response(
            paths["/api/v1/research-engine/runs/{run_id}/reviews/pending"]["get"]
        )["$ref"]
        == "#/components/schemas/PendingReviewResponse"
    )

    submit = paths["/api/v1/research-engine/runs/{run_id}/reviews/{step_index}"]["post"]
    assert (
        submit["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/StageReviewRequest"
    )
    assert _json_response(submit)["$ref"] == (
        "#/components/schemas/StageReviewResponse"
    )

    resume = paths["/api/v1/research-engine/runs/{run_id}/resume"]["post"]
    assert {
        branch.get("$ref")
        for branch in resume["requestBody"]["content"]["application/json"]["schema"][
            "anyOf"
        ]
    } == {"#/components/schemas/RunResumeRequest", None}
    assert _json_response(resume)["$ref"] == "#/components/schemas/RunResponse"

    export = paths["/api/v1/research-engine/runs/{run_id}/export"]["get"]
    export_format = next(
        parameter for parameter in export["parameters"] if parameter["name"] == "format"
    )
    assert export_format["schema"]["$ref"] == (
        "#/components/schemas/src__schemas__research_engine__ExportFormat"
    )


def test_daily_brief_schemas_publish_bounds_pause_state_and_exact_vocabularies() -> (
    None
):
    schemas = _spec()["components"]["schemas"]

    assert schemas["StepType"]["enum"] == [
        "search",
        "screen",
        "extract",
        "synthesize",
        "verify",
        "export",
        "analyze",  # GOO-312
    ]
    assert schemas["src__schemas__research_engine__ExportFormat"]["enum"] == [
        "markdown",
        "json",
        "csv",
    ]
    assert schemas["ReviewKind"]["enum"] == ["screening", "extraction", "final"]
    assert schemas["ReviewDecision"]["enum"] == ["approve", "decline"]

    run_fields = schemas["RunResponse"]["properties"]
    assert run_fields["pause_reason"]["anyOf"][0]["enum"] == [
        "user_paused",
        "review_required",
        "verification_failed",
    ]
    assert run_fields["review_kind"]["anyOf"][0]["$ref"] == (
        "#/components/schemas/ReviewKind"
    )
    assert run_fields["step_index"]["anyOf"][0]["type"] == "integer"
    assert run_fields["output_hash"]["anyOf"][0]["pattern"] == "^[0-9a-f]{64}$"

    scope = schemas["DailyBriefScopeConfirmation"]["properties"]
    assert scope["providers"]["minItems"] == 1
    assert scope["providers"]["maxItems"] == 4
    assert scope["limit_per_provider"]["minimum"] == 1
    assert scope["limit_per_provider"]["maximum"] == 50
    assert scope["confirmed"]["const"] is True
    assert (
        schemas["RunCreate"]["properties"]["scope_confirmation"]["anyOf"][0]["$ref"]
        == "#/components/schemas/DailyBriefScopeConfirmation"
    )

    capability_fields = schemas["ConnectorCapabilityResponse"]["properties"]
    assert set(capability_fields) == {
        "id",
        "label",
        "daily_brief_eligible",
        "available",
        "features",
    }
    assert schemas["BlueprintTemplateDetailResponse"]["required"] == [
        "slug",
        "name",
        "template_source",
        "contract_version",
        "parameters",
        "constraints",
        "coverage",
        "steps",
    ]
