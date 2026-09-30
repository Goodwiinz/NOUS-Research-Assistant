# Agent supported workflows

Status: Current operational guide, checked 2026-09-26 against the Task 3
implementation based on candidate-parent revision
`9efd2f58248d0d86d70a6fcbbc662f4ed1e90851`.

Agent chat runs one routed branch per user turn: main, research, writing, or
data. The code-owned tool registry and the current branch projection determine
which tools can run. The classifier is not a general workflow planner, and
unsupported combinations are not guaranteed to be recognized for every
wording. The agent does not hand a task automatically from one branch to
another. A user can request a supported follow-up in a separate turn, which is
routed independently.

| Branch | Supported work | Boundary |
| --- | --- | --- |
| Main | General questions and the registered general tools. | Only tools in the current main projection are callable. |
| Research | Find papers and local documents, organize sources, and execute code when requested. | Code execution is confirmation-gated. Research cannot save a writing draft in the same turn. |
| Writing | Resolve local sources, retrieve scoped evidence, summarize or compare documents, and create or revise saved drafts. | Writing cannot execute code. A title-only source request is resolved against accessible local documents before external search or ingestion. |
| Data | Extract document entities and inspect the existing knowledge graph. | `extract_entities` returns extracted names and attributes, not durable graph IDs. Search the knowledge graph and use its returned IDs before graph traversal. |

The Research Engine blueprint editor and its saved workflow runs are separate
from agent chat. Chat tools do not start or resume blueprint runs. Use the
[paper discovery guide](../engineering/paper-discovery.md) for supported
blueprint search configuration.

These boundaries describe the implemented branch and tool contracts. They do
not promise that arbitrary natural-language requests will always be classified
into a particular branch or detected as a cross-branch combination. When a
request needs capabilities from multiple branches, explain the available step
and let the user submit the next step separately; do not claim an automatic
handoff or completion of unavailable work.

The closest implementation sources are the shared [agent prompt rules](../../backend/src/services/agent/_prompts.py), the [tool registry](../../backend/src/services/agent/tool_registry.py), and the [research blueprint API](../../backend/src/api/research_engine/blueprints.py).
