# Mocked project identity browser check

This opt-in check exercises the real Next.js UI with mocked Supabase and API
responses. It does not represent live backend acceptance.

```bash
node e2e/nous-flows/mock-project-identity-harness.mjs
MOCK_PROJECT_IDENTITY_E2E=1 BASE_URL=http://localhost:3040 pnpm exec playwright test e2e/nous-flows/research-project-identity.spec.ts --project=chromium --config=playwright.nous.config.ts
```
