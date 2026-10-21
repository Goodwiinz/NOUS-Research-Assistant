import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { api } from '@/services/api-client';
import type { ApiHandoff } from '@/types/api/integration-handoff-contract';

/** Scoped by thread; cleared with the root QueryClient on sign-out. */
export const threadHandoffKey = (
  threadId: string
): readonly ['thread', string, 'handoff'] => ['thread', threadId, 'handoff'];

function isNotFound(error: unknown): boolean {
  const candidate = error as {
    error?: { status_code?: number };
    response?: { status?: number };
  } | null;
  return (
    candidate?.error?.status_code === 404 || candidate?.response?.status === 404
  );
}

/** Latest harness handoff in this chat; null when none was left. */
export async function fetchThreadHandoff(
  threadId: string
): Promise<ApiHandoff | null> {
  try {
    // A readable chat with no handoff yet answers 200 with a null body.
    return await api.get<ApiHandoff | null>(
      `/api/v2/threads/${encodeURIComponent(threadId)}/handoff/latest`
    );
  } catch (error: unknown) {
    // 404: the chat is gone or not readable by this account (servers before
    // HO-3 also answered 404 for "no handoff yet").
    if (isNotFound(error)) return null;
    throw error;
  }
}

/**
 * A standalone CLI save emits no event to this browser, so poll: every 30 s
 * while the chat has a handoff (newer versions), every 5 min while it has
 * none. Focus and a harness `artifact` frame (useInvalidateThreadArtifacts)
 * still refresh at once, and a hidden tab never polls. A push event for
 * handoff saves is the upgrade path if this is too slow or too chatty.
 */
export const HANDOFF_POLL_MS = 30_000;
export const HANDOFF_IDLE_POLL_MS = 5 * 60_000;

export function useThreadHandoff(
  threadId: string | null | undefined
): UseQueryResult<ApiHandoff | null> {
  return useQuery({
    queryKey: threadHandoffKey(threadId ?? ''),
    queryFn: () => fetchThreadHandoff(threadId ?? ''),
    enabled: Boolean(threadId),
    staleTime: 5_000,
    refetchInterval: (query) =>
      query.state.data ? HANDOFF_POLL_MS : HANDOFF_IDLE_POLL_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    retry: false,
  });
}
