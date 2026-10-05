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
    return await api.get<ApiHandoff>(
      `/api/v2/threads/${encodeURIComponent(threadId)}/handoff`
    );
  } catch (error: unknown) {
    if (isNotFound(error)) return null;
    throw error;
  }
}

export function useThreadHandoff(
  threadId: string | null | undefined
): UseQueryResult<ApiHandoff | null> {
  return useQuery({
    queryKey: threadHandoffKey(threadId ?? ''),
    queryFn: () => fetchThreadHandoff(threadId ?? ''),
    enabled: Boolean(threadId),
    staleTime: 5_000,
    retry: false,
  });
}
