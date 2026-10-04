# NOUS documentation

Start with the document that matches your task. This repository contains current
engineering contracts alongside designs, plans, dated audits and retained
evidence; those records have different authority and freshness.

## Start here

| Task | Entry point |
| --- | --- |
| Understand the product and repository | [Project overview](../README.md) and [code maps](CODEMAPS/README.md) |
| Change backend, frontend or API behavior | [Current engineering contracts](engineering/README.md) |
| Work on research workflows | [Research protocols](engineering/research-protocols.md), [project migration](engineering/research-project-migration.md) and [paper discovery](engineering/paper-discovery.md) |
| Understand local Codex, MCP and publication | [Harness bridge contract](engineering/harness-bridge.md) |
| Run tests or interpret verification | [Testing contract](engineering/testing.md), [verification records](testing/README.md) and [evaluation evidence](../evals/README.md) |
| Operate or deploy the application | [Operational notes](engineering/gotchas.md), [operations](operations/README.md) and [infrastructure runbooks](runbooks/README.md) |
| Find audit findings and their evidence | [Audit index](audits/README.md) |
| Find a design, decision or implementation plan | [Decisions](decisions/README.md), [specifications](specs/README.md), [plans](plans/README.md) and [Superpowers records](superpowers/README.md) |
| Follow project knowledge and delivery in Linear | [NOUS codebase knowledge base](https://linear.app/goodwiinz/document/nous-codebase-knowledge-base-c4cf26347ad2) |

## How to read these records

* **Current contracts:** `engineering/` describes implementation boundaries and
  the tests or gates that enforce them. Check the closest contract and source
  before relying on a behavior claim.
* **Design and decision evidence:** specifications, plans, architecture references
  and decision records explain intent and rationale at their own baseline.
* **Dated results:** audits, reports, fix records and evaluation bundles retain
  their original revision, environment and results. A Done ticket, checked plan
  or recorded pass is not a new CI or production acceptance result.
* **Archived records:** `archive/` preserves retired planning material. It is
  useful history, rather than current setup or execution guidance.

Repository engineering contracts remain canonical. Linear is a linked project
knowledge and delivery view; it does not replace the code or its checks.

## Browse by subject

| Area | Collections |
| --- | --- |
| Engineering and implementation | [Engineering](engineering/README.md), [code maps](CODEMAPS/README.md), [architecture](architecture/README.md), [API](api/README.md), [supplemental OpenAPI](openapi-specs/README.md), [database](database/README.md) |
| Product and integrations | [Frontend and product design](frontend/README.md), [feature guides](guides/README.md), [comparisons](competitive-analysis/README.md), [A/B testing](ab-testing/README.md) |
| Operations and quality | [Operations](operations/README.md), [runbooks](runbooks/README.md), [deployment](deployment/README.md), [infrastructure](infrastructure/README.md), [security](security/README.md), [performance](performance/README.md), [testing](testing/README.md) |
| Decisions and delivery records | [Decisions](decisions/README.md), [specifications](specs/README.md), [plans](plans/README.md), [Superpowers](superpowers/README.md) |
| Historical and research evidence | [Audits](audits/README.md), [reports](reports/README.md), [fix records](fixes/README.md), [archive](archive/README.md), [research paper](paper/README.md), [evaluations](../evals/README.md) |

## Keeping documentation organized

Add a document to the closest subject folder and update that folder’s index.
Keep current implementation rules in `engineering/`; preserve dated plans and
audit evidence with their original status and source revision. Add a dated
amendment when a conclusion changes instead of rewriting recorded history.

When moving a file, update relative links and repository path references in the
same change. Preserve supporting evidence files with the record they belong to.
Run `make docs-lint` and `git diff --check -- docs/`, then verify changed links.
The [documentation instructions](AGENTS.md) and [directory-doc tooling](../scripts/docs/README.md)
define the complete workflow.
