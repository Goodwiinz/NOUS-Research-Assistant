'use client';

import { useQuery } from '@tanstack/react-query';
import { projectService } from '@/services/projectService';

export interface WorkingFoldersResult {
  documents: Array<{ id: string; title: string | null }> | undefined;
  notes: Array<{ id: string; title: string; isPinned?: boolean }> | undefined;
  drafts: Array<{ id: string; title: string; version?: number }> | undefined;
  isLoading: boolean;
  errors: {
    documents?: Error;
    notes?: Error;
    drafts?: Error;
  };
  refetch: () => void;
}

export function useProjectWorkingFolders(
  projectId: string | undefined
): WorkingFoldersResult {
  const enabled = Boolean(projectId);

  const docsQ = useQuery({
    queryKey: ['project', projectId, 'documents'],
    queryFn: () =>
      projectService.listProjectDocuments(projectId as string, { limit: 100 }),
    enabled,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });

  const notesQ = useQuery({
    queryKey: ['project', projectId, 'notes'],
    queryFn: () =>
      projectService.listProjectNotes(projectId as string, { limit: 100 }),
    enabled,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });

  const draftsQ = useQuery({
    queryKey: ['project', projectId, 'drafts'],
    queryFn: () =>
      projectService.listDrafts(projectId as string, {
        includeContent: false,
        limit: 100,
      }),
    enabled,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });

  // The documents endpoint returns project↔document association rows with
  // the document nested inside ({id: <association>, document_id, document:
  // {title, filename}}). Flatten to the document itself: the id consumers
  // get must be the *document* id (it is what viewers/selection need), and
  // the label falls back to the filename before giving up on a title.
  const documents = docsQ.data?.documents
    ? docsQ.data.documents.map((pd) => ({
        id: pd.document_id ?? pd.document?.id ?? pd.id,
        title: pd.document?.title ?? pd.document?.filename ?? null,
      }))
    : docsQ.isError
      ? []
      : undefined;

  // API responses are snake_case (is_pinned); normalize for the camelCase
  // consumers.
  const notes = notesQ.data?.notes
    ? notesQ.data.notes.map((n) => ({
        id: n.id,
        title: n.title,
        isPinned: n.is_pinned,
      }))
    : notesQ.isError
      ? []
      : undefined;

  const drafts = draftsQ.data?.drafts
    ? draftsQ.data.drafts.map((d) => ({
        id: d.id,
        title: d.title,
        version: d.version,
      }))
    : draftsQ.isError
      ? []
      : undefined;

  return {
    documents,
    notes,
    drafts,
    isLoading: docsQ.isLoading || notesQ.isLoading || draftsQ.isLoading,
    errors: {
      documents: docsQ.error as Error | undefined,
      notes: notesQ.error as Error | undefined,
      drafts: draftsQ.error as Error | undefined,
    },
    refetch: () => {
      void docsQ.refetch();
      void notesQ.refetch();
      void draftsQ.refetch();
    },
  };
}
