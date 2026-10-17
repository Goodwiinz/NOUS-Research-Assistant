"""Frozen synthetic provider diagnostic on an owned loopback PostgreSQL server."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("fixture.json")
NS = uuid.UUID("c30db4f1-83db-4d20-87e7-10fdbef16b12")
SOURCE_FILES = [
    "evals/discovery-provider-smoke-v1/probe.py",
    "evals/discovery-provider-smoke-v1/fixture.json",
    "backend/src/services/embedding/document_embedding_service.py",
    "backend/src/services/embedding/embedding_service.py",
    "backend/src/services/embedding/cohere_embed_service.py",
    "backend/src/services/search/fulltext_search_service.py",
    "backend/src/core/config.py",
    "backend/src/core/database.py",
    "backend/alembic/versions/jd01_document_embeddings.py",
    "backend/src/services/search/semantic_search_service.py",
    "backend/src/services/search/keyword_search_service.py",
    "backend/src/services/search/cohere_rerank_service.py",
    "backend/src/services/integrations/discover_papers.py",
    "backend/src/services/integrations/read_tools.py",
    "backend/src/services/integrations/context.py",
]


class ProofViolation(Exception):
    """A reachable contract/isolation failure, distinct from infrastructure."""


class SourceIdentityError(RuntimeError):
    """Undeclared source inputs prevent a reproducible provider measurement."""


class ProofPrerequisiteError(RuntimeError):
    """A configured runtime prerequisite failed before a valid measurement."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProofPrerequisiteError(message)


def sid(value: str) -> uuid.UUID:
    return uuid.uuid5(NS, value)


def identity() -> dict[str, Any]:
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True
    ).strip()
    if dirty:
        raise SourceIdentityError(
            "Provider proof requires a clean Git-visible checkout"
        )
    endpoint = os.environ.get(
        "COHERE_EMBED_ENDPOINT", "https://api.cohere.com/v2/embed"
    )
    return {
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_dirty": False,
        "source_tree": subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True
        ).strip(),
        "source_scope": "complete clean Git-visible checkout at source_revision",
        "files": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES
        },
        "python": platform.python_version(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ["SQLAlchemy", "asyncpg", "pgvector", "httpx"]
        },
        "installed_distributions": sorted(
            (
                {"name": distribution.metadata["Name"], "version": distribution.version}
                for distribution in importlib.metadata.distributions()
            ),
            key=lambda item: (item["name"].casefold(), item["version"]),
        ),
        "interpreter": {
            "implementation": platform.python_implementation(),
            "version": sys.version,
            "optimization": sys.flags.optimize,
            "cache_tag": sys.implementation.cache_tag,
        },
        "flags": {
            "DOCUMENT_EMBEDDINGS_ENABLED": True,
            "SEMANTIC_SEARCH_ENABLED": True,
            "AGENT_PAGE_FILTER": True,
            "RETRIEVE_PASSAGES_PAGE_TOP_N": 6,
            "RETRIEVE_PASSAGES_PAGE_SHORT_CIRCUIT": 8,
        },
        "process_env": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        "provider_endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
        "provider_endpoint_mode": (
            "azure_openai_compatible"
            if "azure" in endpoint.lower() or "services.ai" in endpoint.lower()
            else "cohere_native"
        ),
        "rerank_endpoint_sha256": hashlib.sha256(
            os.environ.get("COHERE_RERANK_ENDPOINT", "").encode()
        ).hexdigest(),
        "rerank_model": os.environ.get("COHERE_RERANK_MODEL", "Cohere-rerank-v4.0-pro"),
    }


def write_new(path: Path, value: dict[str, Any]) -> None:
    with path.open("x") as output:
        os.chmod(path, 0o600)
        json.dump(value, output, indent=2)
        output.write("\n")


def metric(ids: list[str], gold: set[str], k: int) -> float:
    return len(set(ids[:k]) & gold) / len(gold)


