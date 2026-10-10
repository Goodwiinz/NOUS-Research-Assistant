import { useBackendCapabilities } from '@/hooks/useBackendCapabilities';
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { projectService } from '@/services/projectService';
import type { ApiClaimListResponse } from '@/types/api/research-claims-contract';

export const draftClaimsQueryKey = (projectId: string, draftId: string) =>
  ['project', projectId, 'claims', draftId] as const;

export function useDraftClaims(
  projectId: string,
  draftId: string
): UseQueryResult<ApiClaimListResponse, Error> {
  const capabilities = useBackendCapabilities(Boolean(projectId && draftId));
  return useQuery({
    queryKey: draftClaimsQueryKey(projectId, draftId),
    queryFn: () => projectService.listClaims(projectId, draftId),
    enabled: Boolean(projectId && draftId) && capabilities.draftClaims,
    retry: false,
  });
}
