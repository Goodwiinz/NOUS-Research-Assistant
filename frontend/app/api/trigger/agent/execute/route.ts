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
  // getUser() verifies the JWT with Supabase; getSession() only reads the
  // (forgeable) cookie. Validate first, then pull the session for the access
  // token we forward downstream.
  const {
    data: { user },
    error: authError,
  } = await supabase.auth.getUser();
  if (authError || !user) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session?.access_token) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
  }

  const body = await request.json();

  const payload = {
    messages: body.messages,
    pageContext: body.page_context,
    model: body.model,
    useRag: body.use_rag,
    maxContextDocs: body.max_context_docs,
    threadId: body.thread_id,
    userId: user.id,
    accessToken: session.access_token,
  };

  const res = await fetch(
    `${TRIGGER_API_URL}/api/v1/tasks/execute-agent/trigger`,
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
    return triggerUpstreamError(res, 'agent run trigger');
  }

  const data = await res.json();
  return NextResponse.json({ runId: data.id });
}