async def exercise(fixture: dict[str, Any], progress: dict[str, Any]) -> dict[str, Any]:
    # Imports happen only after the process has been bound to its scratch DB.
    from sqlalchemy import text

    from src.core.config import settings
    from src.core.database import AsyncSessionLocal, async_engine, engine
    from src.core.encryption import initialize_encryption
    from src.models import Collection, CollectionDocument, Document, Organization
    from src.models.document import DocumentType, ProcessingStatus
    from src.models.user import User, UserRole
    from src.models.workspace import Workspace
    from src.schemas.integration_context import IntegrationContext
    from src.services.embedding.cohere_embed_service import cohere_embed_service
    from src.services.embedding.document_embedding_service import (
        EmbedOutcome,
        document_embedding_service,
    )
    from src.services.integrations import discover_papers as dp
    from src.services.integrations.read_tools import _retrieve_passages
    from src.services.search.cohere_rerank_service import (
        _normalize_rerank_endpoint,
        cohere_rerank_service,
    )
    from src.services.search.fulltext_search_service import fulltext_search_service
    from src.services.search.semantic_search_service import semantic_search_strict

    for key, value in identity()["flags"].items():
        setattr(settings, key, value)
    if (
        engine.url.host not in {"127.0.0.1", "localhost", "::1"}
        or not str(engine.url.database).startswith("provider_probe_")
        or async_engine.url.database != engine.url.database
    ):
        raise ValueError("Resolved application engines must use the owned scratch DB")
    require(
        settings.COHERE_EMBED_MODEL == fixture["model"],
        "Embedding model differs from the frozen fixture",
    )
    declared = identity()
    require(
        cohere_embed_service.is_enabled and cohere_rerank_service.is_enabled,
        "Both Cohere providers must be configured before the first call",
    )
    require(
        hashlib.sha256(cohere_embed_service.endpoint.encode()).hexdigest()
        == declared["provider_endpoint_sha256"],
        "Embedding endpoint differs from declaration",
    )
    require(
        cohere_rerank_service.endpoint
        == _normalize_rerank_endpoint(os.environ.get("COHERE_RERANK_ENDPOINT", "")),
        "Rerank endpoint differs from declared configuration/adapter",
    )
    progress["served_rerank_endpoint_sha256"] = hashlib.sha256(
        str(cohere_rerank_service.endpoint).encode()
    ).hexdigest()
    require(
        cohere_rerank_service.model == declared["rerank_model"],
        "Rerank model differs from declaration",
    )
    require(
        document_embedding_service._active_provider() == fixture["provider"],
        "Embedding provider differs from the frozen fixture",
    )
    os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(os.urandom(32)).decode()
    initialize_encryption()

    def document(item: dict[str, Any], org: str = "a") -> Document:
        key = item["key"]
        return Document(
            id=sid(f"doc-{org}-{key}"),
            organization_id=sid(f"org-{org}"),
            title=item["title"],
            filename=f"{key}.txt",
            file_path=f"/synthetic/{key}.txt",
            file_size_bytes=len(item["text"].encode()),
            mime_type="text/plain",
            document_type=DocumentType.TEXT,
            processing_status=ProcessingStatus.COMPLETED,
            content_text=item["text"],
            content_summary=item["text"],
            document_metadata={"publication_date": item.get("published", "2020-01-01")},
        )

    docs = [document(item) for item in fixture["corpus"]]
    other = document(fixture["corpus"][0], "b")
    try:
        async with AsyncSessionLocal() as db:
            for org in ["a", "b"]:
                db.add(
                    Organization(
                        id=sid(f"org-{org}"), name=org, storage_limit_bytes=10**6
                    )
                )
            await db.flush()
            db.add(
                User(
                    id=sid("user"),
                    organization_id=sid("org-a"),
                    email="probe@example.test",
                    password_hash="unused",
                    first_name="Test",
                    last_name="Probe",
                    role=UserRole.USER,
                    is_active=True,
                )
            )
            await db.flush()
            db.add(
                Workspace(
                    id=sid("workspace"),
                    name="Synthetic",
                    owner_id=sid("user"),
                    organization_id=sid("org-a"),
                )
            )
            await db.flush()
            db.add(
                Collection(
                    id=sid("project"), name="Synthetic", workspace_id=sid("workspace")
                )
            )
            await db.flush()
            db.add_all([*docs, other])
            await db.flush()
            for doc in docs:
                db.add(
                    CollectionDocument(collection_id=sid("project"), document_id=doc.id)
                )
            await db.commit()
            await fulltext_search_service.async_update_document_search_vectors(
                [doc.id for doc in [*docs, other]], db
            )
            await db.commit()
            require(
                await db.scalar(
                    text("SELECT count(*) FROM documents WHERE search_vector IS NULL")
                )
                == 0,
                "Fixture keyword vectors were not populated",
            )
            require(
                await db.scalar(
                    text("SELECT to_regclass('document_embeddings') IS NOT NULL")
                ),
                "pgvector migration did not create document_embeddings",
            )
            for doc in [*docs, other]:
                outcome = await document_embedding_service.embed_document(db, doc)
                require(
                    outcome == EmbedOutcome.EMBEDDED,
                    f"Document embedding outcome: {outcome.value}",
                )

        by_id = {
            str(doc.id): fixture["corpus"][index]["key"]
            for index, doc in enumerate(docs)
        }
        rows: list[dict[str, Any]] = []
        progress["queries"] = rows
        served_ids: set[str] = set()
        for query in fixture["queries"]:
            after = (
                datetime.fromisoformat(query["published_after"]).date()
                if query.get("published_after")
                else None
            )
            before = (
                datetime.fromisoformat(query["published_before"]).date()
                if query.get("published_before")
                else None
            )
            async with AsyncSessionLocal() as db:
                hits = await semantic_search_strict(
                    db,
                    query=query["question"],
                    organization_id=sid("org-a"),
                    project_ids=(
                        None
                        if query.get("scope") == "organization"
                        else [sid("project")]
                    ),
                    published_after=after,
                    published_before=before,
                    limit=fixture["top_k"],
                )
            served_ids.update(hit.document_id for hit in hits)
            if any(hit.document_id not in by_id for hit in hits):
                raise ProofViolation("Semantic result outside the authorized corpus")
            semantic = [by_id[hit.document_id] for hit in hits]

            async def forbidden_arxiv(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                raise AssertionError(
                    "This declared probe requests library channels only"
                )

            payload = await dp.discover(
                dp.DiscoverRequest(
                    question=query["question"],
                    keywords=tuple(query["keywords"]),
                    difficulty=1,
                    sources=("library",),
                    published_after=after,
                    published_before=before,
                ),
                organization_id=sid("org-a"),
                library_scope=Collection.id == sid("project"),
                arxiv_search=forbidden_arxiv,
            )
            require(
                "error" not in payload,
                "Composite discovery returned an error envelope",
            )
            require(
                all(call["status"] in ["ok", "empty"] for call in payload["calls"]),
                "One or more composite provider calls failed",
            )
            served_ids.update(paper["id"] for paper in payload["papers"])
            if any(paper["id"] not in by_id for paper in payload["papers"]):
                raise ProofViolation("Composite result outside the authorized corpus")
            composite = [by_id[paper["id"]] for paper in payload["papers"]]
            if query.get("near_boundary"):
                if "vector-new" in semantic + composite:
                    raise ProofViolation("Result outside the declared date window")
            gold = set(query["gold"])
            rows.append(
                {
                    "query": query["key"],
                    "gold": sorted(gold),
                    "semantic": semantic,
                    "composite": composite,
                    "channel_statuses": payload["calls"],
                    "semantic_recall_at_1": metric(semantic, gold, 1),
                    "semantic_recall_at_3": metric(semantic, gold, 3),
                    "composite_recall_at_1": metric(composite, gold, 1),
                    "composite_recall_at_3": metric(composite, gold, 3),
                    "near_boundary": bool(query.get("near_boundary")),
                    "exact_name_single_relevant_result": (
                        len(composite) == 1 and composite[0] in gold
                        if query.get("exact_name")
                        else None
                    ),
                }
            )

        progress.update(
            query_phase_status="completed",
            aggregate={
                name: mean(row[name] for row in rows)
                for name in [
                    "semantic_recall_at_1",
                    "semantic_recall_at_3",
                    "composite_recall_at_1",
                    "composite_recall_at_3",
                ]
            },
            canonical_aggregate={
                name: mean(row[name] for row in rows if not row["near_boundary"])
                for name in [
                    "semantic_recall_at_1",
                    "semantic_recall_at_3",
                    "composite_recall_at_1",
                    "composite_recall_at_3",
                ]
            },
        )
        page_spec = fixture["page_filter"]
        distractors = [item for item in fixture["corpus"] if item["key"] != "retry"]
        pages = [
            distractors[index % len(distractors)]["text"]
            for index in range(page_spec["total_pages"])
        ]
        for number in page_spec["gold_pages"]:
            pages[number - 1] = fixture["corpus"][1]["text"]
        long = document(
            {
                "key": "pages-long",
                "title": "Synthetic page diagnostic",
                "text": "\n".join(
                    f"[Page {index + 1}]\n{value}" for index, value in enumerate(pages)
                ),
            }
        )
        short = document(
            {
                "key": "pages-short",
                "title": "Synthetic short diagnostic",
                "text": "\n".join(
                    f"[Page {index + 1}]\n{value}"
                    for index, value in enumerate(pages[: page_spec["short_pages"]])
                ),
            }
        )
        async with AsyncSessionLocal() as db:
            db.add_all([long, short])
            await db.flush()
            for doc in [long, short]:
                db.add(
                    CollectionDocument(collection_id=sid("project"), document_id=doc.id)
                )
            await db.commit()
        context = IntegrationContext(
            user_id=sid("user"),
            organization_id=sid("org-a"),
            project_id=sid("project"),
            grant_id=sid("synthetic-grant"),
        )
        async with AsyncSessionLocal() as db:
            await fulltext_search_service.async_update_document_search_vectors(
                [long.id, short.id], db
            )
            await db.commit()
        page_rows: list[dict[str, Any]] = []
        progress["pages"] = page_rows
        for doc in [long, short]:
            async with AsyncSessionLocal() as db:
                result = await _retrieve_passages(
                    db,
                    context,
                    sid("project"),
                    {"query": page_spec["question"], "document_ids": [str(doc.id)]},
                )
            if result.get("error") == "retrieval_unavailable":
                page_rows.append(
                    {
                        "document": "long" if doc.id == long.id else "short",
                        "status": "infrastructure",
                        "reason": "fulltext_fallback_unavailable",
                    }
                )
                continue
            if "error" in result or (
                doc.id == short.id and "page_filter" not in result
            ):
                raise ProofViolation(
                    "Declared owner/page fixture did not satisfy the read contract"
                )
            if "page_filter" not in result:
                page_rows.append(
                    {
                        "document": "long" if doc.id == long.id else "short",
                        "status": "infrastructure",
                        "reason": "page_filter_unavailable_or_fallback",
                        "fallback_chunk_count": len(result.get("chunks", [])),
                    }
                )
                continue
            page_rows.append(
                {
                    "document": "long" if doc.id == long.id else "short",
                    "status": "completed",
                    "metadata": result["page_filter"],
                    "returned_page_numbers": [
                        chunk["page_number"] for chunk in result["chunks"]
                    ],
                }
            )
        if page_rows[1]["status"] == "completed" and (
            page_rows[1]["metadata"]["accepted_pages"]
            != list(range(1, page_spec["short_pages"] + 1))
            or not page_rows[1]["metadata"]["short_circuited"]
        ):
            raise ProofViolation("Short-document skip did not retain all pages")
        accepted = set(page_rows[0].get("metadata", {}).get("accepted_pages", []))
        page_recall = (
            len(accepted & set(page_spec["gold_pages"])) / len(page_spec["gold_pages"])
            if page_rows[0]["status"] == "completed"
            else None
        )
        async with AsyncSessionLocal() as db:
            stored = (
                await db.execute(
                    text(
                        "SELECT model, vector_dims(embedding), count(*) FROM document_embeddings GROUP BY 1, 2"
                    )
                )
            ).all()
            require(
                sum(row[2] for row in stored) == len(docs) + 1,
                "Stored embedding count differs from the declared corpus",
            )
            require(
                all(
                    row[0] == f"cohere:{fixture['model']}"
                    and row[1] == fixture["dimensions"]
                    for row in stored
                ),
                "Stored embedding model or dimensions differ from the frozen fixture",
            )
            database = {
                "server": await db.scalar(text("SHOW server_version")),
                "pgvector": await db.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname='vector'")
                ),
                "revision": await db.scalar(
                    text("SELECT version_num FROM alembic_version")
                ),
                "embedding_rows": [list(row) for row in stored],
            }
        return {
            "status": (
                "infra_degraded"
                if any(row["status"] == "infrastructure" for row in page_rows)
                else "completed"
            ),
            "classification": fixture["classification"],
            "queries": rows,
            "aggregate": {
                name: mean(row[name] for row in rows)
                for name in [
                    "semantic_recall_at_1",
                    "semantic_recall_at_3",
                    "composite_recall_at_1",
                    "composite_recall_at_3",
                ]
            },
            "pages": page_rows,
            "page_gold_recall_at_6": page_recall,
            "canonical_query_count": sum(not row["near_boundary"] for row in rows),
            "near_boundary_query_count": sum(row["near_boundary"] for row in rows),
            "canonical_aggregate": {
                name: mean(row[name] for row in rows if not row["near_boundary"])
                for name in [
                    "semantic_recall_at_1",
                    "semantic_recall_at_3",
                    "composite_recall_at_1",
                    "composite_recall_at_3",
                ]
            },
            "database": database,
            "other_org_excluded": str(other.id) not in served_ids,
            "orgwide_isolation_control_query": "rag-paraphrase",
            "arxiv": "not requested",
            "http_grant_and_deployed_mcp": "not run",
        }
    finally:
        await async_engine.dispose()
        engine.dispose()


