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
