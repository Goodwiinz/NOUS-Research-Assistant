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

    return `${url.pathname}${url.search}${url.hash}`;
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
