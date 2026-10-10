import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import {
  projectService,
  type DraftReviewListResponse,
} from '@/services/projectService';

export const draftReviewQueryKey = (projectId: string) =>
  ['project', projectId, 'draft-reviews'] as const;

export function useDraftReviews(
  projectId: string
): UseQueryResult<DraftReviewListResponse, Error> {
  return useQuery({
    queryKey: draftReviewQueryKey(projectId),
    queryFn: () => projectService.listDraftReviews(projectId),
    enabled: Boolean(projectId),
    retry: false,
  });
}
