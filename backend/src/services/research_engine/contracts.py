"""Typed, versioned contracts for evidence-bearing research stages."""

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictStr

CONTRACT_VERSION = 1
MAX_RESPONSE_BYTES = 32 * 1024
STAGE_NAMES = {"screen", "extract", "synthesize", "verify", "export", "search"}
EVIDENCE_LEVELS = frozenset(
    {"full_text", "abstract", "metadata_only", "workspace_document"}
)
_LEGACY_EVIDENCE_LEVELS = {
    "metadata": "metadata_only",
    "excerpt": "workspace_document",
}


def canonical_json_bytes(value: object) -> bytes:
    """Serialize a JSON value using the workflow's deterministic byte contract."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    """Return the SHA-256 digest of the canonical JSON representation."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_stage_output_hash(output: dict[str, object]) -> str:
    """Hash a persisted stage output using the canonical JSON contract."""
    return canonical_json_sha256(output)


def normalize_evidence_level(value: str | None) -> str:
    """Normalize legacy evidence labels into the canonical vocabulary."""
    if not isinstance(value, str):
        return "metadata_only"
    if value in EVIDENCE_LEVELS:
        return value
    if value in _LEGACY_EVIDENCE_LEVELS:
        return _LEGACY_EVIDENCE_LEVELS[value]
    return "metadata_only"


