"""Report and safely backfill canonical links for legacy research projects.

The command is dry-run by default. It only infers links when the
ResearchProject and Collection UUIDs are identical; names are never matched.

Usage:
    python -m scripts.maintenance.backfill_research_project_collections
    python -m scripts.maintenance.backfill_research_project_collections --apply
    python -m scripts.maintenance.backfill_research_project_collections --require-resolved
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any

from sqlalchemy import Connection, text

from src.core.database import engine

_LOCK_KEY = "research-project-collection-backfill-v1"


def _entry(row: Any, category: str, reason: str) -> dict[str, Any]:
    collection_id = row.linked_collection_id or row.candidate_id
    return {
        "research_engine_project_id": str(row.project_id),
        "project_id": str(collection_id) if collection_id else None,
        "collection_id": str(collection_id) if collection_id else None,
        "category": category,
        "reason": reason,
    }


def build_report(connection: Connection) -> dict[str, Any]:
    """Classify every engine project in stable UUID order without writing."""
    rows = connection.execute(text("""
            SELECT
                rp.id AS project_id,
                rp.collection_id AS linked_collection_id,
                rp.owner_id AS project_owner_id,
                rp.is_deleted AS project_deleted,
                c.id AS candidate_id,
                c.is_deleted AS collection_deleted,
                w.id AS workspace_id,
                w.owner_id AS workspace_owner_id,
                w.organization_id AS workspace_organization_id,
                w.is_deleted AS workspace_deleted,
                w.is_archived AS workspace_archived,
                owner_user.organization_id AS owner_organization_id,
                linked_collection.id AS actual_collection_id,
                linked_collection.is_deleted AS actual_collection_deleted,
                linked_workspace.id AS actual_workspace_id,
                linked_workspace.owner_id AS actual_workspace_owner_id,
                linked_workspace.organization_id AS actual_workspace_organization_id,
                linked_workspace.is_deleted AS actual_workspace_deleted,
                linked_owner.organization_id AS actual_workspace_owner_organization_id,
                claimant.id AS claimant_id
            FROM research_projects AS rp
            LEFT JOIN collections AS c ON c.id = rp.id
            LEFT JOIN workspaces AS w ON w.id = c.workspace_id
            LEFT JOIN users AS owner_user ON owner_user.id = rp.owner_id
            LEFT JOIN collections AS linked_collection
              ON linked_collection.id = rp.collection_id
            LEFT JOIN workspaces AS linked_workspace
              ON linked_workspace.id = linked_collection.workspace_id
            LEFT JOIN users AS linked_owner
              ON linked_owner.id = linked_workspace.owner_id
            LEFT JOIN research_projects AS claimant
              ON claimant.collection_id = c.id
             AND claimant.id <> rp.id
            ORDER BY rp.id
            """)).all()

    entries: list[dict[str, Any]] = []
    for row in rows:
        if row.project_deleted:
            entries.append(_entry(row, "skipped", "project_deleted"))
        elif row.linked_collection_id is not None:
            if row.actual_collection_id is None:
                entries.append(_entry(row, "conflicting", "linked_collection_missing"))
            elif row.actual_collection_deleted:
                entries.append(_entry(row, "conflicting", "linked_collection_deleted"))
            elif row.actual_workspace_id is None:
                entries.append(_entry(row, "conflicting", "linked_workspace_missing"))
            elif row.actual_workspace_deleted:
                entries.append(_entry(row, "conflicting", "linked_workspace_deleted"))
            elif row.owner_organization_id != (
                row.actual_workspace_organization_id
                or row.actual_workspace_owner_organization_id
            ):
                entries.append(
                    _entry(row, "conflicting", "linked_organization_mismatch")
                )
            else:
                entries.append(_entry(row, "skipped", "already_linked"))
        elif row.candidate_id is None:
            entries.append(_entry(row, "unresolved", "no_identical_id_collection"))
        elif row.collection_deleted:
            entries.append(_entry(row, "conflicting", "collection_deleted"))
        elif row.workspace_id is None:
            entries.append(_entry(row, "conflicting", "workspace_missing"))
        elif row.workspace_deleted:
            entries.append(_entry(row, "conflicting", "workspace_deleted"))
        elif row.project_owner_id != row.workspace_owner_id:
            entries.append(_entry(row, "conflicting", "owner_mismatch"))
        elif (
            row.workspace_organization_id is not None
            and row.owner_organization_id != row.workspace_organization_id
        ):
            entries.append(_entry(row, "conflicting", "organization_mismatch"))
        elif row.claimant_id is not None:
            entries.append(_entry(row, "conflicting", "collection_already_linked"))
        else:
            entries.append(_entry(row, "mapped", "identical_id_owner_scope"))

    counts = Counter(entry["category"] for entry in entries)
    for category in ("mapped", "unresolved", "conflicting", "skipped"):
        counts.setdefault(category, 0)
    return {
        "mode": "dry-run",
        "counts": dict(sorted(counts.items())),
        "rows": entries,
        "active_invalid": sum(
            entry["reason"] not in {"project_deleted", "already_linked"}
            for entry in entries
        ),
    }


def run_backfill(
    connection: Connection, *, apply: bool = False, require_resolved: bool = False
) -> tuple[dict[str, Any], int]:
    """Run a deterministic report and optionally apply its safe mappings."""
    if apply:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": _LOCK_KEY}
        )
        connection.execute(text("""
                SELECT c.id
                FROM collections AS c
                JOIN research_projects AS rp ON rp.id = c.id
                ORDER BY c.id
                FOR UPDATE OF c
                """))
        connection.execute(text("""
                SELECT id FROM research_projects
                WHERE collection_id IS NULL
                ORDER BY id
                FOR UPDATE
                """))

    report = build_report(connection)
    report["mode"] = "apply" if apply else "dry-run"
    conflicts = report["counts"]["conflicting"]
    if apply and conflicts == 0:
        mapped_ids = [
            entry["research_engine_project_id"]
            for entry in report["rows"]
            if entry["category"] == "mapped"
        ]
        for project_id in mapped_ids:
            result = connection.execute(
                text("""
                    UPDATE research_projects AS rp
                    SET collection_id = c.id, updated_at = now()
                    FROM collections AS c
                    JOIN workspaces AS w ON w.id = c.workspace_id
                    JOIN users AS owner_user ON owner_user.id = w.owner_id
                    WHERE rp.id = :project_id
                      AND c.id = rp.id
                      AND rp.collection_id IS NULL
                      AND rp.owner_id = w.owner_id
                      AND rp.owner_id = owner_user.id
                      AND rp.is_deleted = false
                      AND c.is_deleted = false
                      AND w.is_deleted = false
                      AND (
                        w.organization_id IS NULL
                        OR owner_user.organization_id = w.organization_id
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM research_projects AS claimant
                        WHERE claimant.collection_id = c.id
                          AND claimant.id <> rp.id
                      )
                    """),
                {"project_id": project_id},
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"project {project_id} changed during collection-link backfill"
                )
        report["applied"] = len(mapped_ids)
        report["active_invalid"] = build_report(connection)["active_invalid"]
    else:
        report["applied"] = 0

    if conflicts:
        report["guard"] = "conflicts"
        return report, 2
    if require_resolved and report["active_invalid"]:
        report["guard"] = "active_projects_invalid"
        return report, 3
    report["guard"] = "resolved" if not report["active_invalid"] else "not_required"
    return report, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="persist safe identical-ID mappings"
    )
    parser.add_argument(
        "--require-resolved",
        action="store_true",
        help="exit nonzero while any active engine project remains unlinked",
    )
    args = parser.parse_args()

    with engine.begin() as connection:
        report, exit_code = run_backfill(
            connection,
            apply=args.apply,
            require_resolved=args.require_resolved,
        )
    print(json.dumps(report, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
