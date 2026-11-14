import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@/lib/supabase/server';
import {
  isTriggerRunId,
  runNotFound,
  triggerUpstreamError,
} from '@/lib/trigger-proxy';

const TRIGGER_API_URL =
  process.env.TRIGGER_API_URL ?? 'https://api.trigger.dev';
const TRIGGER_SECRET_KEY = process.env.TRIGGER_SECRET_KEY;

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ runId: string }> }
) {
  if (!TRIGGER_SECRET_KEY) {
    return NextResponse.json(
      { error: 'Trigger.dev not configured' },
      { status: 503 }
    );
  }

  const supabase = await createClient();
  // getUser() verifies the JWT with Supabase, not just the presence of a
  // (forgeable) session cookie.
  const {
    data: { user },
    error: authError,
  } = await supabase.auth.getUser();
  if (authError || !user) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const { runId } = await params;
  if (!isTriggerRunId(runId)) {
    return runNotFound();
  }

  const res = await fetch(`${TRIGGER_API_URL}/api/v3/runs/${runId}`, {
    headers: {
      Authorization: `Bearer ${TRIGGER_SECRET_KEY}`,
    },
  });

  if (!res.ok) {
    return triggerUpstreamError(res, 'run lookup');
  }

  const run = await res.json();

  // Ownership check (audit #5 IDOR): the executeAgent task stamps
  // metadata.userId with the triggering user (execute-agent.ts:100). Any
  // authenticated user could previously read ANY run's status/output —
  // including the full RAG response and document context — by guessing a
  // runId. Reject runs the caller doesn't own; 404 (not 403) so we don't
  // confirm the run exists. Fail closed when metadata.userId is absent
  // (legacy/foreign runs).
  if (run?.metadata?.userId !== user.id) {
    return NextResponse.json({ error: 'Not found' }, { status: 404 });
  }

  return NextResponse.json({
    runId: run.id,
    status: run.status,
    metadata: run.metadata ?? {},
    output: run.output ?? null,
    error: run.error ?? null,
    createdAt: run.createdAt,
    updatedAt: run.updatedAt,
  });
}
