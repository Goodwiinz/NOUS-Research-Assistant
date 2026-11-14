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

export async function POST(
  request: NextRequest,
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
  const body = await request.json();
  const { tokenId, confirmed } = body;

  if (!tokenId || typeof confirmed !== 'boolean') {
    return NextResponse.json(
      { error: 'tokenId and confirmed are required' },
      { status: 400 }
    );
  }

  // Ownership check (audit #6 HITL hijack): previously the runId path param
  // was ignored (`_runId`) and ANY authenticated user could complete ANY
  // pending HITL waitpoint with a known tokenId — auto-approving a victim's
  // document ingest / note creation. Verify against the run's metadata
  // (set by execute-agent.ts): the caller must OWN the run (metadata.userId)
  // AND the supplied tokenId must be the run's CURRENT pending token
  // (metadata.waitTokenId). The second check prevents pairing an
  // attacker-owned runId with a victim's tokenId. Both fail closed.
  const runRes = await fetch(`${TRIGGER_API_URL}/api/v3/runs/${runId}`, {
    headers: { Authorization: `Bearer ${TRIGGER_SECRET_KEY}` },
  });
  if (!runRes.ok) {
    return NextResponse.json({ error: 'Not found' }, { status: 404 });
  }
  const run = await runRes.json();
  if (run?.metadata?.userId !== user.id) {
    return NextResponse.json({ error: 'Not found' }, { status: 404 });
  }
  if (!run?.metadata?.waitTokenId || run.metadata.waitTokenId !== tokenId) {
    return NextResponse.json(
      { error: 'Token does not belong to this run' },
      { status: 403 }
    );
  }

  const res = await fetch(
    `${TRIGGER_API_URL}/api/v1/waitpoints/tokens/${tokenId}/complete`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${TRIGGER_SECRET_KEY}`,
      },
      body: JSON.stringify({ output: { confirmed } }),
    }
  );

  if (!res.ok) {
    return triggerUpstreamError(res, 'waitpoint completion');
  }

  return NextResponse.json({ status: 'confirmed' });
}
