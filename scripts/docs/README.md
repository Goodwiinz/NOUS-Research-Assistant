# scripts/docs — directory documentation tooling

Two small, dependency-free Python tools that keep per-directory docs honest: a
**generator** that scaffolds a doc you are forced to fill in, and a **linter**
that blocks placeholder or empty docs from being merged. Both operate on the
"directory doc" convention — a `README.md` (preferred) or `doc.md` that explains
what a single directory contains and why.

The policy in one sentence: **a committed directory doc must say something true
about that directory** — no identical boilerplate, no headings with nothing
under them.

## Key files

| File                  | Purpose                                                                                                                                            |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check_dir_docs.py`   | Lint. Fails (exit 1) on placeholder boilerplate, un-filled scaffold markers, or near-empty docs. Run in CI and pre-commit.                         |
| `gen_dir_readme.py`   | Generator. Scaffolds a `README.md` whose content slots are explicit `TODO(dir-doc)` markers — designed to fail the lint until a human fills it in. |
| `dir-docs-ignore.txt` | Allowlist of docs the lint skips, one path glob per line. Keep it short and justified.                                                             |

## Usage

```sh
# Lint every tracked README.md / doc.md
make docs-lint                       # or: python3 scripts/docs/check_dir_docs.py

# Lint specific files (what pre-commit does)
python3 scripts/docs/check_dir_docs.py backend/src/foo/README.md

# Scaffold a fill-me-in doc for one directory
make docs-scaffold DIR=backend/src/foo

# Scaffold every tracked directory that has no doc yet (preview first)
python3 scripts/docs/gen_dir_readme.py --missing --dry-run
python3 scripts/docs/gen_dir_readme.py --missing
```

## What the linter rejects

1. **Boilerplate sentences** — the auto-placeholder template ("This directory is
   part of the project.", "Refer to the main project documentation for more
   information.", "Documentation for `./...`"). Matched case-insensitively.
2. **Un-filled scaffold markers** — `<!-- TODO(dir-doc): ... -->` left in a
   committed file. A freshly generated scaffold is _meant_ to fail here.
3. **Near-empty docs** — fewer than `MIN_BODY_WORDS` real content words once
   headings-that-echo-the-directory-name, code fences, HTML comments, and table
   separators are stripped. Informative headings and table cells count.

Tune the phrase list, marker, and word floor at the top of `check_dir_docs.py`.

## Where it runs

- **CI**: the `lightweight-checks` and `lint-backend` jobs in
  `.github/workflows/test-pipeline.yml` run `check_dir_docs.py` on every push and
  PR to `develop`/`main` (the old `fast-ci.yml` workflow no longer exists).
- **pre-commit**: the local `dir-docs` hook in `.pre-commit-config.yaml` checks
  staged `README.md` / `doc.md` files.
- **Make**: `make docs-lint` and `make docs-scaffold DIR=...`.

## Why this exists

A change that drops one identical placeholder file into every folder is worse
than no docs: it costs reviewers nothing to add and every future reader the time
to open a file that says nothing, and it rots the moment a directory changes.
These tools make the cheap-but-empty path fail and the real-content path easy.
