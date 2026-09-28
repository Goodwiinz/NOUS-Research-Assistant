#!/usr/bin/env python3
"""Validate and aggregate retained GOO-293 writing trial artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from pybtex.database import Person, parse_string

ROOT = Path(__file__).resolve().parents[2]
TASK_DIR = Path(__file__).resolve().parent
DOC_MARKER_RE = re.compile(r"\[Doc (\d+)\]")
VERDICTS = {
    "objective": {"passed", "failed", "not_run"},
    "semantic": {"passed", "failed", "not_run", "judge_unavailable"},
    "authorization": {"passed", "failed", "not_run"},
    "infrastructure": {"passed", "failed"},
}
SENSITIVE_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|credential|"
    r"authorization|proxy[_-]?authorization|cookie|set[_-]?cookie|session[_-]?id)",
    re.IGNORECASE,
)
BIBTEX_FENCE_RE = re.compile(
    r"```(?:bibtex|biblatex)\s*\n(?P<body>.*?)\n```", re.DOTALL | re.IGNORECASE
)
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
RUNNER_IMAGE_RE = re.compile(r"(?:[A-Za-z0-9._/:~-]+@)?sha256:[0-9a-f]{64}")
TRIAL_FIELDS = {
    "task_id",
    "seed",
    "cohort",
    "task_digest",
    "runtime",
    "passed",
    "verdicts",
    "failure_reason",
    "artifacts",
    "invalid_revision",
}
RUNTIME_FIELDS = {
    "run_id",
    "source_sha",
    "provider",
    "model",
    "runner_image",
    "verifier",
    "fixture_digest",
    "calibration_digest",
    "harness_digest",
    "tool_versions",
    "env_flags",
    "configuration",
    "authenticated",
    "real_provider",
    "source_attestation",
}
ARTIFACT_FIELDS = {
    "draft",
    "review",
    "adjudication",
    "calibration",
    "authorization",
    "citations",
    "exports",
}
ARTIFACT_RECORD_FIELDS = {"path", "sha256"}


class EvidenceError(ValueError):
    """Raised when retained trial evidence violates the declared protocol."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read JSON evidence {path}: {exc}") from exc


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _file_digest(path: Path) -> str:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise EvidenceError(f"cannot read retained artifact {path}: {exc}") from exc
    return f"sha256:{digest}"


