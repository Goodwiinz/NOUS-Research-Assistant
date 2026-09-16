# Task 1 report: restore lazy cloud storage initialization

## Status

Complete. S3 and Supabase upload branches now resolve their existing lazy
helper properties before streaming the spool, so a newly constructed
`FileService` no longer calls `upload_fileobj` on `None`. Upload storage and
database cleanup behavior remains in the existing compensation flow.

The upload route preserves safe `HTTPException` and `FileValidationError`
details, while `FileStorageError` and unexpected failures log the diagnostic
internally and return the stable public detail `File upload failed`.

## Exact files changed

- `backend/src/services/documents/file_service.py`
- `backend/src/api/documents/files.py`
- `backend/tests/unit/services/test_file_service_cloud_upload.py`
- `backend/tests/unit/api/test_file_upload_errors.py`
- `.superpowers/sdd/2026-09-16-chat-adversarial-repairs/task-1-report.md`

The pre-existing audit and plan documents in the worktree were left unstaged.

## TDD evidence

RED, before the production edit:

```sh
PYTHONPATH=backend /tmp/chat-audit-20260916/venv/bin/python -m pytest -q \
  backend/tests/unit/services/test_file_service_cloud_upload.py \
  backend/tests/unit/api/test_file_upload_errors.py
```

Result: `7 failed, 2 passed`. The cloud branches raised the observed
`NoneType.upload_fileobj` error, helper constructors were not reached, safe
service errors were wrapped, and the route exposed the injected provider
exception text.

GREEN, after the production edit:

```sh
PYTHONPATH=backend /tmp/chat-audit-20260916/venv/bin/python -m pytest -q \
  backend/tests/unit/services/test_file_service_cloud_upload.py \
  backend/tests/unit/api/test_file_upload_errors.py
```

Result: `9 passed`.

## Verification

- Focused regressions, the preserved compensation suite, and upload guards:
  `29 passed` with:

  ```sh
  PYTHONPATH=backend /tmp/chat-audit-20260916/venv/bin/python -m pytest -q \
    backend/tests/unit/services/test_file_service_cloud_upload.py \
    backend/tests/unit/api/test_file_upload_errors.py \
    backend/tests/test_upload_compensating_delete.py \
    backend/tests/unit/api/test_audit_pr6_upload_guards.py
  ```

- Existing file route/download/async-DB regressions: `12 passed` with:

  ```sh
  PYTHONPATH=backend /tmp/chat-audit-20260916/venv/bin/python -m pytest -q \
    backend/tests/api/documents/test_files_endpoint_bugs.py \
    backend/tests/api/documents/test_download_s3_branch.py \
    backend/tests/api/documents/test_cancel_upload_cleanup.py \
    backend/tests/unit/test_files_async_db_awaited.py
  ```

- Changed-file Ruff, Black, isort, and whitespace checks passed:

  ```sh
  /tmp/chat-audit-20260916/venv/bin/ruff check \
    backend/src/services/documents/file_service.py \
    backend/src/api/documents/files.py \
    backend/tests/unit/services/test_file_service_cloud_upload.py \
    backend/tests/unit/api/test_file_upload_errors.py
  /tmp/chat-audit-20260916/venv/bin/black --check \
    backend/src/services/documents/file_service.py \
    backend/src/api/documents/files.py \
    backend/tests/unit/services/test_file_service_cloud_upload.py \
    backend/tests/unit/api/test_file_upload_errors.py
  /tmp/chat-audit-20260916/venv/bin/isort --check-only \
    backend/src/services/documents/file_service.py \
    backend/src/api/documents/files.py \
    backend/tests/unit/services/test_file_service_cloud_upload.py \
    backend/tests/unit/api/test_file_upload_errors.py
  git diff --check
  ```

## Concerns and limits

Pytest reported one warning because the isolated environment does not install
the repository's optional `timeout` plugin; test outcomes were unaffected.
The cloud regressions use patched in-memory helper constructors and do not
exercise live S3 or Supabase credentials. No deployment, push, merge, or live
storage operation was performed.

## Commit

`fix(upload): restore lazy cloud storage initialization`
