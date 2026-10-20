'use client';

import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useAuthStore } from '@/stores/authStore';
import {
  artifactService,
  type SharedArtifactVersion,
} from '@/services/artifactService';
import { previewKindFor } from '@/components/chat/artifact-panel/ArtifactPreview';

/** Share reads are actor/organization-bound, independent of chat selection. */
export function artifactShareScope(): string | null {
  const { user, isAuthenticated } = useAuthStore.getState();
  return isAuthenticated && user?.id && user.organization_id
    ? JSON.stringify([user.id, user.organization_id])
    : null;
}

export function useArtifactShareScope(): string | null {
  const user = useAuthStore((s) => s.user);
  const authenticated = useAuthStore((s) => s.isAuthenticated);
  return authenticated && user?.id && user.organization_id
    ? JSON.stringify([user.id, user.organization_id])
    : null;
}

export const artifactSharesKey = (scope: string | null, artifactId: string) =>
  ['artifact-shares', scope, artifactId] as const;
export const sharedArtifactKey = (scope: string | null, shareId: string) =>
  ['shared-artifact', scope, shareId] as const;

export type SharedArtifactRead =
  | { status: 'ready'; version: SharedArtifactVersion; blob?: Blob }
  | { status: 'unavailable' };

/** One Query owns metadata and bytes; failed rechecks replace private data. */
export function useSharedArtifact(
  scope: string | null,
  shareId: string
): UseQueryResult<SharedArtifactRead> {
  return useQuery({
    queryKey: sharedArtifactKey(scope, shareId),
    queryFn: async ({ signal }): Promise<SharedArtifactRead> => {
      try {
        const version = await artifactService.getArtifactShare(shareId, signal);
        signal.throwIfAborted();
        const blob =
          previewKindFor(version) === 'download'
            ? undefined
            : await artifactService.getSharedArtifactContent(shareId, signal);
        signal.throwIfAborted();
        return { status: 'ready', version, blob };
      } catch {
        signal.throwIfAborted();
        return { status: 'unavailable' };
      }
    },
    enabled: Boolean(scope && shareId),
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: 'always',
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
}
