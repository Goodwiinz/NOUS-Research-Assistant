import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  getPublicApiBaseUrl,
  getPublicApiOrigin,
  getPublicWebSocketOrigin,
} from '@/utils/publicEndpoints';

describe('public endpoint resolution', () => {
  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
  const originalApiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL;
  const originalWsUrl = process.env.NEXT_PUBLIC_WS_URL;
  const originalWebsocketUrl = process.env.NEXT_PUBLIC_WEBSOCKET_URL;
  const originalTestOrigin = (
    globalThis as typeof globalThis & {
      __TEST_BROWSER_ORIGIN__?: string;
    }
  ).__TEST_BROWSER_ORIGIN__;

  const setLocation = (origin: string) => {
    (
      globalThis as typeof globalThis & { __TEST_BROWSER_ORIGIN__?: string }
    ).__TEST_BROWSER_ORIGIN__ = origin;
  };

  const restoreEnv = (key: string, value: string | undefined) => {
    if (value === undefined) {
      delete process.env[key];
      return;
    }

    process.env[key] = value;
  };

  afterEach(() => {
    vi.unstubAllGlobals();
    restoreEnv('NEXT_PUBLIC_API_URL', originalApiUrl);
    restoreEnv('NEXT_PUBLIC_API_BASE_URL', originalApiBaseUrl);
    restoreEnv('NEXT_PUBLIC_WS_URL', originalWsUrl);
    restoreEnv('NEXT_PUBLIC_WEBSOCKET_URL', originalWebsocketUrl);
    (
      globalThis as typeof globalThis & { __TEST_BROWSER_ORIGIN__?: string }
    ).__TEST_BROWSER_ORIGIN__ = originalTestOrigin;
  });

  it('derives the API and WebSocket origins from the dev app hostname', () => {
    delete process.env.NEXT_PUBLIC_API_URL;
    delete process.env.NEXT_PUBLIC_API_BASE_URL;
    delete process.env.NEXT_PUBLIC_WS_URL;
    delete process.env.NEXT_PUBLIC_WEBSOCKET_URL;

    setLocation('https://dev-app.gen-text.app/login');

    expect(getPublicApiOrigin()).toBe('https://dev-api.gen-text.app');
    expect(getPublicApiBaseUrl()).toBe('https://dev-api.gen-text.app/api/v1');
    expect(getPublicWebSocketOrigin()).toBe('wss://dev-api.gen-text.app');
  });

  it('ignores baked localhost public env on remote hosts', () => {
    process.env.NEXT_PUBLIC_API_URL = 'http://localhost:8000';
    process.env.NEXT_PUBLIC_API_BASE_URL = 'http://localhost:8000/api/v1';
    process.env.NEXT_PUBLIC_WS_URL = 'ws://localhost:8000';

    setLocation('https://dev-app.gen-text.app/login');

    expect(getPublicApiOrigin()).toBe('https://dev-api.gen-text.app');
    expect(getPublicApiBaseUrl()).toBe('https://dev-api.gen-text.app/api/v1');
    expect(getPublicWebSocketOrigin()).toBe('wss://dev-api.gen-text.app');
  });

  it.each(['127.0.0.2', 'localhost.', 'preview.localhost'])(
    'ignores loopback configuration for %s on remote pages',
    (hostname) => {
      process.env.NEXT_PUBLIC_API_URL = `http://${hostname}:8000`;
      process.env.NEXT_PUBLIC_API_BASE_URL = `http://${hostname}:8000/api/v1`;
      process.env.NEXT_PUBLIC_WS_URL = `ws://${hostname}:8000`;
      setLocation('https://dev-app.example');
      expect(getPublicApiBaseUrl()).toBe('https://dev-api.example/api/v1');
      expect(getPublicWebSocketOrigin()).toBe('wss://dev-api.example');
    }
  );

  it('keeps localhost defaults for local development', () => {
    delete process.env.NEXT_PUBLIC_API_URL;
    delete process.env.NEXT_PUBLIC_API_BASE_URL;
    delete process.env.NEXT_PUBLIC_WS_URL;
    delete process.env.NEXT_PUBLIC_WEBSOCKET_URL;

    setLocation('http://localhost:3000/login');

    expect(getPublicApiOrigin()).toBe('http://localhost:8000');
    expect(getPublicApiBaseUrl()).toBe('http://localhost:8000/api/v1');
    expect(getPublicWebSocketOrigin()).toBe('ws://localhost:8000');
  });

  it('ignores baked remote public env on localhost', () => {
    process.env.NEXT_PUBLIC_API_URL = 'https://dev-api.gen-text.app';
    process.env.NEXT_PUBLIC_API_BASE_URL =
      'https://dev-api.gen-text.app/api/v1';
    process.env.NEXT_PUBLIC_WS_URL = 'wss://dev-api.gen-text.app';

    setLocation('http://localhost:3000/login');

    expect(getPublicApiOrigin()).toBe('http://localhost:8000');
    expect(getPublicApiBaseUrl()).toBe('http://localhost:8000/api/v1');
    expect(getPublicWebSocketOrigin()).toBe('ws://localhost:8000');
  });

  it('normalizes trailing slashes on same-environment configured values without producing double slashes', () => {
    // Browser + configured env BOTH remote, so `shouldIgnoreConfiguredOrigin`
    // does NOT discard the env (the previous version of this test failed to
    // exercise trimTrailingSlash because cross-env values were thrown away
    // before reaching the concat path).
    process.env.NEXT_PUBLIC_API_URL = 'https://dev-api.gen-text.app/';
    process.env.NEXT_PUBLIC_API_BASE_URL =
      'https://dev-api.gen-text.app/api/v1/';

    setLocation('https://dev-app.gen-text.app/login');

    const origin = getPublicApiOrigin();
    const baseUrl = getPublicApiBaseUrl();

    // Trailing slashes are trimmed on both origin and base.
    expect(origin).toBe('https://dev-api.gen-text.app');
    expect(baseUrl).toBe('https://dev-api.gen-text.app/api/v1');

    // Explicit assertion that no component path contains `//` (the protocol's
    // `://` is allowed, but not `/api//v1` or a trailing `//`).
    const pathOnly = baseUrl.replace(/^[a-z]+:\/\//, '');
    expect(pathOnly).not.toMatch(/\/\//);
  });

  it('preserves local proxy routing for the main API client and local origins for direct callers', () => {
    process.env.NEXT_PUBLIC_API_URL = 'https://dev-api.example';
    process.env.NEXT_PUBLIC_API_BASE_URL = 'https://dev-api.example/api/v1';
    setLocation('http://localhost:3000');
    expect(getPublicApiBaseUrl('/api/v1', { preferLocalProxy: true })).toBe(
      '/api/v1'
    );
    expect(getPublicApiBaseUrl()).toBe('http://localhost:8000/api/v1');
  });

  it('honors a caller-supplied default path when deriving from the browser origin', () => {
    delete process.env.NEXT_PUBLIC_API_URL;
    delete process.env.NEXT_PUBLIC_API_BASE_URL;

    setLocation('http://localhost:3000/login');

    expect(getPublicApiBaseUrl('/api/v2')).toBe('http://localhost:8000/api/v2');
    expect(getPublicApiBaseUrl('/custom')).toBe('http://localhost:8000/custom');
  });

  it('uses explicitly configured public endpoints when they are not localhost', () => {
    process.env.NEXT_PUBLIC_API_URL = 'https://staging-api.gen-text.app';
    process.env.NEXT_PUBLIC_WS_URL = 'wss://staging-api.gen-text.app';

    setLocation('https://dev-app.gen-text.app/login');

    expect(getPublicApiOrigin()).toBe('https://staging-api.gen-text.app');
    expect(getPublicApiBaseUrl()).toBe(
      'https://staging-api.gen-text.app/api/v1'
    );
    expect(getPublicWebSocketOrigin()).toBe('wss://staging-api.gen-text.app');
  });

  it('does not append the API version twice when API_URL is versioned', () => {
    process.env.NEXT_PUBLIC_API_URL = 'https://custom.example/api/v1/';
    delete process.env.NEXT_PUBLIC_API_BASE_URL;
    setLocation('https://app.example');

    expect(getPublicApiOrigin()).toBe('https://custom.example');
    expect(getPublicApiBaseUrl()).toBe('https://custom.example/api/v1');
  });

  it('derives WebSocket routing from an explicitly configured API base', () => {
    delete process.env.NEXT_PUBLIC_API_URL;
    process.env.NEXT_PUBLIC_API_BASE_URL = 'https://custom.example/api/v2/';
    delete process.env.NEXT_PUBLIC_WS_URL;
    delete process.env.NEXT_PUBLIC_WEBSOCKET_URL;
    setLocation('https://app.example');

    expect(getPublicApiOrigin()).toBe('https://custom.example');
    expect(getPublicApiBaseUrl()).toBe('https://custom.example/api/v2');
    expect(getPublicWebSocketOrigin()).toBe('wss://custom.example');
  });

  it('leaves unknown server-side origins unset instead of inventing localhost', () => {
    delete process.env.NEXT_PUBLIC_API_URL;
    delete process.env.NEXT_PUBLIC_API_BASE_URL;
    delete process.env.NEXT_PUBLIC_WS_URL;
    delete process.env.NEXT_PUBLIC_WEBSOCKET_URL;
    delete (
      globalThis as typeof globalThis & { __TEST_BROWSER_ORIGIN__?: string }
    ).__TEST_BROWSER_ORIGIN__;
    vi.stubGlobal('window', undefined);

    expect(getPublicApiOrigin()).toBe('');
    expect(getPublicApiBaseUrl()).toBe('/api/v1');
    expect(getPublicWebSocketOrigin()).toBe('');
  });
});
