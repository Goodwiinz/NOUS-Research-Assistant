import { NextRequest, NextResponse } from 'next/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

const middlewareMocks = vi.hoisted(() => ({
  updateSession: vi.fn(async (request: NextRequest, requestHeaders?: Headers) =>
    NextResponse.next({
      request: { headers: requestHeaders ?? request.headers },
    })
  ),
}));

vi.mock('@/lib/supabase/middleware', () => ({
  updateSession: middlewareMocks.updateSession,
}));

import { proxy } from '../../proxy';

describe('proxy protected-route recovery metadata', () => {
  it('forwards the requested path and query alongside CSP headers', async () => {
    const response = await proxy(
      new NextRequest(
        'https://nous.example/chat?thread=thread-1&panel=sources',
        {
          headers: {
            'x-client-header': 'preserved',
            'x-nous-request-path': '//evil.example/phish',
          },
        }
      )
    );

    expect(
      response.headers.get('x-middleware-request-x-nous-request-path')
    ).toBe('/chat?thread=thread-1&panel=sources');
    expect(response.headers.get('x-middleware-request-x-client-header')).toBe(
      'preserved'
    );
    expect(response.headers.get('x-middleware-request-x-nonce')).toBeTruthy();
    expect(response.headers.get('content-security-policy')).toContain(
      "script-src 'self' 'nonce-"
    );
  });
});

describe('proxy CSP connect-src', () => {
  const connectSrc = async (): Promise<string> => {
    const response = await proxy(new NextRequest('http://localhost:3000/chat'));
    const csp = response.headers.get('content-security-policy') ?? '';
    return csp.split('; ').find((d) => d.startsWith('connect-src')) ?? '';
  };

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('allows the local backend on :8000 in development', async () => {
    vi.stubEnv('NODE_ENV', 'development');
    const directive = await connectSrc();
    expect(directive).toContain('http://localhost:8000');
    expect(directive).toContain('ws://localhost:8000');
    expect(directive).toContain('http://127.0.0.1:8000');
    expect(directive).toContain('ws://127.0.0.1:8000');
  });

  it('keeps production connect-src free of plain-http origins', async () => {
    vi.stubEnv('NODE_ENV', 'production');
    expect(await connectSrc()).toBe("connect-src 'self' https: wss:");
  });
});
