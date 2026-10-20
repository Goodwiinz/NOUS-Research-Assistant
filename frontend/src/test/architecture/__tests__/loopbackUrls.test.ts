// @vitest-environment node
import { Linter } from 'eslint';
import { createRequire } from 'node:module';
import { describe, expect, it } from 'vitest';
const require = createRequire(import.meta.url);
const rule =
  require('../../../../eslint-rules/no-loopback-runtime-urls.mjs').default;

const lint = (code: string): ReturnType<Linter['verify']> =>
  new Linter().verify(code, [
    {
      plugins: { nous: { rules: { 'no-loopback-runtime-urls': rule } } },
      rules: { 'nous/no-loopback-runtime-urls': 'error' },
    },
  ]);

describe('loopback runtime URL guard', () => {
  it.each([
    "const url = 'http://localhost:8000';",
    "const url = 'ws://127.0.0.1:8000/ws';",
    "const url = 'https://[::1]:8443';",
    'const url = `http://localhost:${port}`;',
    "const url = 'http://localhost?query=1';",
    "const url = 'http://test@localhost:8000';",
  ])('rejects a runtime loopback URL: %s', (code) => {
    expect(lint(code)).toEqual([
      expect.objectContaining({
        ruleId: 'nous/no-loopback-runtime-urls',
        severity: 2,
      }),
    ]);
  });

  it.each([
    '// http://localhost:8000 is an example in a comment\nconst url = getPublicApiOrigin();',
    "const url = 'https://api.example';",
    "const url = '/api/v1';",
    "const url = 'https://localhost.example';",
  ])('allows comments, configured origins and relative routing: %s', (code) => {
    expect(lint(code)).toEqual([]);
  });
});
