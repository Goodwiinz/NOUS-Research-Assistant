const DEFAULT_AUTH_REDIRECT = '/dashboard';
const AUTH_REDIRECT_VALIDATION_ORIGIN = 'https://auth-redirect.invalid';

export const AUTH_RETURN_TO_HEADER = 'x-nous-request-path';

export function getSafeAuthRedirect(
  candidate: string | null | undefined,
  origin: string,
  fallback: string = DEFAULT_AUTH_REDIRECT
): string {
  if (!candidate) {
    return fallback;
  }

  try {
    const url = new URL(candidate, origin);

    if (url.origin !== origin) {
      return fallback;
    }

    const path = `${url.pathname}${url.search}${url.hash}`;

    // SECURITY (GOO-402): the origin check above runs on the parsed URL, but
    // WHATWG dot-segment removal can collapse a same-origin input such as
    // `/.//evil.com`, `/%2e//evil.com` or `/a/..//evil.com` to the pathname
    // `//evil.com`. Callers re-resolve the returned string (router.push,
    // `new URL(path, origin)`), which treats a leading `//` (or `/\`) as
    // protocol-relative and leaves the origin. Reject any such result.
    if (/^[/\\]{2}/.test(path)) {
      return fallback;
    }

    return path;
  } catch {
    return fallback;
  }
}

/**
 * Build a login URL that can safely resume an internal protected route.
 *
 * Callers may pass request-derived values, so sanitize before encoding them as
 * `next`. Invalid, external, or absent destinations deliberately omit `next`;
 * the login page then applies its existing `/dashboard` default.
 */
export function getLoginPathWithRedirect(
  candidate: string | null | undefined
): string {
  const destination = getSafeAuthRedirect(
    candidate,
    AUTH_REDIRECT_VALIDATION_ORIGIN,
    ''
  );
  return destination
    ? `/login?next=${encodeURIComponent(destination)}`
    : '/login';
}
