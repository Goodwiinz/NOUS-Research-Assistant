import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { projectService } from '@/services/projectService';
import type { ApiReleaseCheck } from '@/types/api/research-release-contract';

export const draftReleaseQueryKey = (
  projectId: string,
  draftId: string,
  version: number
) => ['draft-release', projectId, draftId, version] as const;

/** GOO-307: status, blockers and invalidation for one exact draft version. */
export function useDraftRelease(
  projectId: string | undefined,
  draftId: string,
  version: number
): UseQueryResult<ApiReleaseCheck, Error> {
  return useQuery({
    queryKey: draftReleaseQueryKey(projectId ?? '', draftId, version),
    queryFn: () =>
      projectService.getDraftRelease(projectId ?? '', draftId, version),
    enabled: Boolean(projectId && draftId),
    retry: false,
  });
}
