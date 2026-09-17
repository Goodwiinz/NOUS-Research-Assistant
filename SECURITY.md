# Security Policy

This policy provides repository-specific context for security review of NOUS. It does not replace authentication, authorization, deployment, or incident-response procedures. Repository ownership follows `.github/CODEOWNERS`.

## System and Scope

NOUS is a multi-user AI research workspace. Next.js web and external CLI clients call a FastAPI backend over HTTP, server-sent events, and WebSockets. The backend runs LangGraph agents and workers and connects to PostgreSQL, Redis, Neo4j, object storage, model and retrieval providers, external research connectors, and an optional E2B code-execution sandbox.

This policy covers first-party application code, APIs, agent and tool orchestration, background jobs, storage and database code, migrations, deployment configuration, and client code in this repository. Deployment and feature exposure vary by environment; unless current deployment evidence proves otherwise, review HTTP, streaming, WebSocket, upload, connector, and agent surfaces as potentially reachable from an untrusted network.

Security-sensitive assets include credentials and signing keys; private documents and derived text, embeddings, entities, citations, and figures; organization, workspace, project, conversation, memory, and agent state; provider data and credentials; generated drafts and exports; audit and observability data; and infrastructure control planes.

## Threat Model and Trust Boundaries

Assume unauthenticated clients and authenticated users may be malicious. Treat request fields, identifiers, uploaded files, document text and metadata, prompts, retrieved passages, project skill documents, model output, tool arguments, connector responses, and filenames or URLs from external services as attacker-controlled.

Important boundaries are:

- clients to the FastAPI authentication, authorization, rate-limit, and tenant-context layers;
- authenticated identity to organization-, user-, project-, and workspace-scoped records;
- public, member, and owner workspace access to nested conversations, threads, messages, and collections;
- the model and retrieved content to the server-owned tool registry and execution policy;
- an approval interrupt to any classified destructive tool execution;
- application and worker processes to databases, caches, object storage, model providers, research connectors, and observability systems; and
- agent-authored code to the E2B sandbox, including package installation, network access, filesystem state, and sandbox lifetime.

Identity and authorization context must come from verified credentials and server-side records, never from model output or client-supplied organization, user, role, workspace, project, or thread fields alone.

Workspace access is intentionally membership-based: a non-deleted workspace and its nested resources are accessible to its owner, an explicit member, or anyone when the workspace is public. Public workspaces may therefore be visible across organizations. This does not relax organization scoping for documents, attachments, searches, citations, knowledge-graph data, memories, or other tenant-owned assets.

## Security Invariants

- Protected HTTP, SSE, WebSocket, CLI, worker, and tool paths authenticate the caller and fail closed before reading or mutating protected state. Token signature, expiry, type, and configured issuer or audience constraints must be enforced where applicable. Tokens must not be accepted from URL query strings when a header or protocol channel is required.
- Every access to tenant-owned data is scoped by authoritative server-side identity. Knowing or guessing an object ID is not authorization. Cross-tenant denials and errors must not reveal another tenant's identifiers, titles, filenames, counts, or existence.
- Workspace authorization uses the public/member/owner rule above and checks the complete non-deleted parent chain. Document access remains organization-scoped even when a document is referenced by a workspace, project, attachment, citation, cache entry, search result, or agent tool. A user role such as administrator is not by itself a tenant boundary bypass.
- Role, permission, API-key, and organization-management operations are authorized within the relevant organization. Client claims and request fields cannot grant roles, widen API-key scope, or select another tenant.
- Prompts, retrieved content, model output, and project skills are data, not authority. They cannot change identity, tool policy, approval requirements, network policy, or system instructions. Executable tool metadata and policy tags remain server-owned.
- Tools classified as destructive, including persistent mutations, ingestion, and code execution, raise approval before the first side effect. Rejection or cancellation causes no mutation. Approval is bound to the displayed tool and arguments, executes at most once, and remains safe under retries, replay, disconnect, and resume.
- Code execution occurs only in the configured sandbox after approval. Sandboxes are isolated by authorized user/thread context, cannot access application hosts, internal services, tenant data, or platform credentials except through an explicitly designed capability, and have bounded runtime, output, packages, network use, and lifetime.
- Upload and parsing paths enforce bounded size and work, allowed formats, safe object keys and filenames, and tenant-aware storage access. They prevent traversal, unsafe active content, parser-driven command execution, and cross-tenant deduplication leaks. Storage and quota changes are compensated when later persistence fails.
- Outbound fetches and connectors validate destinations and redirects, do not expose internal networks or cloud metadata, use bounded timeouts and result sizes, and do not leak provider credentials. External responses remain untrusted when cached, rendered, indexed, or supplied to a model.
- Queries use parameterized statements and validated sort/filter fields. Cache, checkpoint, task, deduplication, and object-storage keys preserve the same tenant and user boundaries as the underlying records.
- Secrets, tokens, raw private content, and sensitive tool arguments do not appear in client errors, logs, traces, analytics, or model prompts beyond the minimum explicitly required. Production secrets are supplied out of band and are never replaced by development defaults.
- User-controlled and model-generated content is safely rendered in the web client and exports. Citations and model confidence are provenance features, not authorization decisions or proof that content is safe.
- Security-relevant failures return bounded, non-sensitive errors. A dependency outage or optional-control failure must not silently broaden data access, tool authority, sandbox privilege, or tenant scope.

