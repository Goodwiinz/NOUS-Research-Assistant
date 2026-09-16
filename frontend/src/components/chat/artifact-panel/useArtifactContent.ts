import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import {
  projectService,
  type Draft,
  type DraftCitationsResponse,
  type ProjectNote,
} from '@/services/projectService';

/**
 * Content fetching for note/draft artifacts. Detail keys nest under the
 * plural list keys (['project', id, 'notes'] / ['project', id, 'drafts']) so
 * BOTH the PROJECT_MUTATING_TOOLS invalidation (useChatStreaming /
 * agentChatStore) and useProjectStore's note/draft mutations — which
 * invalidate the plural prefixes — refresh an open artifact, no extra
 * wiring. Tenant scoping is inherited from the project endpoints.
 */
export function useNoteArtifact(
  projectId: string,
  noteId: string
): UseQueryResult<ProjectNote> {
  return useQuery<ProjectNote>({
    queryKey: ['project', projectId, 'notes', noteId],
    queryFn: () => projectService.getNote(projectId, noteId),
  });
}

export function useDraftArtifact(
  projectId: string,
  draftId: string
): UseQueryResult<Draft> {
  return useQuery<Draft>({
    queryKey: ['project', projectId, 'drafts', draftId],
    queryFn: () => projectService.getDraft(projectId, draftId),
  });
}

export function useDraftCitations(
  projectId: string,
  draftId: string
): UseQueryResult<DraftCitationsResponse> {
  return useQuery<DraftCitationsResponse>({
    queryKey: ['project', projectId, 'drafts', draftId, 'citations'],
    queryFn: () => projectService.getDraftCitations(projectId, draftId),
  });
}
