/**
 * Next.js API Route for Health Check
 *
 * This route reports the health of the Next.js frontend itself and, when a
 * backend origin is configured, makes a best-effort probe of the backend
 * `/health` endpoint.
 *
 * On Vercel the frontend runs in a serverless runtime that cannot reach a
 * developer's `localhost:8000` — the previous hardcoded `http://localhost:8000`
 * probe was always meaningless there. We now resolve the backend origin from
 * the same helper as API rewrites (`BACKEND_URL || NEXT_PUBLIC_API_URL`) and
 * skip the probe if configuration is unusable or still points at localhost.
 *
 * The frontend liveness result is decoupled from backend reachability: this
 * route returns 200 whenever the frontend is serving, so an unreachable or
 * unhealthy backend never makes the frontend appear down to uptime checks.
 */

import { resolveBackendUrl } from '../../../config/resolveBackendUrl';

const BACKEND_PROBE_TIMEOUT_MS = 3000;

/** Resolve the backend origin to probe, or null when none is usable. */
function resolveBackendOrigin(): string | null {
  let url: URL;
  try {
    url = new URL(resolveBackendUrl());
  } catch {
    return null;
  }

  // A localhost origin is unreachable from the Vercel runtime — don't probe it.
  const localHosts = new Set(['localhost', '127.0.0.1', '0.0.0.0']);
  if (localHosts.has(url.hostname)) {
    return null;
  }

  return url.origin;
}

export async function GET(): Promise<Response> {
  const frontend = {
    framework: 'Next.js',
    status: 'healthy' as const,
  };

  const backendOrigin = resolveBackendOrigin();

  // No reachable backend configured (e.g. Vercel runtime with no public API
  // origin): report the frontend as alive without a meaningless probe.
  if (!backendOrigin) {
    return Response.json({
      status: 'healthy',
      frontend,
      backend: {
        status: 'unknown',
        detail:
          'No reachable backend origin configured; backend health is not ' +
          'probed from the frontend runtime.',
      },
      timestamp: new Date().toISOString(),
    });
  }

  try {
    const backendResponse = await fetch(`${backendOrigin}/health`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      signal: AbortSignal.timeout(BACKEND_PROBE_TIMEOUT_MS),
    });

    if (!backendResponse.ok) {
      throw new Error(`Backend health check failed: ${backendResponse.status}`);
    }

    const backendHealth = await backendResponse.json();

    return Response.json({
      status: 'healthy',
      frontend,
      backend: backendHealth,
      timestamp: new Date().toISOString(),
    });
  } catch (error) {
    console.error('Backend health probe failed:', error);

    // The frontend is still serving, so it stays 200/healthy — only the
    // backend sub-status reflects the failure.
    return Response.json({
      status: 'healthy',
      frontend,
      backend: {
        status: 'unhealthy',
        error: error instanceof Error ? error.message : 'Unknown error',
      },
      timestamp: new Date().toISOString(),
    });
  }
}