def run(declaration: dict[str, Any], artifacts: Path) -> dict[str, Any]:
    """Consume one declaration atomically and retain its attempt lifecycle."""
    identifier = declaration.get("declaration_id")
    if not isinstance(identifier, str) or len(identifier) != 32:
        raise SourceIdentityError(
            "A new --declare record with an attempt ID is required"
        )
    try:
        parsed = uuid.UUID(hex=identifier)
    except ValueError:
        raise SourceIdentityError("Invalid declaration attempt ID") from None
    if parsed.hex != identifier:
        raise SourceIdentityError("Invalid declaration attempt ID")
    registry = ROOT / "evals/jobs/provider-attempt-claims"
    registry.mkdir(mode=0o700, parents=True, exist_ok=True)
    claim = registry / f"{identifier}.jsonl"
    try:
        fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SourceIdentityError(
            "Declaration already consumed; declare a new attempt"
        ) from None
    with os.fdopen(fd, "w") as handle:
        handle.write(
            json.dumps(
                {
                    "status": "claimed",
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "declaration_sha256": hashlib.sha256(
                        json.dumps(declaration, sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    try:
        result = _run_once(declaration, artifacts)
    except BaseException as failure:
        terminal = {"status": "infrastructure", "error_type": type(failure).__name__}
        raise
    else:
        terminal = result
        return result
    finally:
        with claim.open("a") as handle:
            handle.write(json.dumps({"terminal": terminal}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _run_once(declaration: dict[str, Any], artifacts: Path) -> dict[str, Any]:
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    if declaration["identity"] != identity():
        raise SourceIdentityError("Inputs changed after declaration")
    try:
        url = make_url(os.environ["NOUS_PROVIDER_PROOF_DATABASE_URL"])
    except Exception:
        raise ValueError("Invalid local database URL") from None
    if url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only an owned loopback PostgreSQL server is allowed")
    name = f"provider_probe_{uuid.uuid4().hex[:12]}"
    admin = create_engine(
        url.set(drivername="postgresql+psycopg2"),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    scratch = url.set(database=name, drivername="postgresql")
    result: dict[str, Any] = {
        "status": "infrastructure",
        "identity": declaration["identity"],
    }
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        os.environ["DATABASE_URL"] = scratch.render_as_string(hide_password=False)
        os.environ["SUPABASE_DB_URL"] = scratch.render_as_string(hide_password=False)
        os.environ["ENVIRONMENT"] = "testing"
        os.environ.update(declaration["identity"]["process_env"])
        migrated = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT / "backend",
            capture_output=True,
            timeout=300,
            check=False,
        )
        migration_log = artifacts / f"{name}-migration.log"
        with migration_log.open("xb") as output:
            os.chmod(migration_log, 0o600)
            output.write(migrated.stdout + migrated.stderr)
        result["migration_log_sha256"] = hashlib.sha256(
            migrated.stdout + migrated.stderr
        ).hexdigest()
        result["migration_log"] = migration_log.name
        if migrated.returncode:
            raise RuntimeError("Scratch migration failed")
        sys.path.insert(0, str(ROOT / "backend"))
        result.update(asyncio.run(exercise(json.loads(FIXTURE.read_text()), result)))
    except ProofViolation as failure:
        result.update(status="objective_failure", violation=str(failure))
    except Exception as failure:
        result["error_type"] = type(failure).__name__
        result["status"] = "infrastructure"
        # Retain diagnostics only locally; never include provider keys or DB URLs.
        result["diagnostic"] = (
            str(failure)
            if isinstance(failure, (AssertionError, ProofPrerequisiteError))
            else "Execution unavailable"
        )
    finally:
        try:
            with admin.connect() as connection:
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
                )
            result["scratch_database_removed"] = True
        except Exception as cleanup_failure:
            result.update(
                status="infrastructure",
                cleanup_error_type=type(cleanup_failure).__name__,
                scratch_database_removed=False,
                retained_scratch_database=name,
            )
        admin.dispose()
    try:
        result["source_after"] = identity()
        if result["source_after"] != declaration["identity"]:
            raise SourceIdentityError("Inputs changed during provider execution")
    except Exception as failure:
        result.update(
            status="infrastructure",
            error_type=type(failure).__name__,
            source_integrity=(
                "failed" if isinstance(failure, SourceIdentityError) else "unavailable"
            ),
        )
    result["recorded_at"] = datetime.now(timezone.utc).isoformat()
    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--declare", type=Path)
    action.add_argument("--run", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.declare:
        write_new(
            args.declare,
            {
                "declared_at": datetime.now(timezone.utc).isoformat(),
                "declaration_id": uuid.uuid4().hex,
                "identity": identity(),
                "fixture": str(FIXTURE.relative_to(ROOT)),
            },
        )
        return
    if args.output is None or args.output.exists():
        raise ValueError("A new --output file is required")
    result = run(json.loads(args.run.read_text()), args.output.parent)
    write_new(args.output, result)
    print(json.dumps({"status": result["status"], "output": str(args.output)}))
    if result["status"] != "completed":
        raise SystemExit(1 if result["status"] == "objective_failure" else 2)


def main() -> None:
    try:
        _main()
    except SourceIdentityError as failure:
        print(
            json.dumps(
                {
                    "status": "infrastructure",
                    "error_type": type(failure).__name__,
                    "diagnostic": str(failure),
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
