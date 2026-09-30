# Daily Research Brief source-default enablement evidence

Date: 2026-09-28

Source commit: `2d7c52cff01be1fc03daef1bd9a88728045b0e67`

Parent commit: `e442a5bbe02a752ad93075893945d8490a0b08c0`

Authorization scope: the user authorized enabling the repository source default
and opening a pull request. This record covers only the local source change.
Production configuration was not inspected or changed, and no push, pull
request, deployment, or production flag operation was performed by this
verification run.

## Behavior

`DAILY_RESEARCH_BRIEF_ENABLED` now defaults to `true`. With no override, the
server lists and loads the bundled Daily Research Brief template, accepts
server-owned blueprint creation, and applies the existing scope checks when a
new run starts. An explicit environment value of `false` still parses as false,
hides list/detail, refuses Daily blueprint creation, and refuses a new run from
an existing Daily blueprint. Custom workflows remain available. Existing-run
read/export behavior is unchanged because the runtime guard remains limited to
template discovery and new work.

## TDD evidence

The focused expectations were run before changing the setting default:

```text
PYTHONPATH=backend .venv/bin/python -m pytest -c backend/pytest.ini -q \
  backend/tests/unit/services/test_blueprint_loader.py \
  backend/tests/unit/api/test_research_template_security.py \
  backend/tests/unit/api/test_research_engine_endpoints.py
```

Expected RED: `14 failed, 21 passed` in `10.87s`. Failures were the old default
hiding or refusing Daily work; the explicit-false loader/create/start guards
remained green. Raw capture SHA-256:
`5d778f3b8ca658692f6f1a557dfc6bbd986a1636266f419b4d29c410e6f4d18c`.

After the one-line default change, the same pre-commit command passed `35`
tests in `10.52s`. Raw capture SHA-256:
`7b3c012645d905a8b7603f67fb60a04ad8564619e701fa9f61dade4f65100e14`.

## Exact-source checks

All results below ran on exact source
`2d7c52cff01be1fc03daef1bd9a88728045b0e67`.

| Check | Classification | Result |
| --- | --- | --- |
| Focused loader/template/API command above | PASS | `35 passed`, 2 pre-existing warnings, `11.15s`; raw SHA-256 `690233dbf3c5350bcd2c2f04ac36d4da17d542ba4065751f1609113ecafcd033`. |
| Ruff check and format check on the four changed files | PASS | All checks passed; all four files already formatted; raw SHA-256 `4bd8248f4985e327cd636d5b563aa46d1d34db2021498c8d8b37613b35f181d1`. |
| MyPy on `blueprints/loader.py` | PASS | No issues in one source file; raw SHA-256 `5d4b6d285b77932e3d08212c3b4974d0a803f98ec60408ac6ccf6a063fa6c19e`. |
| Direct MyPy diagnostic comparison for `config.py` | FAILED, baseline-identical | Exact source and its parent each report the same 16 existing `no-untyped-def` diagnostics; the changed setting adds none. Current raw SHA-256 `c982550645e82efdafb4052eca4e582975ae119381d71108080e0c79c5101bad`; parent raw SHA-256 `3defc2e1cc30386df481fce99a74f930825fc599fac27a05cfc48beebdb773f4`. |
| `python scripts/ci/generate_openapi.py --check` | PASS | OpenAPI snapshot is current; raw SHA-256 `68b90a6628fe6ab5d5f54151fc5a50b8c6c2cfd96c5055af6a97c018a656a1b8`. |
| Bandit `1.9.4`, repository baseline and CI severity filters | PASS | No unsuppressed medium/high-confidence source finding; raw SHA-256 `a46b6071658e641ad5f988e0542e9ad6f2ee7224bd4f70171aa7f2e668e614fd`. |
| Gitleaks `8.30.1`, exact commit | PASS | One commit scanned; no leak; raw SHA-256 `4b1bd1b7ab01a7ac26f607878f2b926fe4b53cab1091dca4a343d36e5f948ec0`. |

No HTTP schema, frontend source, database schema, lifecycle transition, report
rendering, or performance path changed. Dedicated type generation, frontend,
PostgreSQL, migration, browser, and performance reruns were therefore not part
of this narrow source-default amendment; their earlier results remain bound to
their recorded source revisions and are not relabeled.

## Release ledger retained

The source default was changed under explicit user authorization despite the
existing negative release decision. JavaScript and Python dependency audits,
anonymous legacy Safety, full frontend validation, and full local CI remain
`FAILED`. Current-model evaluation and authenticated Safety remain `BLOCKED`.
Remote candidate-SHA checks, production configuration/rollback inspection, and
deployment or production flag changes remain `NOT RUN`. This local source
change does not claim that any of those risks was cleared.
