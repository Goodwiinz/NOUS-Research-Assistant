const LOCAL_HOSTNAMES = new Set(['localhost', '127.0.0.1', '0.0.0.0', '[::1]']);
const API_VERSION_PATH_RE = /\/api\/v[0-9]+\/?$/;

type GlobalWithTestOrigin = typeof globalThis & {
  __TEST_BROWSER_ORIGIN__?: string;
};

const trimTrailingSlash = (value: string): string => value.replace(/\/+$/, '');

const isLocalHostname = (hostname: string): boolean => {
  const normalized = hostname.toLowerCase().replace(/\.$/, '');
  return (
    LOCAL_HOSTNAMES.has(normalized) ||
    /^127(?:\.\d{1,3}){3}$/.test(normalized) ||
    normalized.endsWith('.localhost')
  );
};

const parseUrl = (value: string): URL | null => {
  try {
    return new URL(value);
  } catch {
    return null;
  }
};

const getBrowserLocation = (): URL | null => {
  const testOrigin = (globalThis as GlobalWithTestOrigin)
    .__TEST_BROWSER_ORIGIN__;
  if (testOrigin) {
    return parseUrl(testOrigin);
  }

  if (typeof window === 'undefined') {
    return null;
  }

  return parseUrl(window.location.origin);
};

export const shouldUseLocalProxyForConfiguredOrigin = (
  value: string
): boolean => {
  const configured = parseUrl(value);
  const browserLocation = getBrowserLocation();

  if (!configured || !browserLocation) {
    return false;
  }

  return (
    isLocalHostname(browserLocation.hostname) &&
    !isLocalHostname(configured.hostname)
  );
};

const shouldIgnoreConfiguredOrigin = (value: string): boolean => {
  const configured = parseUrl(value);
  const browserLocation = getBrowserLocation();

  if (!configured || !browserLocation) {
    return false;
  }

  const configuredIsLocal = isLocalHostname(configured.hostname);
  const browserIsLocal = isLocalHostname(browserLocation.hostname);

  // Prevent cross-environment leakage from baked NEXT_PUBLIC_* values.
  // Localhost UI should not call remote hosts by default, and remote UIs
  // should not call localhost.
  return configuredIsLocal !== browserIsLocal;
};

const mapFrontendHostToApiHost = (hostname: string): string => {
  if (hostname.startsWith('dev-app.')) {
    return hostname.replace(/^dev-app\./, 'dev-api.');
  }

  if (hostname.startsWith('staging-app.')) {
    return hostname.replace(/^staging-app\./, 'staging-api.');
  }

  if (hostname.startsWith('app.')) {
    return hostname.replace(/^app\./, 'api.');
  }

  return hostname;
};

const getConfiguredOrigin = (
  values: Array<string | undefined>,
  protocols: readonly string[] = ['http:', 'https:']
): string => {
  for (const value of values) {
    const trimmed = value?.trim();
    if (!trimmed || shouldIgnoreConfiguredOrigin(trimmed)) {
      continue;
    }

    if (trimmed.startsWith('/') && !trimmed.startsWith('//')) {
      return trimTrailingSlash(trimmed);
    }
    const parsed = parseUrl(trimmed);
    if (!parsed || !protocols.includes(parsed.protocol)) {
      continue;
    }

    return trimTrailingSlash(trimmed);
  }

  return '';
};

export const getPublicApiOrigin = (): string => {
  const configured = getConfiguredOrigin([
    process.env.NEXT_PUBLIC_API_URL,
    process.env.NEXT_PUBLIC_API_BASE_URL,
  ]);
  if (configured && !configured.startsWith('/')) {
    return configured.replace(API_VERSION_PATH_RE, '');
  }

  const browserLocation = getBrowserLocation();
  if (!browserLocation) {
    return '';
  }

  if (isLocalHostname(browserLocation.hostname)) {
    return 'http://localhost:8000';
  }

  const apiHost = mapFrontendHostToApiHost(browserLocation.hostname);
  if (apiHost !== browserLocation.hostname) {
    return `${browserLocation.protocol}//${apiHost}`;
  }

  return browserLocation.origin;
};

export const getPublicApiBaseUrl = (
  defaultPath: string = '/api/v1',
  options: { preferLocalProxy?: boolean } = {}
): string => {
  // The main API client uses Next's proxy when a local UI is configured
  // with a remote backend. Direct streaming callers keep their local default.
  if (options.preferLocalProxy) {
    const rawConfigured = [
      process.env.NEXT_PUBLIC_API_BASE_URL,
      process.env.NEXT_PUBLIC_API_URL,
    ].find((value) => value?.trim());
    if (
      rawConfigured &&
      shouldUseLocalProxyForConfiguredOrigin(rawConfigured.trim())
    ) {
      return trimTrailingSlash(defaultPath);
    }
  }
  const configuredBaseUrl = getConfiguredOrigin([
    process.env.NEXT_PUBLIC_API_BASE_URL,
    process.env.NEXT_PUBLIC_API_URL,
  ]);
  if (configuredBaseUrl) {
    if (
      configuredBaseUrl.startsWith('/') ||
      API_VERSION_PATH_RE.test(configuredBaseUrl)
    ) {
      return configuredBaseUrl;
    }

    return `${configuredBaseUrl}${defaultPath}`;
  }

  const origin = getPublicApiOrigin();
  return origin ? `${origin}${defaultPath}` : defaultPath;
};

export const getPublicWebSocketOrigin = (): string => {
  const configured = getConfiguredOrigin(
    [process.env.NEXT_PUBLIC_WS_URL, process.env.NEXT_PUBLIC_WEBSOCKET_URL],
    ['ws:', 'wss:', 'http:', 'https:']
  );
  if (configured) {
    return configured.replace(/^http/, 'ws');
  }

  const apiOrigin = getPublicApiOrigin();
  if (apiOrigin) {
    return apiOrigin.replace(/^http/, 'ws');
  }

  return '';
};
