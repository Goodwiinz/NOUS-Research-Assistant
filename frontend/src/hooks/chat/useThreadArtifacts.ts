import {
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useCallback, useEffect, useRef } from 'react';

import {
  artifactService,
  type ArtifactVersion,
  type ProjectArtifact,
  type ThreadArtifact,
} from '@/services/artifactService';
import { threadHandoffKey } from '@/hooks/chat/useThreadHandoff';
import { useChatStore } from '@/store/chat-store';
import { useArtifactScope } from './useArtifactScope';

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

export const projectArtifactsKey = (
  projectId: string
): readonly ['project', string, 'artifacts'] => [
  'project',
  projectId,
  'artifacts',
];

/** Late uploads land after the run closes; keep a slow visible-tab poll. */
const LATE_OUTPUT_POLL_MS = 15_000;
/**
 * A publication that lands after the run's terminal event gets no SSE
 * announcement (the lifecycle drain skips closed runs), so keep polling for
 * a while after this thread's stream ends. Rows alone cannot tell us whether
 * more output is coming.
 */
const LATE_OUTPUT_WINDOW_MS = 5 * 60_000;

export function useThreadArtifacts(
  threadId: string | null | undefined
): UseQueryResult<ThreadArtifact[]> {
  const scope = useArtifactScope();
  const streamingHere = useChatStore(
    (s) => s.isStreaming && s.streamingThreadId === threadId
  );
  const streamEndedAt = useRef<number | null>(null);
  const wasStreaming = useRef(false);
  useEffect(() => {
    if (wasStreaming.current && !streamingHere)
      streamEndedAt.current = Date.now();
    wasStreaming.current = streamingHere;
  }, [streamingHere]);
  return useQuery({
    queryKey: [...threadArtifactsKey(threadId ?? ''), scope],
    queryFn: ({ signal }) =>
      artifactService.listThreadArtifacts(threadId ?? '', signal),
    enabled: Boolean(threadId && scope),
    staleTime: 5_000,
    // Poll while output can still arrive without a stream frame: the run is
    // live (frames invalidate too, but a suppressed announcement is cheap to
    // cover), it ended recently, or a run file's message has not landed yet.
    refetchInterval: (query) => {
      const endedAt = streamEndedAt.current;
      const recentlyEnded =
        endedAt !== null && Date.now() - endedAt < LATE_OUTPUT_WINDOW_MS;
      // Only run output awaits a message; standalone rows (no run) never get one.
      const pending = query.state.data?.some(
        (a) => a.reference.messageId === null && a.reference.runId !== null
      );
      return streamingHere || recentlyEnded || pending
        ? LATE_OUTPUT_POLL_MS
        : false;
    },
    refetchIntervalInBackground: false,
    retry: false,
  });
}

export function useProjectArtifacts(
  projectId: string | null | undefined
): UseQueryResult<ProjectArtifact[]> {
  const scope = useArtifactScope(projectId ?? null, true);
  return useQuery({
    queryKey: [...projectArtifactsKey(projectId ?? ''), scope],
    queryFn: ({ signal }) =>
      artifactService.listProjectArtifacts(projectId ?? '', signal),
    enabled: Boolean(projectId && scope),
    staleTime: 5_000,
    retry: false,
  });
}

export function useArtifactVersions(
  artifactId: string | null | undefined
): UseQueryResult<ArtifactVersion[]> {
  const scope = useArtifactScope();
  return useQuery({
    queryKey: [...artifactVersionsKey(artifactId ?? ''), scope],
    queryFn: ({ signal }) =>
      artifactService.listVersions(artifactId ?? '', signal),
    enabled: Boolean(artifactId && scope),
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
      // A harness publishes its results, then saves the handoff naming them.
      await queryClient.invalidateQueries({
        queryKey: threadHandoffKey(threadId),
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

/** The only cache for authenticated version bytes; previews/editors share it. */
export function useArtifactContent(
  versionId: string,
  enabled = true
): UseQueryResult<Blob> {
  const scope = useArtifactScope();
  return useQuery({
    queryKey: ['artifact-content', scope, versionId],
    queryFn: ({ signal }) =>
      artifactService.fetchVersionBlob(versionId, signal),
    enabled: Boolean(scope && versionId && enabled),
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  });
}