## Reportable Findings and Severity Context

A finding is reportable when a realistic path from attacker-controlled input or a compromised lower-trust component violates an invariant and affects confidentiality, integrity, availability, authorization, auditability, or approval guarantees. Demonstrate reachability under a supported or plausibly deployed configuration and distinguish observed behavior from an unverified control or test expectation.

Severity should reflect the strongest realistic impact:

- **Critical:** unauthenticated arbitrary code execution outside the sandbox, sandbox escape into application or cloud control planes, broad credential compromise, systemic cross-tenant disclosure, or an approval/authentication bypass enabling widespread destructive action.
- **High:** targeted access to another tenant's private content, meaningful authentication or organization-scoped authorization bypass, persistent script execution in another user's session, exploitable access to internal services or credentials, or a protected mutation/code execution occurring without valid approval.
- **Medium:** limited sensitive disclosure, scoped integrity violations, repeatable remote resource exhaustion with material service impact, or a security-control weakness requiring significant preconditions.
- **Low:** defense-in-depth weaknesses with concrete security relevance but minimal direct impact.

Hallucinations, weak retrieval, unsupported citations, cosmetic issues, and ordinary correctness bugs are not security findings unless they cross a security boundary, expose protected data, cause an unauthorized side effect, or materially defeat a security control. A vulnerable dependency or dormant feature is reportable when first-party configuration, integration, or deployment makes the vulnerable behavior realistically reachable.

## Out of Scope, Exclusions, and Accepted Risk

No repository component or vulnerability class has an owner-confirmed blanket exclusion, and no security weakness is accepted by this policy. Generated files, dependency copies, local worktrees, evaluation fixtures, historical audits, and stale deployment examples are not primary implementation sources, but they do not suppress a finding when they establish a reachable first-party dependency, configuration, packaging, secret-exposure, or deployment risk.

Localhost-only development defaults are not by themselves production vulnerabilities. Any path that permits those defaults in a shared or deployed environment is reportable. Do not treat comments, tests, documentation, prior audit status, feature flags, or the presence of a compensating control as proof that an issue is unreachable or accepted.

## Known Limitations and Compensating Controls

- Current deployment notes identify only the development ArgoCD environment as live and describe some retained manifests and retrieval integrations as stale or disabled. Verify current deployment evidence before changing severity; absence from one environment is not a permanent exclusion.
- Some revocation and rate-limit behavior degrades or fails open when Redis is unavailable, and rate-limit state may fall back to process-local memory. Treat the outage requirement as a precondition, not as suppression or accepted risk.
- Field-level encryption initialization is non-fatal and legacy plaintext rows may exist. Production configuration validation and external storage encryption are compensating context, not substitutes for testing confidentiality and key-handling paths.
- Code execution, external connectors, model providers, observability, and retrieval backends are configuration-dependent. When enabled, their credential, network, isolation, and data-retention boundaries are in scope.
- Security tests and deterministic agent evaluations document intended controls such as tenant isolation and approval idempotency. They are useful regression evidence but do not prove the control works on every route or deployment.

When evidence conflicts with this policy, report the conflict rather than silently weakening an invariant. Changes that add exclusions, accept risk, or alter tenant, workspace, approval, or sandbox boundaries require explicit owner review.
