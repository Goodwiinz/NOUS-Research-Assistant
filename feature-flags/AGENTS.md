# AGENTS.md

Feature-flag guidance. The repository-root `AGENTS.md` still applies.

## Scope and sources of truth

This directory contains the tracked LaunchDarkly-shaped configuration and
examples. Compare it with the backend flag service at
`backend/src/services/infrastructure/feature_flags.py`, the frontend enum and
defaults in `frontend/src/services/featureFlags.tsx`, and the concrete runtime
registration or test that consumes them. The [invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md)
classifies the root-config relationship as review risk `RR-002`, not as a
confirmed live or dead path.

The tracked root config has no proven production reachability by itself.
Examples are illustrative, provider-managed flags are external state, and
environment-specific ConfigMaps or mock defaults are separate consumers until
their loader is traced. Preserve that distinction when reviewing a change.

## Invalid patterns

- Do not call `feature-flags/launchdarkly-config.json` live, generated, dead,
  or interchangeable from its presence or filename. Prove a runtime consumer
  and registration first.
- Do not change a provider key, local enum value, variation type, default, or
  rollout rule in only one consumer. Backend and frontend expectations must be
  checked together, including safe fallback behavior.
- Do not put SDK keys, credentials, tokens, user data, or other secrets in
  this root or in a feature-flag report/log. Provider access stays behind the
  existing indirect secret boundary.
- Do not promote example configuration to runtime policy, or remove a flag
  because its current consumer is unresolved. Record the evidence and owner
  before changing reachability.

## Required workflow

- Before editing, identify the runtime consumer, registration point, provider
  environment, and fallback path. Reconcile flag keys, enum members, allowed
  values, defaults, and rollout targeting across the backend, frontend, and
  any deployment configuration.
- Resolve and test the relative loader in
  `backend/src/services/infrastructure/feature_flags.py` (currently the
  `../../feature-flags/launchdarkly-config.json` path). Editors must establish
  where that path resolves from the actual module and consumer; do not assume
  it reaches this tracked root.
- Keep mock/development behavior distinct from provider-backed evaluation.
  Add or update focused consumer tests for the real layout before declaring
  the root config live, dead, or safe to replace.
- Keep rollout changes reversible and secret-free. Do not use a flag as a
  substitute for tenant authorization or a migration/rollback gate.

## Verification

There is no root-local automated validator for `feature-flags/`. Until a
runtime consumer is established, verification is an evidence review of the
loader path and provider/config ownership, not a claim that the root file is
loaded. Once a consumer is proven, run its focused key/default/variation tests
and report provider-dependent checks separately when the service is
unavailable.
