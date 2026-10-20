import { createRequire } from 'node:module';
import { describe, expect, it } from 'vitest';

const require = createRequire(import.meta.url);
const { LOCAL_BACKEND_URL, resolveBackendUrl } =
  require('../../../config/resolveBackendUrl') as {
    LOCAL_BACKEND_URL: string;
    resolveBackendUrl: (env?: NodeJS.ProcessEnv) => string;
  };

describe('resolveBackendUrl', () => {
  it('uses BACKEND_URL when configured', () => {
    expect(
      resolveBackendUrl({
        BACKEND_URL: 'https://api.gen-text.app/',
        VERCEL: '1',
      } as NodeJS.ProcessEnv)
    ).toBe('https://api.gen-text.app');
  });

  it('falls back to NEXT_PUBLIC_API_URL for Vercel builds', () => {
    expect(
      resolveBackendUrl({
        NEXT_PUBLIC_API_URL: 'https://dev-api.gen-text.app',
        VERCEL: '1',
      } as NodeJS.ProcessEnv)
    ).toBe('https://dev-api.gen-text.app');
  });

  it('normalizes versioned public API base URLs to an origin', () => {
    expect(
      resolveBackendUrl({
        NEXT_PUBLIC_API_BASE_URL: 'https://dev-api.gen-text.app/api/v1/',
        VERCEL: '1',
      } as NodeJS.ProcessEnv)
    ).toBe('https://dev-api.gen-text.app');
  });

  it('fails Vercel builds instead of rewriting API traffic to localhost', () => {
    expect(() =>
      resolveBackendUrl({ VERCEL: '1' } as NodeJS.ProcessEnv)
    ).toThrow(/BACKEND_URL or NEXT_PUBLIC_API_URL must be set/);
  });

  it('keeps localhost fallback for local development', () => {
    expect(resolveBackendUrl({} as NodeJS.ProcessEnv)).toBe(LOCAL_BACKEND_URL);
  });

  it('requires an explicit backend for production outside Vercel too', () => {
    expect(() => resolveBackendUrl({ NODE_ENV: 'production' })).toThrow(
      /BACKEND_URL or NEXT_PUBLIC_API_URL must be set/
    );
  });

  it('permits an intentional local backend for a local production build', () => {
    expect(
      resolveBackendUrl({
        NODE_ENV: 'production',
        BACKEND_URL: 'http://127.0.0.1:8443',
      })
    ).toBe('http://127.0.0.1:8443');
  });
});
