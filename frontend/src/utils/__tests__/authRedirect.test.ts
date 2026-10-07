import { describe, expect, it } from 'vitest';
import {
  getLoginPathWithRedirect,
  getSafeAuthRedirect,
} from '@/utils/authRedirect';

describe('getSafeAuthRedirect', () => {
  it('falls back to dashboard when next points to an external origin', () => {
    expect(
      getSafeAuthRedirect('https://evil.example/phish', 'http://localhost:3000')
    ).toBe('/dashboard');
  });

  it('preserves internal destinations and query strings', () => {
    expect(
      getSafeAuthRedirect('/verify-email?from=signup', 'http://localhost:3000')
    ).toBe('/verify-email?from=signup');
  });

  it('rejects double-slash protocol-relative candidates (//evil.com)', () => {
    expect(getSafeAuthRedirect('//evil.com', 'http://localhost:3000')).toBe(
      '/dashboard'
    );
  });

  it('rejects backslash protocol-relative candidates (/\\evil.com)', () => {
    // WHATWG URL parsing treats `/\` like `//` for special schemes, so this
    // resolves off-origin even though it "starts with a single slash".
    expect(getSafeAuthRedirect('/\\evil.com', 'http://localhost:3000')).toBe(
      '/dashboard'
    );
    expect(getSafeAuthRedirect('/\\/evil.com', 'http://localhost:3000')).toBe(
      '/dashboard'
    );
  });

  it('preserves query string and hash on a same-origin destination', () => {
    expect(
      getSafeAuthRedirect('/dashboard?tab=x#y', 'http://localhost:3000')
    ).toBe('/dashboard?tab=x#y');
  });

  it('encodes a safe internal destination into the login next parameter', () => {
    expect(getLoginPathWithRedirect('/chat?thread=thread-1')).toBe(
      '/login?next=%2Fchat%3Fthread%3Dthread-1'
    );
  });

  it('omits next when the requested post-login destination is external', () => {
    expect(getLoginPathWithRedirect('https://evil.example/phish')).toBe(
      '/login'
    );
    expect(getLoginPathWithRedirect('//evil.example/phish')).toBe('/login');
    expect(getLoginPathWithRedirect('/\\evil.example/phish')).toBe('/login');
  });
});

describe('getSafeAuthRedirect — normalization bypasses (GOO-402)', () => {
  const origin = 'http://localhost:3000';

  // Each payload is a same-origin URL whose normalized pathname collapses to
  // `//host` (or `///host`), which callers would resolve as protocol-relative.
  it.each([
    ['/.//evil.com'],
    ['/%2e//evil.com'],
    ['/a/..//evil.com'],
    ['/..//evil.com'],
    ['/%2e%2e//evil.com'],
    ['/./\\evil.com'],
    ['/.\\/evil.com'],
    ['/.//\\evil.com'],
    ['/x/../\\evil.com'],
    ['/.\t//evil.com'],
  ])('rejects dot-segment payload %j', (candidate) => {
    expect(getSafeAuthRedirect(candidate, origin)).toBe('/dashboard');
  });

  it('rejects an absolute same-origin URL whose path is protocol-relative', () => {
    expect(getSafeAuthRedirect(`${origin}//evil.com`, origin)).toBe(
      '/dashboard'
    );
  });

  it('honours a custom fallback when rejecting', () => {
    expect(getSafeAuthRedirect('/.//evil.com', origin, '/chat')).toBe('/chat');
  });

  it('still preserves path, query and hash for a normal destination', () => {
    expect(getSafeAuthRedirect('/dashboard?x=1#h', origin)).toBe(
      '/dashboard?x=1#h'
    );
  });

  it('still resolves a harmless dot-segment to its normalized internal path', () => {
    expect(getSafeAuthRedirect('/a/../b', origin)).toBe('/b');
  });

  it('keeps percent-encoded slashes literal (not a host)', () => {
    expect(getSafeAuthRedirect('/%2fnot-a-host', origin)).toBe(
      '/%2fnot-a-host'
    );
  });

  it('omits next in the login path for a dot-segment payload', () => {
    expect(getLoginPathWithRedirect('/.//evil.example/phish')).toBe('/login');
    expect(getLoginPathWithRedirect('/%2e//evil.example/phish')).toBe('/login');
  });
});
