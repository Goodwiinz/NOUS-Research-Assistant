# Confirmed alphaXiv workflows in NOUS

Implement four backend workflows on the existing arXiv ingestion and document
storage paths: tenant-scoped metadata fetch on cache miss, explicit abstract-page
sync, administrator-triggered recent-paper background ingestion, and durable PDF
delivery through the authenticated same-origin file endpoint.

The source evidence is the alphaXiv traffic report and development schema in the
October 15 investigation artifacts. They establish workflow contracts, not
production server-to-server requests. NOUS deliberately uses its existing arXiv
Atom client for initial metadata/search and the abstract page for explicit sync.
No automatic refresh schedule or bulk-feed implementation is inferred or enabled.

Implementation sequence:

1. Add a small metadata cache, migration and service; preserve revision pins and
   distinguish unchecked withdrawal state from a checked false value.
2. Add authenticated routes and a bounded administrator-only Celery import.
   Revalidate the actor before delayed work; preserve tenant predicates.
3. Promote downloaded PDFs with existing compensating persistence, upgrading an
   abstract-only document when necessary. Reuse the existing file download route.
4. Verify cache isolation, parser failure behavior, PDF upgrades, authorization
   and queue errors. Regenerate both API contracts and run repository gates.
5. Have Claude Fable review the complete diff and address supported findings.

Deployment requires applying the migration and restarting API/Celery workers.
The feature does not install a scheduler or change the existing PDF-chat work.

## Local verification

Initial checks used base `588191ab772219c45b18feadc72b979208fa0f60`. After
Fable's first review, the fixes were rebased onto
`7fbd027ce9f9b7a222c4038edf7ea321416a86dd` (the discovery feature). Final checks:

- Backend architecture/API/arXiv/thread matrix: 1,834 passed, 72 skipped. The
  configured integration database at localhost:54322 is unavailable; those
  skipped tests are not claimed as passing.
- Ruff on backend/src, changed-file Black/isort, added-file mypy (8 files),
  directory docs, feature map, OpenAPI snapshot and frontend TypeScript passed.
- New migration upgrade/downgrade, RLS and browser-role denial, tenant isolation
  and older-write protection passed against an isolated temporary PostgreSQL.
  This is a delta test, not replay of the historical migration chain.
- Guard mutation checks rejected all five mutants; restored tests passed.
  Commands use the repository Python environment:

  ```sh
  python -m pytest -q backend/tests/unit/services/arxiv/test_papers.py::test_cache_hit_and_tenant_isolation
  python -m pytest -q backend/tests/unit/services/arxiv/test_papers.py::test_older_lookup_cannot_overwrite_sync
  python -m pytest -q backend/tests/unit/services/arxiv/test_persistence.py::test_require_pdf_upgrades_durable_abstract_in_place
  python -m pytest -q backend/tests/unit/services/arxiv/test_papers.py::test_recent_import_preserves_explicit_sync
  python -m pytest -q backend/tests/unit/services/arxiv/test_persistence.py::test_fulltext_ingest_enriches_pdf_only_row_without_reupload
  ```

  Guards are `papers.py:187` (cached return), `papers.py:160` (older-write
  rejection), `papers.py:156` (insert only on miss), `persistence.py:292`
  (required PDF), and `persistence.py:327` (full-text enrichment). Each was temporarily
  neutralized, its corresponding assertion failed, and the source bytes were
  restored before rerunning. Evidence logs are retained with the task's
  `implementation-review` artifacts.

Fable's first review identified full-text enrichment being blocked by artifact
reuse, overly narrow categories, cache-hit admission charges and sync metadata
being erased by recent imports. Regression tests cover each correction. Smaller
corrections normalize identifier casing/empty dates/PDF links, use a disposable
PDF download cache, and retain unknown status for ambiguous withdrawal wording.