def _resolve_artifact(bundle_path: Path, raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise EvidenceError(f"{bundle_path.name}: artifact path is required")
    candidate = (bundle_path.parent / raw_path).resolve()
    bundle_root = bundle_path.parent.resolve()
    if candidate != bundle_root and bundle_root not in candidate.parents:
        raise EvidenceError(f"{bundle_path.name}: artifact escapes its trial directory")
    if not candidate.is_file():
        raise EvidenceError(
            f"{bundle_path.name}: retained artifact is missing: {raw_path}"
        )
    return candidate


def _verify_declared_artifact(bundle_path: Path, record: Any) -> Path:
    if not isinstance(record, dict):
        raise EvidenceError(f"{bundle_path.name}: artifact record must be an object")
    path = _resolve_artifact(bundle_path, record.get("path"))
    observed = _file_digest(path)
    if record.get("sha256") != observed:
        raise EvidenceError(
            f"{bundle_path.name}: checksum mismatch for {record.get('path')}"
        )
    return path


def _reject_sensitive_configuration(value: Any, path: str = "configuration") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if (
                SENSITIVE_KEY_RE.search(str(key))
                and child is not None
                and child != ""
                and child is not False
            ):
                raise EvidenceError(
                    f"retained runtime configuration contains sensitive field {child_path}"
                )
            _reject_sensitive_configuration(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_configuration(child, f"{path}[{index}]")


def _reject_unknown_fields(
    bundle_path: Path, value: Any, allowed: set[str], path: str
) -> None:
    if not isinstance(value, dict):
        raise EvidenceError(f"{bundle_path.name}: {path} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EvidenceError(
            f"{bundle_path.name}: unsupported retained field(s) in {path}: "
            f"{', '.join(unknown)}"
        )


def _validate_retained_trial_shape(bundle_path: Path, trial: dict[str, Any]) -> None:
    """Keep the published report on an explicit, credential-safe schema."""
    _reject_unknown_fields(bundle_path, trial, TRIAL_FIELDS, "trial")
    runtime = trial.get("runtime")
    _reject_unknown_fields(bundle_path, runtime, RUNTIME_FIELDS, "runtime")
    if isinstance(runtime, dict) and runtime.get("source_attestation") is not None:
        _reject_unknown_fields(
            bundle_path,
            runtime["source_attestation"],
            ARTIFACT_RECORD_FIELDS,
            "runtime.source_attestation",
        )
    verdicts = trial.get("verdicts")
    _reject_unknown_fields(bundle_path, verdicts, set(VERDICTS), "verdicts")
    artifacts = trial.get("artifacts")
    if artifacts is not None:
        _reject_unknown_fields(bundle_path, artifacts, ARTIFACT_FIELDS, "artifacts")
        draft = artifacts.get("draft")
        if draft is not None:
            _reject_unknown_fields(
                bundle_path,
                draft,
                ARTIFACT_RECORD_FIELDS
                | {"id", "project_id", "version", "content_sha256"},
                "artifacts.draft",
            )
        for name in (
            "review",
            "adjudication",
            "calibration",
            "authorization",
            "citations",
        ):
            record = artifacts.get(name)
            if record is not None:
                _reject_unknown_fields(
                    bundle_path,
                    record,
                    ARTIFACT_RECORD_FIELDS,
                    f"artifacts.{name}",
                )
        exports = artifacts.get("exports")
        if exports is not None:
            _reject_unknown_fields(
                bundle_path, exports, {"markdown", "latex"}, "artifacts.exports"
            )
            for name in ("markdown", "latex"):
                record = exports.get(name)
                if record is not None:
                    _reject_unknown_fields(
                        bundle_path,
                        record,
                        ARTIFACT_RECORD_FIELDS,
                        f"artifacts.exports.{name}",
                    )
    proof = trial.get("invalid_revision")
    if proof is not None:
        _reject_unknown_fields(
            bundle_path,
            proof,
            {"attempted", "outcome", "review_id", "review", "before", "after"},
            "invalid_revision",
        )
        if proof.get("review") is not None:
            _reject_unknown_fields(
                bundle_path,
                proof["review"],
                ARTIFACT_RECORD_FIELDS,
                "invalid_revision.review",
            )
        for name in ("before", "after"):
            snapshot = proof.get(name)
            if snapshot is not None:
                _reject_unknown_fields(
                    bundle_path,
                    snapshot,
                    {"draft_id", "version", "content_sha256"},
                    f"invalid_revision.{name}",
                )


def _verify_source_checkout(source_sha: str) -> None:
    if SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise EvidenceError("source SHA must be a full lowercase Git commit SHA")
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{source_sha}^{{commit}}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tracked_changes = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise EvidenceError(f"cannot verify executed source checkout: {exc}") from exc
    if head != source_sha:
        raise EvidenceError(
            f"executed source checkout is {head}, not declared {source_sha}"
        )
    if tracked_changes:
        raise EvidenceError("executed source checkout has tracked modifications")


def _git_tree_sha(source_sha: str) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", f"{source_sha}^{{tree}}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise EvidenceError(f"cannot resolve source tree for {source_sha}") from exc


def _verify_source_attestation(
    bundle_path: Path, runtime: dict[str, Any], source_sha: str
) -> None:
    path = _verify_declared_artifact(bundle_path, runtime.get("source_attestation"))
    attestation = _load_json(path)
    empty_diff_digest = f"sha256:{hashlib.sha256(b'').hexdigest()}"
    if (
        not isinstance(attestation, dict)
        or attestation.get("run_id") != runtime.get("run_id")
        or attestation.get("source_sha") != source_sha
        or attestation.get("git_tree") != _git_tree_sha(source_sha)
        or attestation.get("tracked_diff_sha256") != empty_diff_digest
        or attestation.get("runner_image") != runtime.get("runner_image")
    ):
        raise EvidenceError(
            f"{bundle_path.name}: runner source attestation is not bound to the clean source tree"
        )


def _source_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _harness_digest() -> str:
    files = [TASK_DIR / "collect.py"]
    lines = [f"{_file_digest(path)}  {path.relative_to(ROOT)}" for path in files]
    return _canonical_digest(lines)


def _corpus(protocol: dict[str, Any], name: str) -> tuple[Path, dict[str, Any]]:
    relative = protocol.get("corpora", {}).get(name)
    if not isinstance(relative, str):
        raise EvidenceError(f"protocol corpus {name!r} is missing")
    path = (TASK_DIR / relative).resolve()
    value = _load_json(path)
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        raise EvidenceError(f"corpus {name!r} must contain a tasks list")
    return path, value


def _calibration_fixture(protocol: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    relative = protocol.get("calibration_fixture")
    if not isinstance(relative, str):
        raise EvidenceError("protocol calibration fixture is missing")
    path = (TASK_DIR / relative).resolve()
    value = _load_json(path)
    fixtures = value.get("fixtures") if isinstance(value, dict) else None
    if not isinstance(fixtures, list) or len(fixtures) < 2:
        raise EvidenceError("calibration fixture must contain known truth cases")
    identities = {
        fixture.get("id") for fixture in fixtures if isinstance(fixture, dict)
    }
    if identities != {"known-correct", "known-wrong"}:
        raise EvidenceError(
            "calibration fixture requires known-correct and known-wrong"
        )
    expected = {
        fixture.get("id"): fixture.get("expected")
        for fixture in fixtures
        if isinstance(fixture, dict)
    }
    if expected != {"known-correct": "accept", "known-wrong": "reject"}:
        raise EvidenceError("calibration truth outcomes are invalid")
    return path, value


def _task_index(
    protocol: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    tasks: dict[str, dict[str, Any]] = {}
    cohorts: dict[str, str] = {}
    seen_digests: dict[str, str] = {}
    for cohort in ("development", "held_out"):
        _, corpus = _corpus(protocol, cohort)
        for task in corpus["tasks"]:
            task_id = task.get("id") if isinstance(task, dict) else None
            if not isinstance(task_id, str) or not task_id:
                raise EvidenceError(f"{cohort} corpus contains a task without an id")
            if task_id in tasks:
                raise EvidenceError(f"duplicate task id across corpora: {task_id}")
            digest = _canonical_digest(task)
            if digest in seen_digests:
                raise EvidenceError(
                    f"development and held-out tasks overlap: {task_id} and "
                    f"{seen_digests[digest]}"
                )
            seen_digests[digest] = task_id
            tasks[task_id] = task
            cohorts[task_id] = cohort
    return tasks, cohorts


def _expected_trials(protocol: dict[str, Any]) -> set[tuple[str, int]]:
    supported = set(protocol.get("supported_seeds", []))
    expected: set[tuple[str, int]] = set()
    plan = protocol.get("trial_plan")
    if not isinstance(plan, list) or not plan:
        raise EvidenceError("protocol trial_plan must be a non-empty list")
    for entry in plan:
        task_id = entry.get("task_id") if isinstance(entry, dict) else None
        seeds = entry.get("seeds") if isinstance(entry, dict) else None
        if not isinstance(task_id, str) or not isinstance(seeds, list) or not seeds:
            raise EvidenceError("each trial_plan entry requires task_id and seeds")
        for seed in seeds:
            if seed not in supported:
                raise EvidenceError(f"trial seed {seed!r} is not declared as supported")
            key = (task_id, seed)
            if key in expected:
                raise EvidenceError(f"duplicate planned trial: {task_id}/{seed}")
            expected.add(key)
    return expected


def _trial_kinds(protocol: dict[str, Any]) -> dict[tuple[str, int], str]:
    return {
        (entry["task_id"], seed): entry["kind"]
        for entry in protocol["trial_plan"]
        for seed in entry["seeds"]
    }


def validate_protocol(protocol: dict[str, Any]) -> dict[str, Any]:
    tasks, cohorts = _task_index(protocol)
    expected = _expected_trials(protocol)
    missing = sorted(task_id for task_id, _ in expected if task_id not in tasks)
    if missing:
        raise EvidenceError(f"trial plan references unknown tasks: {missing}")
    expected_cohorts = {cohorts[task_id] for task_id, _ in expected}
    if expected_cohorts != {"development", "held_out"}:
        raise EvidenceError("trial plan must include development and held-out tasks")
    invalid_kinds = sorted(
        {
            str(entry.get("kind"))
            for entry in protocol["trial_plan"]
            if entry.get("kind") not in {"canonical", "near_boundary"}
        }
    )
    if invalid_kinds:
        raise EvidenceError(f"unsupported trial kinds: {invalid_kinds}")
    canonical = [
        entry for entry in protocol["trial_plan"] if entry.get("kind") == "canonical"
    ]
    canonical_count = sum(len(entry["seeds"]) for entry in canonical)
    if canonical_count != 5:
        raise EvidenceError("the canonical comparison must declare exactly five trials")
    calibration_path, calibration = _calibration_fixture(protocol)
    return {
        "tasks": tasks,
        "cohorts": cohorts,
        "expected": expected,
        "kinds": _trial_kinds(protocol),
        "corpus_digests": {
            name: _file_digest(_corpus(protocol, name)[0])
            for name in ("development", "held_out")
        },
        "calibration_digest": _file_digest(calibration_path),
        "calibration": calibration,
    }


def _validate_verdicts(bundle_path: Path, trial: dict[str, Any]) -> dict[str, str]:
    verdicts = trial.get("verdicts")
    if not isinstance(verdicts, dict):
        raise EvidenceError(f"{bundle_path.name}: verdicts object is required")
    normalized: dict[str, str] = {}
    for dimension, allowed in VERDICTS.items():
        verdict = verdicts.get(dimension)
        if verdict not in allowed:
            raise EvidenceError(
                f"{bundle_path.name}: invalid {dimension} verdict {verdict!r}"
            )
        normalized[dimension] = verdict
    is_product_pass = all(verdict == "passed" for verdict in normalized.values())
    if not isinstance(trial.get("passed"), bool) or trial["passed"] != is_product_pass:
        raise EvidenceError(
            f"{bundle_path.name}: passed flag must equal the computed verdict status"
        )
    return normalized


def _verify_invalid_revision(
    bundle_path: Path, proof: Any, draft: dict[str, Any]
) -> None:
    if not isinstance(proof, dict) or proof.get("attempted") is not True:
        raise EvidenceError(f"{bundle_path.name}: invalid revision proof is required")
    if proof.get("outcome") != "blocked" or not proof.get("review_id"):
        raise EvidenceError(
            f"{bundle_path.name}: invalid revision was not retained as blocked"
        )
    review_path = _verify_declared_artifact(bundle_path, proof.get("review"))
    review = _load_json(review_path)
    candidate_content = review.get("candidate_content")
    candidate_hash = (
        hashlib.sha256(candidate_content.encode("utf-8")).hexdigest()
        if isinstance(candidate_content, str)
        else None
    )
    if (
        review.get("id") != proof.get("review_id")
        or review.get("outcome") != "blocked"
        or review.get("project_id") != draft.get("project_id")
        or review.get("base_draft_id") != draft.get("id")
        or not candidate_content
        or review.get("candidate_content_hash") != candidate_hash
    ):
        raise EvidenceError(
            f"{bundle_path.name}: blocked revision review is not bound to the attempted revision"
        )
    before = proof.get("before")
    after = proof.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise EvidenceError(
            f"{bundle_path.name}: revision before/after state is required"
        )
    identity = ("draft_id", "version", "content_sha256")
    if any(before.get(field) != after.get(field) for field in identity):
        raise EvidenceError(
            f"{bundle_path.name}: invalid revision replaced the current valid artifact"
        )
    expected = {
        "draft_id": draft.get("id"),
        "version": draft.get("version"),
        "content_sha256": draft.get("content_sha256"),
    }
    if any(before.get(field) != expected[field] for field in identity):
        raise EvidenceError(
            f"{bundle_path.name}: revision snapshots are not bound to the retained draft"
        )


def _verify_draft_artifact(bundle_path: Path, trial: dict[str, Any]) -> dict[str, Any]:
    artifacts = trial.get("artifacts")
    if not isinstance(artifacts, dict):
        raise EvidenceError(f"{bundle_path.name}: passed verdict lacks artifacts")
    draft = artifacts.get("draft")
    if not isinstance(draft, dict) or not draft.get("id"):
        raise EvidenceError(f"{bundle_path.name}: exact saved draft id is required")
    if not draft.get("project_id"):
        raise EvidenceError(f"{bundle_path.name}: exact saved project id is required")
    if not isinstance(draft.get("version"), int) or draft["version"] < 1:
        raise EvidenceError(f"{bundle_path.name}: saved draft version is invalid")
    draft_path = _verify_declared_artifact(bundle_path, draft)
    draft_digest = _file_digest(draft_path)
    if draft.get("content_sha256") != draft_digest:
        raise EvidenceError(f"{bundle_path.name}: saved draft content hash mismatch")
    draft_content = draft_path.read_text(encoding="utf-8")
    markers = sorted(set(DOC_MARKER_RE.findall(draft_content)), key=int)
    if not markers:
        raise EvidenceError(
            f"{bundle_path.name}: saved draft has no canonical citations"
        )
    return {
        "artifacts": artifacts,
        "draft": draft,
        "digest": draft_digest,
        "content": draft_content,
        "markers": markers,
    }


def _verify_objective_artifacts(
    bundle_path: Path, trial: dict[str, Any], context: dict[str, Any]
) -> None:
    artifacts = context["artifacts"]
    draft = context["draft"]
    draft_digest = context["digest"]
    draft_content = context["content"]
    markers = context["markers"]
    run_id = trial["runtime"]["run_id"]

    review_path = _verify_declared_artifact(bundle_path, artifacts.get("review"))
    review = _load_json(review_path)
    review_content = review.get("candidate_content")
    review_content_hash = (
        hashlib.sha256(review_content.encode("utf-8")).hexdigest()
        if isinstance(review_content, str)
        else None
    )
    if review.get("outcome") != "passed" or not review.get("id"):
        raise EvidenceError(f"{bundle_path.name}: persisted passing review is required")
    if (
        review_content != draft_content
        or review_content_hash != draft_digest.removeprefix("sha256:")
        or review.get("candidate_content_hash") != review_content_hash
        or review.get("project_id") != draft.get("project_id")
    ):
        raise EvidenceError(
            f"{bundle_path.name}: review is not bound to the saved draft"
        )
    if review.get("draft_id") != draft.get("id") or review.get("run_id") != run_id:
        raise EvidenceError(
            f"{bundle_path.name}: review identity is not bound to this run and draft"
        )

    citation_path = _verify_declared_artifact(bundle_path, artifacts.get("citations"))
    citations = _load_json(citation_path)
    if not isinstance(citations, list) or not citations:
        raise EvidenceError(f"{bundle_path.name}: persisted citations are required")
    citation_indices = [
        row.get("citation_index") for row in citations if isinstance(row, dict)
    ]
    if len(citation_indices) != len(citations) or any(
        not isinstance(index, int) or index < 1 for index in citation_indices
    ):
        raise EvidenceError(
            f"{bundle_path.name}: citation rows require positive integer indices"
        )
    if len(set(citation_indices)) != len(citation_indices):
        raise EvidenceError(
            f"{bundle_path.name}: duplicate persisted citation indices are forbidden"
        )
    citation_markers = sorted(
        (str(index) for index in citation_indices),
        key=int,
    )
    if citation_markers != markers:
        raise EvidenceError(
            f"{bundle_path.name}: saved citation identities do not reconcile"
        )
    citation_rows: dict[str, dict[str, Any]] = {}
    citation_row_ids: set[str] = set()
    for row in citations:
        if not isinstance(row, dict):
            raise EvidenceError(f"{bundle_path.name}: citation rows must be objects")
        if not row.get("document_id"):
            raise EvidenceError(
                f"{bundle_path.name}: citation rows require document ids"
            )
        if not row.get("id") or row.get("draft_id") != draft.get("id"):
            raise EvidenceError(
                f"{bundle_path.name}: citation rows are not bound to the retained draft"
            )
        if str(row["id"]) in citation_row_ids:
            raise EvidenceError(
                f"{bundle_path.name}: duplicate persisted citation row identities"
            )
        citation_row_ids.add(str(row["id"]))
        metadata_source = row.get("metadata_source")
        if metadata_source not in {"citation", "document_fallback"}:
            raise EvidenceError(
                f"{bundle_path.name}: citation rows must identify canonical metadata source"
            )
        if not row.get("citation_id") and metadata_source != "document_fallback":
            raise EvidenceError(
                f"{bundle_path.name}: linked citation metadata requires a citation id"
            )
        canonical_metadata = row.get("canonical_metadata")
        required_metadata = {"title", "authors", "year", "venue", "doi", "arxiv_id"}
        if not isinstance(canonical_metadata, dict) or not required_metadata.issubset(
            canonical_metadata
        ):
            raise EvidenceError(
                f"{bundle_path.name}: citation rows require explicit canonical metadata"
            )
        marker = str(row["citation_index"])
        citation_rows[marker] = row

    exports = artifacts.get("exports")
    if not isinstance(exports, dict):
        raise EvidenceError(f"{bundle_path.name}: downloaded exports are required")
    markdown_path = _verify_declared_artifact(bundle_path, exports.get("markdown"))
    markdown = markdown_path.read_text(encoding="utf-8")
    separator = "\n\n## References\n\n"
    if separator not in markdown:
        raise EvidenceError(
            f"{bundle_path.name}: Markdown export has no References section"
        )
    markdown_body, references = markdown.rsplit(separator, 1)
    if markdown_body != draft_content:
        raise EvidenceError(
            f"{bundle_path.name}: Markdown export body is not the retained draft"
        )
    for marker in markers:
        if f"[Doc {marker}]" not in references and f"doc{marker}" not in references:
            raise EvidenceError(
                f"{bundle_path.name}: Markdown References lost canonical doc{marker}"
            )
    match = BIBTEX_FENCE_RE.search(references)
    if match is None:
        raise EvidenceError(
            f"{bundle_path.name}: Markdown evidence must retain parseable BibTeX"
        )
    _verify_bibtex_metadata(bundle_path, match.group("body"), citation_rows)

    latex_path = _verify_declared_artifact(bundle_path, exports.get("latex"))
    try:
        with zipfile.ZipFile(latex_path) as archive:
            tex_name = next(
                name for name in archive.namelist() if name.endswith(".tex")
            )
            tex = archive.read(tex_name).decode("utf-8")
            bib = archive.read("references.bib").decode("utf-8")
    except (
        OSError,
        StopIteration,
        UnicodeDecodeError,
        zipfile.BadZipFile,
        KeyError,
    ) as exc:
        raise EvidenceError(
            f"{bundle_path.name}: LaTeX export cannot be opened: {exc}"
        ) from exc
    for marker in markers:
        if f"\\cite{{doc{marker}}}" not in tex or not re.search(
            rf"@\w+\{{doc{marker}\s*,", bib
        ):
            raise EvidenceError(
                f"{bundle_path.name}: LaTeX/BibTeX lost canonical doc{marker}"
            )
    if tex != _expected_latex_document(draft_content):
        raise EvidenceError(
            f"{bundle_path.name}: LaTeX manuscript is not the retained draft"
        )
    _verify_bibtex_metadata(bundle_path, bib, citation_rows)

    _verify_invalid_revision(bundle_path, trial.get("invalid_revision"), draft)


def _expected_latex_document(markdown_content: str) -> str:
    escaped = re.sub(r"([\\{}$&%#^_~])", r"\\\1", markdown_content)
    latex = re.sub(
        r"^## (\d+\.\s)?(.+)$", r"\\section{\2}", escaped, flags=re.MULTILINE
    )
    latex = re.sub(r"^### (.+)$", r"\\subsection{\1}", latex, flags=re.MULTILINE)
    latex = re.sub(r"\[Doc (\d+)\]", r"\\cite{doc\1}", latex)
    title = markdown_content.split("\n")[0].replace("## ", "")
    title = re.sub(r"([\\{}$&%#^_~])", r"\\\1", title)
    return f"""\\documentclass{{article}}
\\usepackage{{natbib}}
\\usepackage{{hyperref}}

\\title{{{title}}}
\\author{{Generated by Research Assistant}}
\\date{{\\today}}

\\begin{{document}}

\\maketitle

{latex}

\\bibliographystyle{{plain}}
\\bibliography{{references}}

\\end{{document}}
"""


def _normalized_text(value: Any) -> str:
    text = str(value or "")
    for escaped, literal in (
        (r"\&", "&"),
        (r"\%", "%"),
        (r"\#", "#"),
        (r"\_", "_"),
        (r"\$", "$"),
        (r"\{", "{"),
        (r"\}", "}"),
    ):
        text = text.replace(escaped, literal)
    return re.sub(r"\s+", " ", text).strip()


def _verify_bibtex_metadata(
    bundle_path: Path,
    bibtex: str,
    citation_rows: dict[str, dict[str, Any]],
) -> None:
    try:
        bibliography = parse_string(bibtex, "bibtex")
    except Exception as exc:
        raise EvidenceError(
            f"{bundle_path.name}: downloaded BibTeX cannot be parsed: {exc}"
        ) from exc
    expected_keys = {f"doc{marker}" for marker in citation_rows}
    if set(bibliography.entries) != expected_keys:
        raise EvidenceError(
            f"{bundle_path.name}: exported BibTeX keys do not match saved citations"
        )
    for marker, row in citation_rows.items():
        entry = bibliography.entries[f"doc{marker}"]
        metadata = row["canonical_metadata"]
        fields = {
            "title": entry.fields.get("title"),
            "year": entry.fields.get("year"),
            "venue": entry.fields.get("journal") or entry.fields.get("booktitle"),
            "doi": entry.fields.get("doi"),
            "arxiv_id": entry.fields.get("eprint"),
        }
        for field, observed in fields.items():
            if _normalized_text(observed) != _normalized_text(metadata.get(field)):
                raise EvidenceError(
                    f"{bundle_path.name}: exported doc{marker} {field} differs "
                    "from canonical metadata"
                )
        expected_authors = [
            _normalized_text(Person(author))
            for author in (metadata.get("authors") or [])
        ]
        observed_authors = [
            _normalized_text(person) for person in entry.persons.get("author", [])
        ]
        if observed_authors != expected_authors:
            raise EvidenceError(
                f"{bundle_path.name}: exported doc{marker} authors differ from "
                "canonical metadata"
            )


def _verify_semantic_artifacts(
    bundle_path: Path,
    trial: dict[str, Any],
    context: dict[str, Any],
    protocol_state: dict[str, Any],
) -> None:
    artifacts = context["artifacts"]
    draft_digest = context["digest"]
    runtime = trial["runtime"]
    adjudication_path = _verify_declared_artifact(
        bundle_path, artifacts.get("adjudication")
    )
    adjudication = _load_json(adjudication_path)
    if (
        adjudication.get("independent") is not True
        or adjudication.get("verdict") != "passed"
        or not adjudication.get("id")
        or not adjudication.get("reviewer")
        or not adjudication.get("verifier")
    ):
        raise EvidenceError(
            f"{bundle_path.name}: independent passing adjudication is required"
        )
    if adjudication.get("draft_content_sha256") != draft_digest:
        raise EvidenceError(
            f"{bundle_path.name}: adjudication is not bound to the saved draft"
        )
    if adjudication.get("task_digest") != trial.get("task_digest"):
        raise EvidenceError(
            f"{bundle_path.name}: adjudication is not bound to the frozen task"
        )
    if adjudication.get("run_id") != runtime["run_id"]:
        raise EvidenceError(f"{bundle_path.name}: adjudication is not bound to the run")
    if adjudication.get("reviewer") in {runtime.get("provider"), runtime.get("model")}:
        raise EvidenceError(
            f"{bundle_path.name}: adjudicator must be independent of the trial model"
        )

    calibration_path = _verify_declared_artifact(
        bundle_path, artifacts.get("calibration")
    )
    calibration = _load_json(calibration_path)
    frozen_fixtures = {
        fixture["id"]: fixture for fixture in protocol_state["calibration"]["fixtures"]
    }
    results = calibration.get("results")
    retained_results = (
        {
            result.get("fixture_id"): result
            for result in results
            if isinstance(result, dict)
        }
        if isinstance(results, list)
        else {}
    )
    truth_is_bound = (
        isinstance(results, list)
        and len(results) == len(retained_results) == len(frozen_fixtures)
        and set(retained_results) == set(frozen_fixtures)
    )
    if truth_is_bound:
        for fixture_id, fixture in frozen_fixtures.items():
            result = retained_results[fixture_id]
            if (
                result.get("input_digest") != _canonical_digest(fixture["input"])
                or result.get("expected") != fixture["expected"]
                or result.get("observed") != fixture["expected"]
            ):
                truth_is_bound = False
                break
    if (
        not calibration.get("id")
        or calibration.get("outcome") != "passed"
        or calibration.get("reviewer") != adjudication.get("reviewer")
        or calibration.get("verifier") != adjudication.get("verifier")
        or adjudication.get("verifier") != runtime.get("verifier")
        or calibration.get("fixture_digest") != runtime["calibration_digest"]
        or adjudication.get("calibration_id") != calibration.get("id")
        or not truth_is_bound
    ):
        raise EvidenceError(
            f"{bundle_path.name}: passing adjudication lacks bound calibration evidence"
        )


def _verify_authorization_artifacts(
    bundle_path: Path, trial: dict[str, Any], context: dict[str, Any]
) -> None:
    artifacts = context["artifacts"]
    draft = context["draft"]
    authorization_path = _verify_declared_artifact(
        bundle_path, artifacts.get("authorization")
    )
    authorization = _load_json(authorization_path)
    if (
        not authorization.get("id")
        or authorization.get("draft_id") != draft.get("id")
        or authorization.get("run_id") != trial["runtime"]["run_id"]
    ):
        raise EvidenceError(
            f"{bundle_path.name}: authorization evidence is not bound to the run and draft"
        )
    checks = authorization.get("checks")
    if not isinstance(checks, dict):
        raise EvidenceError(f"{bundle_path.name}: authorization checks are required")
    collaborator = checks.get("collaborator_download")
    foreign = checks.get("foreign_project_download")
    expected_resource = {
        "resource_type": "draft_export",
        "resource_id": draft.get("id"),
    }
    if (
        not isinstance(collaborator, dict)
        or collaborator.get("status") != 200
        or not collaborator.get("principal_id")
        or collaborator.get("project_id") != draft.get("project_id")
        or any(
            collaborator.get(key) != value for key, value in expected_resource.items()
        )
    ):
        raise EvidenceError(
            f"{bundle_path.name}: collaborator download authorization is unproven"
        )
    if (
        not isinstance(foreign, dict)
        or foreign.get("status") != 404
        or not foreign.get("principal_id")
        or foreign.get("principal_id") == collaborator.get("principal_id")
        or not foreign.get("project_id")
        or foreign.get("project_id") == draft.get("project_id")
        or any(foreign.get(key) != value for key, value in expected_resource.items())
    ):
        raise EvidenceError(f"{bundle_path.name}: foreign-project denial is unproven")


def _verify_passed_artifacts(
    bundle_path: Path,
    trial: dict[str, Any],
    verdicts: dict[str, str],
    protocol_state: dict[str, Any] | None = None,
) -> None:
    passed_dimensions = {
        name
        for name in ("objective", "semantic", "authorization")
        if verdicts[name] == "passed"
    }
    if not passed_dimensions:
        return
    context = _verify_draft_artifact(bundle_path, trial)
    if "objective" in passed_dimensions:
        _verify_objective_artifacts(bundle_path, trial, context)
    if "semantic" in passed_dimensions:
        if protocol_state is None:
            raise EvidenceError(
                f"{bundle_path.name}: frozen calibration state is required"
            )
        _verify_semantic_artifacts(bundle_path, trial, context, protocol_state)
    if "authorization" in passed_dimensions:
        _verify_authorization_artifacts(bundle_path, trial, context)


def _verify_success_artifacts(bundle_path: Path, trial: dict[str, Any]) -> None:
    protocol = _load_json(TASK_DIR / "protocol.json")
    if not isinstance(protocol, dict):
        raise EvidenceError("protocol must be a JSON object")
    _verify_passed_artifacts(
        bundle_path,
        trial,
        {"objective": "passed", "semantic": "passed", "authorization": "passed"},
        validate_protocol(protocol),
    )


def _validate_runtime_identity(
    bundle_path: Path,
    runtime: dict[str, Any],
    protocol_state: dict[str, Any],
    cohort: str,
    source_sha: str,
) -> None:
    required_strings = (
        "run_id",
        "provider",
        "model",
        "runner_image",
        "verifier",
        "fixture_digest",
        "calibration_digest",
        "harness_digest",
    )
    if runtime.get("source_sha") != source_sha:
        raise EvidenceError(f"{bundle_path.name}: source SHA does not match this run")
    missing = [field for field in required_strings if not runtime.get(field)]
    if missing:
        raise EvidenceError(
            f"{bundle_path.name}: incomplete runtime identity: {', '.join(missing)}"
        )
    runner_image = str(runtime["runner_image"])
    if RUNNER_IMAGE_RE.fullmatch(runner_image) is None:
        raise EvidenceError(
            f"{bundle_path.name}: runner image must be pinned by sha256 digest"
        )
    if runtime["fixture_digest"] != protocol_state["corpus_digests"][cohort]:
        raise EvidenceError(
            f"{bundle_path.name}: fixture digest does not match the frozen corpus"
        )
    if runtime["calibration_digest"] != protocol_state["calibration_digest"]:
        raise EvidenceError(
            f"{bundle_path.name}: calibration digest does not match frozen truth"
        )
    if runtime["harness_digest"] != _harness_digest():
        raise EvidenceError(
            f"{bundle_path.name}: harness digest does not match this collector"
        )
    if not isinstance(runtime.get("configuration"), dict):
        raise EvidenceError(f"{bundle_path.name}: model configuration is required")
    if not isinstance(runtime.get("env_flags"), dict):
        raise EvidenceError(
            f"{bundle_path.name}: executed environment flags are required"
        )
    tool_versions = runtime.get("tool_versions")
    if (
        not isinstance(tool_versions, dict)
        or not tool_versions
        or any(
            not str(name).strip() or not str(version).strip()
            for name, version in tool_versions.items()
        )
    ):
        raise EvidenceError(f"{bundle_path.name}: pinned tool versions are required")
    _reject_sensitive_configuration(runtime["configuration"])
    _reject_sensitive_configuration(runtime["env_flags"], "runtime.env_flags")
    _verify_source_attestation(bundle_path, runtime, source_sha)


def _evidence_identities(
    bundle_path: Path, trial: dict[str, Any], verdicts: dict[str, str]
) -> set[tuple[str, str]]:
    identities = {("run", str(trial["runtime"]["run_id"]))}
    source_record = trial["runtime"].get("source_attestation")
    if isinstance(source_record, dict) and source_record.get("sha256"):
        identities.add(("source_attestation", str(source_record["sha256"])))
    artifacts = trial.get("artifacts")
    if not isinstance(artifacts, dict):
        return identities
    draft = artifacts.get("draft")
    if isinstance(draft, dict) and draft.get("id"):
        identities.add(("draft", str(draft["id"])))
        if draft.get("sha256"):
            identities.add(("draft_artifact", str(draft["sha256"])))
    artifact_names: list[str] = []
    if verdicts["objective"] == "passed":
        artifact_names.extend(("review",))
        citations = artifacts.get("citations")
        if isinstance(citations, dict) and citations.get("sha256"):
            identities.add(("citations_artifact", str(citations["sha256"])))
        exports = artifacts.get("exports")
        if isinstance(exports, dict):
            for name in ("markdown", "latex"):
                record = exports.get(name)
                if isinstance(record, dict) and record.get("sha256"):
                    identities.add((f"{name}_artifact", str(record["sha256"])))
        proof = trial.get("invalid_revision") or {}
        if proof.get("review_id"):
            identities.add(("blocked_review", str(proof["review_id"])))
        blocked_record = proof.get("review")
        if isinstance(blocked_record, dict) and blocked_record.get("sha256"):
            identities.add(("blocked_review_artifact", str(blocked_record["sha256"])))
    if verdicts["semantic"] == "passed":
        artifact_names.extend(("adjudication", "calibration"))
    if verdicts["authorization"] == "passed":
        artifact_names.extend(("authorization",))
    for name in artifact_names:
        record = artifacts.get(name)
        path = _verify_declared_artifact(bundle_path, record)
        if not isinstance(record, dict):
            raise EvidenceError(
                f"{bundle_path.name}: {name} artifact record must be an object"
            )
        value = _load_json(path)
        if not isinstance(value, dict) or not value.get("id"):
            raise EvidenceError(
                f"{bundle_path.name}: {name} evidence requires a durable id"
            )
        identities.add((name, str(value["id"])))
        identities.add((f"{name}_artifact", str(record["sha256"])))
    return identities


def validate_trial(
    bundle_path: Path,
    trial: dict[str, Any],
    protocol_state: dict[str, Any],
    source_sha: str,
) -> tuple[tuple[str, int], dict[str, str]]:
    _validate_retained_trial_shape(bundle_path, trial)
    task_id = trial.get("task_id")
    seed = trial.get("seed")
    if not isinstance(task_id, str) or not isinstance(seed, int):
        raise EvidenceError(
            f"{bundle_path.name}: task_id and integer seed are required"
        )
    key = (task_id, seed)
    if key not in protocol_state["expected"]:
        raise EvidenceError(f"{bundle_path.name}: undeclared trial {task_id}/{seed}")
    task = protocol_state["tasks"][task_id]
    cohort = protocol_state["cohorts"][task_id]
    if trial.get("cohort") != cohort:
        raise EvidenceError(f"{bundle_path.name}: cohort does not match frozen corpus")
    if trial.get("task_digest") != _canonical_digest(task):
        raise EvidenceError(f"{bundle_path.name}: task digest does not match corpus")
    runtime = trial.get("runtime")
    if not isinstance(runtime, dict):
        raise EvidenceError(f"{bundle_path.name}: runtime identity is required")
    _validate_runtime_identity(bundle_path, runtime, protocol_state, cohort, source_sha)
    verdicts = _validate_verdicts(bundle_path, trial)
    if any(
        verdicts[name] == "passed"
        for name in ("objective", "semantic", "authorization")
    ):
        if (
            runtime.get("authenticated") is not True
            or runtime.get("real_provider") is not True
        ):
            raise EvidenceError(
                f"{bundle_path.name}: a pass requires an authenticated "
                "real-provider run"
            )
        _verify_passed_artifacts(bundle_path, trial, verdicts, protocol_state)
    if (
        not all(verdict == "passed" for verdict in verdicts.values())
        and not str(trial.get("failure_reason") or "").strip()
    ):
        raise EvidenceError(
            f"{bundle_path.name}: non-passing trial needs a failure reason"
        )
    return key, verdicts


def _runtime_manifest(runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        field: runtime[field]
        for field in (
            "source_sha",
            "provider",
            "model",
            "runner_image",
            "verifier",
            "calibration_digest",
            "harness_digest",
            "tool_versions",
            "env_flags",
            "configuration",
            "authenticated",
            "real_provider",
        )
    }


def collect(
    protocol_path: Path, trials_dir: Path, output_path: Path, source_sha: str
) -> dict[str, Any]:
    _verify_source_checkout(source_sha)
    protocol = _load_json(protocol_path)
    if not isinstance(protocol, dict):
        raise EvidenceError("protocol must be a JSON object")
    state = validate_protocol(protocol)
    bundles = sorted(trials_dir.glob("*/trial.json"))
    if not bundles:
        raise EvidenceError(f"no retained trial bundles found under {trials_dir}")

    observed: dict[tuple[str, int], dict[str, str]] = {}
    evidence_owners: dict[tuple[str, str], tuple[str, int]] = {}
    runtime_manifest: dict[str, Any] | None = None
    runtime_manifest_digest: str | None = None
    trials = []
    for bundle_path in bundles:
        trial = _load_json(bundle_path)
        if not isinstance(trial, dict):
            raise EvidenceError(f"{bundle_path}: trial bundle must be an object")
        key, verdicts = validate_trial(bundle_path, trial, state, source_sha)
        manifest = _runtime_manifest(trial["runtime"])
        manifest_digest = _canonical_digest(manifest)
        if runtime_manifest_digest is None:
            runtime_manifest = manifest
            runtime_manifest_digest = manifest_digest
        elif manifest_digest != runtime_manifest_digest:
            raise EvidenceError(
                f"{bundle_path.name}: runtime manifest differs across retained trials"
            )
        if key in observed:
            raise EvidenceError(f"duplicate retained trial: {key[0]}/{key[1]}")
        observed[key] = verdicts
        for identity in _evidence_identities(bundle_path, trial, verdicts):
            owner = evidence_owners.get(identity)
            if owner is not None:
                raise EvidenceError(
                    f"reused {identity[0]} evidence across retained trials: "
                    f"{owner[0]}/{owner[1]} and {key[0]}/{key[1]}"
                )
            evidence_owners[identity] = key
        trials.append(trial)
    missing = sorted(state["expected"] - set(observed))
    if missing:
        raise EvidenceError(f"missing declared trials: {missing}")

    dimensions = {
        name: dict(Counter(verdicts[name] for verdicts in observed.values()))
        for name in VERDICTS
    }
    product_passes = sum(
        all(verdicts[name] == "passed" for name in VERDICTS)
        for verdicts in observed.values()
    )
    unscored = sum(
        verdicts["infrastructure"] == "failed"
        or verdicts["semantic"] == "judge_unavailable"
        for verdicts in observed.values()
    )
    sample_sizes = {
        "canonical": sum(kind == "canonical" for kind in state["kinds"].values()),
        "near_boundary": sum(
            kind == "near_boundary" for kind in state["kinds"].values()
        ),
        "development": sum(
            state["cohorts"][task_id] == "development" for task_id, _ in observed
        ),
        "held_out": sum(
            state["cohorts"][task_id] == "held_out" for task_id, _ in observed
        ),
    }

    def summarize_kind(kind: str) -> dict[str, Any]:
        selected = {
            key: verdicts
            for key, verdicts in observed.items()
            if state["kinds"][key] == kind
        }
        passes = sum(
            all(verdicts[name] == "passed" for name in VERDICTS)
            for verdicts in selected.values()
        )
        kind_unscored = sum(
            verdicts["infrastructure"] == "failed"
            or verdicts["semantic"] == "judge_unavailable"
            for verdicts in selected.values()
        )
        return {
            "sample_size": len(selected),
            "product_passes": passes,
            "product_failures": len(selected) - passes - kind_unscored,
            "unscored": kind_unscored,
            "dimensions": {
                name: dict(Counter(value[name] for value in selected.values()))
                for name in VERDICTS
            },
        }

    by_kind = {kind: summarize_kind(kind) for kind in ("canonical", "near_boundary")}
    canonical_semantic_passes = by_kind["canonical"]["dimensions"]["semantic"].get(
        "passed", 0
    )
    report = {
        "schema_version": "1.0",
        "protocol_id": protocol.get("id"),
        "source_sha": source_sha,
        "protocol_digest": _file_digest(protocol_path),
        "harness_digest": _harness_digest(),
        "corpus_digests": {
            name: state["corpus_digests"][name] for name in ("development", "held_out")
        },
        "calibration_digest": state["calibration_digest"],
        "runtime_manifest": runtime_manifest,
        "runtime_manifest_digest": runtime_manifest_digest,
        "sample_size": len(observed),
        "sample_sizes": sample_sizes,
        "product_passes": product_passes,
        "product_failures": len(observed) - product_passes - unscored,
        "unscored": unscored,
        "dimensions": dimensions,
        "by_kind": by_kind,
        "canonical_semantic_acceptance": {
            "passes": canonical_semantic_passes,
            "required": 4,
            "met": canonical_semantic_passes >= 4,
        },
        "trials": trials,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=TASK_DIR / "protocol.json")
    parser.add_argument("--trials-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", default=None)
    args = parser.parse_args()
    try:
        report = collect(
            args.protocol.resolve(),
            args.trials_dir.resolve(),
            args.output.resolve(),
            args.source_sha or _source_sha(),
        )
    except (EvidenceError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "sample_size": report["sample_size"],
                "product_passes": report["product_passes"],
                "product_failures": report["product_failures"],
                "unscored": report["unscored"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
