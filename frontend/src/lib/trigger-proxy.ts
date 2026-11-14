import { NextResponse } from 'next/server';

/**
 * Trigger.dev v3 run ids are `run_` followed by an alphanumeric id. The
 * proxy routes interpolate the id into a Trigger API path that is sent with
 * TRIGGER_SECRET_KEY, so anything else (e.g. an encoded `../`) is rejected
 * before a request is made.
 */
const TRIGGER_RUN_ID = /^run_[a-z0-9]{1,64}$/i;

export function isTriggerRunId(value: unknown): value is string {
  return typeof value === 'string' && TRIGGER_RUN_ID.test(value);
}

export function runNotFound(): NextResponse {
  return NextResponse.json({ error: 'Not found' }, { status: 404 });
}

/**
 * Stable client response for a failed Trigger.dev call. The upstream body can
 * carry internal detail, so it is logged server-side (truncated) and never
 * returned. An upstream 404 reads like any other missing or foreign run.
 */
export async function triggerUpstreamError(
  res: Response,
  operation: string
): Promise<NextResponse> {
  if (res.status === 404) {
    return runNotFound();
  }
  let detail = '';
  try {
    detail = (await res.text()).slice(0, 500);
  } catch {
    detail = '<unreadable body>';
  }
  console.error(
    `Trigger.dev ${operation} failed with status ${res.status}: ${detail}`
  );
  return NextResponse.json(
    { error: 'Agent run service request failed' },
    { status: 502 }
  );
}
