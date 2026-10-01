"""Run manifest v2: analyze-step parsing, assembly, completeness, hashes (GOO-312).

Stdlib only (``test_experiment_boundary`` enforces it), so the decision ledger
and GOO-313's rerun rules can import it. Nothing here reads a database, a
sandbox or storage: callers pass plain values and get plain values back.

A manifest binds one ``analyze`` step of one run. Completeness is derived,
never asserted: every required identity that is ``null`` or absent is listed
in ``missing``, a run that did not complete is listed as ``status``, and any
entry makes the manifest ``incomplete``. ``image_digest`` is recorded as
``null`` with a reason (E2B exposes a template id, not a content digest) and
does not block completeness; the pinned lock plus the template id is the
environment identity.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping

SCHEMA = "nous.run-manifest/2"
LEGACY_SCHEMA = "nous.run-manifest/1"
SCHEMA_VERSION = 2
STEP_TYPE = "analyze"
CODE_NAME = "main.py"
LOCK_NAME = "pip-freeze.txt"
INPUT_KINDS = ("document", "evidence_table_version")
OUTPUT_ROLES = ("figure", "table", "metrics", "data")
FIGURE_ROLES = ("figure", "table")
ARTIFACT_ROLES = ("input", "code", "environment", "output")
MAX_METRICS_BYTES = 32 * 1024
NO_DIGEST = {"value": None, "reason": "provider_exposes_no_digest"}
REQUIRED_PATHS = (
    "code.sha256",
    "environment.template_id",
    "environment.lock_sha256",
    "seed",
    "protocol_version_id",
    "question_version_id",
    "started_at",
    "completed_at",
)
_PER_FILE = ("sha256", "artifact_id")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# name[extras]==version, one per line of a pip requirements file; never a
# URL, a path or an unpinned range.
_PINNED = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?==[A-Za-z0-9][A-Za-z0-9.+!_-]*$"
)
# ponytail: the seed rule is a declared-field check, not static analysis of
# the code; a script that draws randomness without saying so is the
# author's deviation to record.
_STOCHASTIC = re.compile(
    r"(?i)\b(random|numpy\.random|torch|tensorflow|jax|bootstrap|monte[_ -]?carlo"
    r"|shuffle|sampling|permutation)\b"
)
_SECRET_PATTERN = re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\s*=(?!=)")
_MIN_SECRET_CHARS = 8


@dataclass(frozen=True)
class AnalyzeSpec:
    code: str
    code_sha256: str
    command: str
    parameters: dict[str, Any]
    seed: int | None
    inputs: tuple[dict[str, Any], ...]
    outputs: tuple[dict[str, Any], ...]
    requirements: tuple[str, ...]
    template: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(value: object) -> bytes:
    """Byte-identical to ``contracts.canonical_json_bytes`` (kept stdlib)."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_bytes(manifest))


def _safe_name(name: Any) -> str:
    if not isinstance(name, str) or not name or len(name) > 255:
        raise ValueError("unsafe_path")
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise ValueError("unsafe_path")
    if any(ord(char) < 32 for char in name):
        raise ValueError("unsafe_path")
    return name


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_analyze_step")
    return value


