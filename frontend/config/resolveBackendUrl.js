const LOCAL_BACKEND_URL = 'http://localhost:8000';
const API_VERSION_PATH_RE = /\/api\/v[0-9]+\/?$/;

const trimTrailingSlash = (value) => value.replace(/\/$/, '');

const normalizeOriginCandidate = (value) => {
  const trimmed = value?.trim();
  if (!trimmed) {
    return '';
  }

  const parsed = new URL(trimmed);
  parsed.pathname = parsed.pathname.replace(API_VERSION_PATH_RE, '') || '/';
  parsed.search = '';
  parsed.hash = '';

  return trimTrailingSlash(parsed.toString());
};

const resolveBackendUrl = (env = process.env) => {
  const configuredBackendUrl = env.BACKEND_URL || env.NEXT_PUBLIC_API_URL;
  if (configuredBackendUrl?.trim()) {
    return normalizeOriginCandidate(configuredBackendUrl);
  }

  if (env.NEXT_PUBLIC_API_BASE_URL?.trim()) {
    return normalizeOriginCandidate(env.NEXT_PUBLIC_API_BASE_URL);
  }

  if (env.VERCEL === '1' || env.NODE_ENV === 'production') {
    throw new Error(
      'BACKEND_URL or NEXT_PUBLIC_API_URL must be set for production builds so API rewrites do not target localhost.'
    );
  }

  return LOCAL_BACKEND_URL;
};

module.exports = {
  LOCAL_BACKEND_URL,
  resolveBackendUrl,
};
