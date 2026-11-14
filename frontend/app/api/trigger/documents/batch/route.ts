import { NextRequest, NextResponse } from 'next/server';
import { triggerUpstreamError } from '@/lib/trigger-proxy';
import { createClient } from '@/lib/supabase/server';

const TRIGGER_API_URL =
  process.env.TRIGGER_API_URL ?? 'https://api.trigger.dev';
const TRIGGER_SECRET_KEY = process.env.TRIGGER_SECRET_KEY;

export async function POST(request: NextRequest) {
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

  const body = await request.json();

  if (!Array.isArray(body.documentIds) || body.documentIds.length === 0) {
    return NextResponse.json(
      { error: 'documentIds array is required' },
      { status: 400 }
    );
  }

  const payload = {
    documentIds: body.documentIds,
    priority: body.priority ?? 'normal',
    userId: user.id,
  };

  const res = await fetch(
    `${TRIGGER_API_URL}/api/v1/tasks/batch-process-documents/trigger`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${TRIGGER_SECRET_KEY}`,
      },
      body: JSON.stringify({ payload }),
    }
  );

  if (!res.ok) {
    return triggerUpstreamError(res, 'document batch trigger');
  }

  const data = await res.json();
  return NextResponse.json({ runId: data.id });
}
