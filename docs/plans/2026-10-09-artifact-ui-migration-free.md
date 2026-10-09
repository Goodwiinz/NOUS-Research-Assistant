# Migration-free artifact panel completion

Spec: recovered Plan03 `/tmp/rag-linear-audit-20261009.maH9q2/linear-evidence/afda841a-2bc5-4bf0-b947-eeef32ace257.md`, `docs/plans/2026-10-04-harness-plan-amendment.md`, and the parent-approved design recorded in this plan. The original f787a61a7 spec object is unavailable; rulings against that unavailable source are provisional.

## Global constraints

No migration, sharing, native completion, CLI editing, deployment, or remote writes. Root owns browser editing transport and generated contracts. Cherry-pick its reviewed commit before changing HTTP adapters. TanStack Query alone owns artifact metadata and bytes; Zustand owns only transient panel identities/order/pinning/scope and navigation guards. Current auth actor/org, workspace, project, and thread form the scope. All new runtime capabilities default false. Existing document/note/draft/citation/handoff and mobile focus behavior remain usable.

## Task 1: Scoped tabs and content ownership

Interfaces: existing artifactPanelStore facade remains compatible; generated tabs identify exact versions. Scope from canonical auth/chat owners; Query keys include that scope. Queries forward cancellation. Scope changes immediately hide old panels and reset UI before paint. Async responses never open a different scope.

1. Add failing store/component tests for exact-version deduplication, keyboard tabs/close focus, scope reset and stale opens. Add a deferred query scope test.
Expected: new assertions fail before implementation.
2. Implement ordered generated tabs, selection/close, accessible tab strip, scope hook and authenticated Query content owner; preserve existing panel views and late output polling.
Expected: targeted tests and TypeScript pass.
3. Mutation-check stale scope and response guards, restore source, commit explicit paths.
Expected: each mutation fails the corresponding behavioral test.
4. Final task verification: `pnpm --dir frontend test src/store/__tests__/artifactPanelStore.test.ts src/components/chat/artifact-panel/__tests__ src/hooks/chat/__tests__/useThreadArtifacts.test.tsx`.
Expected: exit 0.

## Task 2: Browser editing and bounded static preview

Interfaces: consumes Task 1 scope/content cache and root generated EditArtifactRequest/ArtifactVersionDTO contract. Existing server settings own editing/preview capabilities. POST expected parent/publication/text; 2MiB UTF-8 inclusive; empty text valid.

1. Add failing tests for capability off, UTF-8 bound, empty edit, successful version advancement, unchanged request replay after uncertain failure, conflict buffer preservation/latest identity, and stale scope success suppression. Add bounded CSV/JSON/image/PDF/source preview tests.
Expected: fail for missing behavior.
2. Implement generated adapter, transient editor buffer, frozen request ID/payload across retries, safe errors, explicit reload/copy on conflict without rebasing, dirty navigation confirmation, and version/list invalidation after success. Implement bounded escaped static previews from the single Query blob cache.
Expected: focused tests and TypeScript pass.
3. Mutation-check retry identity, parent conflict and stale completion guards; restore source, commit explicit paths.
Expected: mutations fail.
4. Final task verification: `pnpm --dir frontend test src/components/chat/artifact-panel/__tests__ src/services/__tests__/artifactService.test.ts`.
Expected: exit 0.

## Task 3: Explicit isolated interactive HTML

Interfaces: consumes Task 2 capability and cached bounded HTML source. Interactive preview is opt-in and disabled by default; source remains available.

1. Add failing wrapper/message validation tests and real-browser adversarial cases.
Expected: missing preview isolation fails.
2. Implement opaque outer trusted wrapper with nonce bootstrap and CSP default-src none/frame-src blob, inner Blob iframe sandbox allow-scripts only with first CSP blocking network/forms/base. No same-origin, credential forwarding or RPC. Validate event source, nonce, message type and bounded dimensions. Revoke URLs on teardown and scope/version changes.
Expected: calculator HTML works; browser probes cannot reach parent/cookie/network/popups/top/service worker/self HTTP or blob navigation.
3. Run all targeted tests, type/lint gates, full suite, coverage comparator and real browser proof; request independent final review, address significant findings with red/green tests; commit evidence.
Expected: tests/gates pass or external limitations are explicitly recorded without weaker thresholds.
4. Final task verification: `pnpm --dir frontend test src/components/chat/artifact-panel/__tests__ src/store/__tests__/artifactPanelStore.test.ts src/hooks/chat/__tests__/useThreadArtifacts.test.tsx src/services/__tests__/artifactService.test.ts`.
Expected: exit 0.

## Review focus

Review account/org/workspace/project/thread changes while requests are pending; stale toolbar controls and save completion; duplicate content ownership; retry with edited buffer after ambiguous response; pinned/legacy views; dirty close and mobile focus; CSP bypass through closing-script text, nested frames, blob self-navigation, redirects, and messages from sibling frames. No migration or sharing scope may enter this branch.

## Interactive HTML adaptation

Blob documents inherit their creator's policy ([CSP3](https://www.w3.org/TR/CSP/)). The nonce-only outer policy therefore also constrains embedded HTML. The trusted wrapper parses the bounded source inertly, removes external scripts and inline event attributes, and propagates the trusted application request nonce to embedded script tags. Self-contained scripts using `addEventListener` work; inline handlers, remote assets, npm/React builds, and arbitrary network access remain unsupported, with the escaped source always available.

A second intersecting outer policy permits inline scripts only. It cannot broaden the first nonce-only policy; it blocks external script URLs even when generated code copies its nonce. Both restrictions survive Blob self-navigation. The outer `frame-src blob:` blocks HTTP self-navigation before a network request; Chromium replaces a blocked document with its error page, so the test checks zero outgoing requests rather than expecting the old DOM to remain visible. Both frames retain sandbox `allow-scripts` only. No credentials or RPC enter the frame; only validated source/origin/nonce and bounded height/error/ready messages leave it. Stop, scope/version changes, and panel teardown discard the opaque document and its Blob URL; the wrapper also revokes its URL on `pagehide`.

Security/compatibility tradeoff: nonce propagation preserves embedded script interaction while inline handler semantics are intentionally unsupported rather than rewritten. Browser proof runs with `bypassCSP:false`, including a calculator, forged messages, external scripts copying the nonce, network/API probes, HTTP navigation, and Blob navigation. Interactive execution remains default off through the existing server capability.

Independent review proved that `srcDoc` also inherits the application's nonce policy. The wrapper therefore uses the validated nonce already attached to Next framework scripts, while keeping a distinct random per-mount message identity. Missing or invalid trusted script nonce fails to source fallback. All browser cases now include the exact production parent policy, with a different message identity.