class StrictContract(BaseModel):
    """Base model that rejects coercion and undeclared response fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ScreeningDecision(StrictContract):
    source_id: StrictStr
    part_id: StrictStr
    included: StrictBool
    reason: StrictStr = Field(max_length=1000)


class EvidenceItem(StrictContract):
    pointer: StrictStr
    quote: StrictStr = Field(min_length=1, max_length=4000)
    page_reference: StrictStr | None


class ExtractionRecord(StrictContract):
    source_id: StrictStr
    part_id: StrictStr
    data: Any
    evidence: list[EvidenceItem]


class ExtractedClaim(StrictContract):
    claim_text: StrictStr = Field(min_length=1, max_length=1000)
    quote: StrictStr = Field(min_length=1, max_length=4000)
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    page_reference: StrictStr | None


class ClaimRecord(StrictContract):
    source_id: StrictStr
    part_id: StrictStr
    claims: list[ExtractedClaim]


class EvidenceReference(StrictContract):
    evidence_id: StrictStr
    relation: Literal["supports", "contradicts"]


class SynthesizedClaim(StrictContract):
    claim_text: StrictStr = Field(min_length=1, max_length=1000)
    evidence: list[EvidenceReference] = Field(max_length=32)


class SynthesisSection(StrictContract):
    heading: StrictStr = Field(min_length=1, max_length=120)
    claims: list[SynthesizedClaim]


class VerificationCheck(StrictContract):
    claim_id: StrictStr
    status: Literal["supported", "unverified", "contradicted"]
    reason: StrictStr = Field(max_length=1000)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_provider_json(raw: str) -> dict[str, Any]:
    """Parse one bounded JSON object, rejecting ambiguity and trailing text."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("provider JSON response exceeds the 32 KiB limit")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeEncodeError) as exc:
        raise ValueError("provider returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("provider JSON response must be an object")
    _validate_finite_numbers(value)
    return value


def _validate_finite_numbers(value: Any) -> None:
    """Reject exponent-overflow floats wherever they occur in decoded JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number is forbidden")
    if isinstance(value, dict):
        for child in value.values():
            _validate_finite_numbers(child)
    elif isinstance(value, list):
        for child in value:
            _validate_finite_numbers(child)


def validate_envelope(value: dict[str, Any], stage_type: str) -> dict[str, Any]:
    """Validate common envelope metadata and its stage-owned top-level keys."""
    if value.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("unsupported research contract version")
    if value.get("stage_type") != stage_type:
        raise ValueError("research stage envelope type does not match step")
    usage = value.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("research stage envelope is missing usage metadata")
    if type(usage.get("model_calls")) is not int or usage["model_calls"] < 0:
        raise ValueError("usage.model_calls must be a non-negative integer")
    if type(usage.get("total_tokens")) is not int or usage["total_tokens"] < 0:
        raise ValueError("usage.total_tokens must be a non-negative integer")
    if not isinstance(usage.get("batches"), list):
        raise ValueError("usage.batches must be a list")
    allowed = {
        "screen": {"screening", "included_source_ids", "processing_coverage"},
        "extract": {"extractions", "processing_coverage", "diagnostics"},
        "synthesize": {"synthesis", "processing_coverage", "diagnostics"},
        "verify": {"verification", "processing_coverage"},
        "search": {
            "source_records",
            "coverage",
            "selected_sources",
            "sources",
            "query",
        },
        "export": {"export", "format", "exported", "markdown", "media_type"},
    }.get(stage_type)
    if allowed is None:
        raise ValueError("unknown research stage type")
    required = {
        "screen": {"screening", "included_source_ids", "processing_coverage"},
        "extract": {"extractions", "processing_coverage"},
        "synthesize": {"synthesis", "processing_coverage"},
        "verify": {"verification", "processing_coverage"},
        "search": {"source_records", "coverage", "selected_sources"},
        "export": {"format", "exported"},
    }[stage_type]
    if not required.issubset(value):
        raise ValueError("research stage envelope is missing required stage data")
    common = {"contract_version", "stage_type", "usage", "content"}
    if set(value) - common - allowed:
        raise ValueError("research stage envelope contains unowned fields")
    return value


def _contains_remote_reference(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                if not isinstance(child, str) or not child.startswith("#"):
                    return True
            if _contains_remote_reference(child):
                return True
    elif isinstance(value, list):
        return any(_contains_remote_reference(item) for item in value)
    return False


def validate_user_schema(schema: Any) -> Draft202012Validator:
    """Compile only local-reference Draft 2020-12 JSON schemas."""
    if not isinstance(schema, dict):
        raise ValueError("extraction schema must be a JSON object")
    version = schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
    if version != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("only JSON Schema Draft 2020-12 is supported")
    if _contains_remote_reference(schema):
        raise ValueError("remote JSON Schema references are not allowed")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError("extraction schema is invalid") from exc
    checker = FormatChecker()
    requested = _schema_formats(schema)
    unsupported = requested - checker.checkers.keys()
    if unsupported:
        raise ValueError(
            "unsupported extraction schema format: " + ", ".join(sorted(unsupported))
        )
    return Draft202012Validator(schema, format_checker=checker)


def _schema_formats(schema: Any) -> set[str]:
    if isinstance(schema, dict):
        found = {schema["format"]} if isinstance(schema.get("format"), str) else set()
        for child in schema.values():
            found.update(_schema_formats(child))
        return found
    if isinstance(schema, list):
        result: set[str] = set()
        for child in schema:
            result.update(_schema_formats(child))
        return result
    return set()


def _scalar_leaves(value: Any, pointer: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        return [
            leaf
            for key, child in value.items()
            for leaf in _scalar_leaves(
                child, pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
            )
        ]
    if isinstance(value, list):
        return [
            leaf
            for index, child in enumerate(value)
            for leaf in _scalar_leaves(child, pointer + "/" + str(index))
        ]
    return [] if value is None else [(pointer or "", value)]


def validate_extraction_record(
    record: dict[str, Any],
    *,
    schema: dict[str, Any],
    source_parts: Mapping[tuple[str, str], str],
    trusted_pages: Mapping[tuple[str, str], set[str]] | None = None,
) -> dict[str, Any]:
    """Validate schema conformity and exact, same-source quote provenance."""
    parsed = ExtractionRecord.model_validate(record)
    validator = validate_user_schema(schema)
    errors = list(validator.iter_errors(parsed.data))
    if errors:
        raise ValueError("extracted data does not match the required schema")
    part_key = (parsed.source_id, parsed.part_id)
    source_text = source_parts.get(part_key)
    if source_text is None:
        raise ValueError("extraction references an unknown source part")
    evidence_by_pointer: dict[str, EvidenceItem] = {}
    for evidence in parsed.evidence:
        if evidence.pointer in evidence_by_pointer:
            raise ValueError("duplicate evidence pointer")
        if evidence.quote not in source_text:
            raise ValueError(
                "evidence quote is not an exact substring of the referenced source"
            )
        if evidence.page_reference is not None:
            allowed = (trusted_pages or {}).get(part_key, set())
            if evidence.page_reference not in allowed:
                raise ValueError(
                    "page reference was not supplied by trusted source metadata"
                )
        evidence_by_pointer[evidence.pointer] = evidence
    missing = [
        pointer
        for pointer, _ in _scalar_leaves(parsed.data)
        if pointer not in evidence_by_pointer
    ]
    if missing:
        raise ValueError("non-null extracted values lack exact evidence")
    return parsed.model_dump(mode="json")


def validate_screening(
    response: dict[str, Any], requested_parts: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Require exactly one screening decision for each requested source part."""
    if set(response) != {"screening"} or not isinstance(
        response.get("screening"), list
    ):
        raise ValueError("screen response must contain only a screening list")
    decisions = [
        ScreeningDecision.model_validate(item) for item in response["screening"]
    ]
    ids = [(item.source_id, item.part_id) for item in decisions]
    if len(ids) != len(set(ids)) or set(ids) != requested_parts:
        raise ValueError(
            "screen response has missing, duplicate, or unknown source parts"
        )
    return [item.model_dump(mode="json") for item in decisions]


def resolve_parameters(value: Any, parameters: dict[str, Any]) -> Any:
    """Resolve typed whole-value placeholders and bounded embedded JSON text."""
    if isinstance(value, list):
        return [resolve_parameters(item, parameters) for item in value]
    if isinstance(value, dict):
        return {
            key: resolve_parameters(item, parameters) for key, item in value.items()
        }
    if not isinstance(value, str):
        return value

    match = re.fullmatch(
        r"\s*(?:\$([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})\s*", value
    )
    if match:
        name = match.group(1) or match.group(2)
        if name not in parameters:
            raise ValueError(f"unknown required parameter placeholder: {name}")
        return copy.deepcopy(parameters[name])

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name not in parameters:
            raise ValueError(f"unknown required parameter placeholder: {name}")
        return json.dumps(parameters[name], ensure_ascii=False, separators=(",", ":"))

    return re.sub(
        r"\$([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value
    )


def merge_stage_output(
    context: dict[str, Any], output: dict[str, Any], step_index: int
) -> dict[str, Any]:
    """Merge only declared stage outputs while preserving completed evidence."""
    merged = copy.deepcopy(context)
    if (
        output.get("contract_version") != CONTRACT_VERSION
        or output.get("stage_type") not in STAGE_NAMES
    ):
        previous_verification = merged.get("verification")
        merged.update(copy.deepcopy(output))
        incoming_verification = merged.get("verification")
        if (
            isinstance(previous_verification, dict)
            and previous_verification.get("passed") is False
            and isinstance(incoming_verification, dict)
        ):
            preserved = copy.deepcopy(previous_verification)
            preserved["passed"] = False
            preserved["continued_after_failure"] = bool(
                previous_verification.get("continued_after_failure")
                or incoming_verification.get("continued_after_failure")
            )
            merged["verification"] = preserved
        return merged
    stage_type = output["stage_type"]
    validate_envelope(output, stage_type)
    merged["contract_version"] = CONTRACT_VERSION
    stage_results = copy.deepcopy(merged.get("stage_results", {}))
    if not isinstance(stage_results, dict):
        stage_results = {}
    stage_results[str(step_index)] = copy.deepcopy(output)
    merged["stage_results"] = stage_results
    if stage_type == "search":
        if not isinstance(output.get("source_records"), list):
            raise ValueError("search source_records must be a list")
        for key in (
            "source_records",
            "coverage",
            "selected_sources",
            "query",
            "sources",
        ):
            if key in output:
                merged[key] = copy.deepcopy(output[key])
        return merged
    if stage_type == "screen":
        merged["screening"] = copy.deepcopy(output.get("screening", []))
        merged["included_source_ids"] = copy.deepcopy(
            output.get("included_source_ids", [])
        )
    elif stage_type == "extract":
        merged["extractions"] = copy.deepcopy(output.get("extractions", []))
    elif stage_type == "synthesize":
        merged["synthesis"] = copy.deepcopy(output.get("synthesis", {}))
    elif stage_type == "export":
        for key in ("exported", "format", "markdown", "media_type", "content"):
            if key in output:
                merged[key] = copy.deepcopy(output[key])
    elif stage_type == "verify":
        previous = merged.get("verification")
        current = copy.deepcopy(output.get("verification", {}))
        if isinstance(previous, dict) and previous.get("passed") is False:
            previous_claims = {
                item.get("claim_id"): item
                for item in previous.get("claims", [])
                if isinstance(item, dict) and item.get("status") != "supported"
            }
            current_claims = list(current.get("claims", []))
            current_by_id = {
                item.get("claim_id"): index
                for index, item in enumerate(current_claims)
                if isinstance(item, dict)
            }
            for claim_id, failed_claim in previous_claims.items():
                if claim_id in current_by_id:
                    current_claims[current_by_id[claim_id]] = copy.deepcopy(
                        failed_claim
                    )
                else:
                    current_claims.append(copy.deepcopy(failed_claim))
            current["claims"] = current_claims
            current["passed"] = False
            current["deterministic_passed"] = bool(
                previous.get("deterministic_passed", True)
                and current.get("deterministic_passed", True)
            )
            current["schema_passed"] = bool(
                previous.get("schema_passed", True)
                and current.get("schema_passed", True)
            )
            if previous.get("semantic_status") in {"failed", "unverified"}:
                current["semantic_status"] = previous["semantic_status"]
            current["coverage_complete"] = bool(
                previous.get("coverage_complete", False)
                and current.get("coverage_complete", False)
            )
            current["continued_after_failure"] = bool(
                previous.get("continued_after_failure")
                or current.get("continued_after_failure")
            )
        merged["verification"] = current
    processing_coverage = copy.deepcopy(merged.get("processing_coverage", {}))
    if isinstance(output.get("processing_coverage"), dict):
        processing_coverage.update(copy.deepcopy(output["processing_coverage"]))
    merged["processing_coverage"] = processing_coverage
    return merged


def immediate_stage_envelope(
    context: Mapping[str, Any], expected_stage_type: str
) -> dict[str, Any]:
    """Return a copy of the latest persisted envelope for one upstream stage."""

    stage_results = context.get("stage_results")
    if not isinstance(stage_results, Mapping):
        raise ValueError("research stage is missing its immediate upstream envelope")
    ordered: list[tuple[int, Mapping[str, Any]]] = []
    for index, value in stage_results.items():
        if not isinstance(value, Mapping):
            continue
        try:
            numeric_index = int(index)
        except (TypeError, ValueError):
            continue
        if value.get("stage_type") == expected_stage_type:
            ordered.append((numeric_index, value))
    if not ordered:
        raise ValueError("research stage is missing its immediate upstream envelope")
    return copy.deepcopy(dict(max(ordered, key=lambda item: item[0])[1]))


def rehydrate_stage_outputs(steps: list[Any]) -> dict[str, Any]:
    """Rebuild accumulated context from persisted ResearchStep-like rows."""
    ordered = sorted(steps, key=lambda step: int(step.step_index))
    expected = list(range(len(ordered)))
    observed = [int(step.step_index) for step in ordered]
    if observed != expected:
        raise ValueError("persisted research steps are not contiguous")
    context: dict[str, Any] = {}
    for step in ordered:
        output = step.output
        if isinstance(output, dict):
            context = merge_stage_output(context, output, int(step.step_index))
    return context


def finite_number(value: Any) -> bool:
    """Return whether a JSON numeric value is finite without accepting booleans."""
    return type(value) in (int, float) and math.isfinite(value)
