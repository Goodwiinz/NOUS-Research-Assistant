import {
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useCallback } from 'react';

import {
  artifactService,
  type ArtifactVersion,
  type ThreadArtifact,
} from '@/services/artifactService';

/**
 * Query keys are scoped by thread; the root QueryClient is cleared on
 * sign-out, so a previous account's rows never survive an account switch.
 */
export const threadArtifactsKey = (
  threadId: string
): readonly ['thread', string, 'artifacts'] => [
  'thread',
  threadId,
  'artifacts',
];

export const artifactVersionsKey = (
  artifactId: string
): readonly ['artifact', string, 'versions'] => [
  'artifact',
  artifactId,
  'versions',
];

/** Late uploads land after the run closes; keep a slow visible-tab poll. */
const LATE_OUTPUT_POLL_MS = 15_000;

export function useThreadArtifacts(
  threadId: string | null | undefined
): UseQueryResult<ThreadArtifact[]> {
  return useQuery({
    queryKey: threadArtifactsKey(threadId ?? ''),
    queryFn: () => artifactService.listThreadArtifacts(threadId ?? ''),
    enabled: Boolean(threadId),
    staleTime: 5_000,
    // Poll only while something can still change without a stream: a file
    // whose message has not landed yet. Live runs invalidate on the event.
    refetchInterval: (query) =>
      query.state.data?.some((a) => a.reference.messageId === null)
        ? LATE_OUTPUT_POLL_MS
        : false,
    refetchIntervalInBackground: false,
    retry: false,
  });
}

export function useArtifactVersions(
  artifactId: string | null | undefined
): UseQueryResult<ArtifactVersion[]> {
  return useQuery({
    queryKey: artifactVersionsKey(artifactId ?? ''),
    queryFn: () => artifactService.listVersions(artifactId ?? ''),
    enabled: Boolean(artifactId),
    staleTime: 5_000,
    retry: false,
  });
}

/** Called from the stream consumer when an `artifact` frame arrives. */
export function useInvalidateThreadArtifacts(): (
  threadId: string,
  ref?: { artifactId: string }
) => Promise<void> {
  const queryClient = useQueryClient();
  return useCallback(
    async (threadId: string, ref?: { artifactId: string }) => {
      await queryClient.invalidateQueries({
        queryKey: threadArtifactsKey(threadId),
      });
      if (ref) {
        await queryClient.invalidateQueries({
          queryKey: artifactVersionsKey(ref.artifactId),
        });
      }
    },
    [queryClient]
  );
}

export type { ThreadArtifact };