def _inputs(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError("invalid_analyze_step")
    parsed: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("invalid_analyze_step")
        name = _safe_name(item.get("name"))
        if item.get("kind") not in INPUT_KINDS:
            raise ValueError("unsupported_input_kind")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("invalid_analyze_step")
        try:
            ref = str(uuid.UUID(str(item.get("id"))))
        except ValueError as error:
            raise ValueError("invalid_analyze_step") from error
        parsed.append({"name": name, "kind": item["kind"], "id": ref, "sha256": digest})
    if len({item["name"] for item in parsed}) != len(parsed):
        raise ValueError("duplicate_input_name")
    return tuple(parsed)


def _outputs(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("invalid_analyze_step")
    parsed: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("invalid_analyze_step")
        name = _safe_name(item.get("name"))
        if item.get("role") not in OUTPUT_ROLES:
            raise ValueError("invalid_analyze_step")
        media_type = _text(item.get("media_type"))
        if len(media_type) > 255:
            raise ValueError("invalid_analyze_step")
        parsed.append({"name": name, "media_type": media_type, "role": item["role"]})
    if len({item["name"] for item in parsed}) != len(parsed):
        raise ValueError("duplicate_output_name")
    if sum(item["role"] == "metrics" for item in parsed) > 1:
        raise ValueError("invalid_analyze_step")
    return tuple(parsed)


def parse_analyze_step(params: Mapping[str, Any]) -> AnalyzeSpec:
    """Validate one ``analyze`` step's params; ``ValueError`` with a stable code."""
    if not isinstance(params, Mapping):
        raise ValueError("invalid_analyze_step")
    code = _text(params.get("code"))
    code_sha256 = params.get("code_sha256")
    if code_sha256 != sha256_hex(code.encode("utf-8")):
        raise ValueError("code_hash_mismatch")
    requirements = params.get("requirements") or []
    if not isinstance(requirements, list) or not all(
        isinstance(entry, str) and _PINNED.fullmatch(entry) for entry in requirements
    ):
        raise ValueError("unpinned_requirement")
    parameters = params.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError("invalid_analyze_step")
    seed = params.get("seed")
    if seed is not None and (type(seed) is not int):
        raise ValueError("invalid_analyze_step")
    if seed is None and _STOCHASTIC.search(json.dumps(parameters, sort_keys=True)):
        raise ValueError("missing_seed")
    return AnalyzeSpec(
        code=code,
        code_sha256=code_sha256,
        command=_text(params.get("command")),
        parameters=dict(parameters),
        seed=seed,
        inputs=_inputs(params.get("inputs") or []),
        outputs=_outputs(params.get("outputs")),
        requirements=tuple(requirements),
        template=_text(params.get("template")),
    )


def parse_metrics(data: bytes) -> Any:
    """The ``role: metrics`` output as JSON (at most 32 KiB)."""
    if len(data) > MAX_METRICS_BYTES:
        raise ValueError("invalid_metrics")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("invalid_metrics") from error


def build(
    *,
    run: Mapping[str, Any],
    protocol: Mapping[str, Any] | None,
    question: Mapping[str, Any] | None,
    spec: AnalyzeSpec,
    code: Mapping[str, Any],
    environment: Mapping[str, Any] | None,
    inputs: Iterable[Mapping[str, Any]],
    outputs: Iterable[Mapping[str, Any]],
    metrics: Any,
    status: str,
    deviation_ids: Iterable[str],
) -> dict[str, Any]:
    """The ``nous.run-manifest/2`` document. ``run`` carries ``run_id``,
    ``effective_plan_hash``, ``blueprint_id``, ``blueprint_version``,
    ``step_index``, ``started_at`` and ``completed_at``; absent identities
    stay ``None`` so ``completeness`` lists them."""
    hypothesis = None if question is None else question.get("hypothesis")
    return {
        "schema": SCHEMA,
        "run_id": run["run_id"],
        "protocol_version_id": None if protocol is None else protocol.get("id"),
        "protocol_content_hash": (
            None if protocol is None else protocol.get("content_hash")
        ),
        "question_version_id": None if question is None else question.get("id"),
        "hypothesis_sha256": (
            None if hypothesis is None else sha256_hex(str(hypothesis).encode("utf-8"))
        ),
        "effective_plan_hash": run.get("effective_plan_hash"),
        "blueprint_id": run.get("blueprint_id"),
        "blueprint_version": run.get("blueprint_version"),
        "step_index": run.get("step_index"),
        "command": spec.command,
        "parameters": spec.parameters,
        "seed": spec.seed,
        "code": {
            "sha256": code.get("sha256"),
            "artifact_id": code.get("artifact_id"),
            "repository": code.get("repository"),
            "commit": code.get("commit"),
            "commit_verified": False,
        },
        "environment": None if environment is None else dict(environment),
        "inputs": [
            {
                key: item.get(key)
                for key in ("name", "kind", "ref_id", "sha256", "byte_size")
            }
            | {"artifact_id": item.get("artifact_id")}
            for item in inputs
        ],
        "outputs": [
            {
                key: item.get(key)
                for key in ("name", "role", "media_type", "sha256", "byte_size")
            }
            | {"artifact_id": item.get("artifact_id")}
            for item in outputs
        ],
        "metrics": metrics,
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "status": status,
        "deviation_ids": sorted(str(value) for value in deviation_ids),
    }


def _lookup(manifest: Mapping[str, Any], path: str) -> Any:
    value: Any = manifest
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def completeness(
    manifest: Mapping[str, Any],
) -> tuple[Literal["complete", "incomplete"], list[str]]:
    """``("complete", [])`` only when every required identity is present
    and the run completed; otherwise every missing JSON path."""
    missing = [path for path in REQUIRED_PATHS if _lookup(manifest, path) is None]
    for group in ("inputs", "outputs"):
        items = manifest.get(group)
        if not isinstance(items, list):
            missing.append(group)
            continue
        for index, item in enumerate(items):
            for key in _PER_FILE:
                if not isinstance(item, Mapping) or item.get(key) is None:
                    missing.append(f"{group}[{index}].{key}")
    if not manifest.get("outputs"):
        missing.append("outputs")
    if manifest.get("status") != "completed":
        missing.append("status")
    return ("incomplete" if missing else "complete"), sorted(set(missing))


def legacy_view(
    legacy: Mapping[str, Any] | None, missing: tuple[str, ...] = ("schema_version<2",)
) -> dict[str, Any]:
    """A run without a v2 manifest: its legacy body, unchanged, and no
    synthesized code, environment, input or output identity."""
    return {
        "schema": LEGACY_SCHEMA,
        "completeness": "incomplete",
        "missing": list(missing),
        "legacy": dict(legacy or {}),
    }


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def assert_no_secrets(value: Any, *, secret_values: Iterable[str]) -> None:
    """``ValueError("manifest_secret_detected")`` when any string in ``value``
    contains a configured secret or a ``key=``-style credential assignment."""
    secrets = [s for s in secret_values if s and len(s) >= _MIN_SECRET_CHARS]
    for text in _strings(value):
        if _SECRET_PATTERN.search(text) or any(s in text for s in secrets):
            raise ValueError("manifest_secret_detected")


def artifact_key(organization_id: str, run_id: str, sha256: str) -> str:
    """Content-addressed private key; never put in a manifest or a DTO."""
    return f"artifacts/{organization_id}/research-runs/{run_id}/{sha256}"
