import { useQuery, useQueryClient } from '@tanstack/react-query';

import { artifactService, type ThreadArtifact } from '@/services/artifactService';

/**
 * Query keys are scoped by thread; the root QueryClient is cleared on
 * sign-out, so a previous account's rows never survive an account switch.
 */
export const threadArtifactsKey = (threadId: string) =>
  ['thread', threadId, 'artifacts'] as const;

export const artifactVersionsKey = (artifactId: string) =>
  ['artifact', artifactId, 'versions'] as const;

/** Late uploads land after the run closes; keep a slow visible-tab poll. */
const LATE_OUTPUT_POLL_MS = 15_000;

export function useThreadArtifacts(threadId: string | null | undefined) {
  return useQuery({
    queryKey: threadArtifactsKey(threadId ?? ''),
    queryFn: () => artifactService.listThreadArtifacts(threadId ?? ''),
    enabled: Boolean(threadId),
    staleTime: 5_000,
    refetchInterval: LATE_OUTPUT_POLL_MS,
    refetchIntervalInBackground: false,
    retry: false,
  });
}

export function useArtifactVersions(artifactId: string | null | undefined) {
  return useQuery({
    queryKey: artifactVersionsKey(artifactId ?? ''),
    queryFn: () => artifactService.listVersions(artifactId ?? ''),
    enabled: Boolean(artifactId),
    staleTime: 5_000,
    retry: false,
  });
}

/** Called from the stream consumer when an `artifact` frame arrives. */
export function useInvalidateThreadArtifacts() {
  const queryClient = useQueryClient();
  return (threadId: string) =>
    queryClient.invalidateQueries({ queryKey: threadArtifactsKey(threadId) });
}

export type { ThreadArtifact };
