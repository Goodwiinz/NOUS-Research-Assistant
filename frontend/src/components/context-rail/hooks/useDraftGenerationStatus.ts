'use client';

import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  projectService,
  type GenerationStatus,
} from '@/services/projectService';

const TERMINAL = ['completed', 'failed', 'cancelled'];

export function isTerminal(status: string | undefined): boolean {
  return Boolean(status && TERMINAL.includes(status));
}

/**
 * Poll the project's latest draft-generation task so the rail can show a
 * pending indicator while the agent drafts in the background.
 *
 * The status endpoint 404s when the project has never generated — that is a
 * normal "nothing running" answer, not an error, so retry is off and the
 * error case resolves to `null`.
 */
export function useDraftGenerationStatus(projectId: string | undefined): {
  status: GenerationStatus | null;
  cancel: () => Promise<void>;
} {
  const qc = useQueryClient();

  const q = useQuery({
    queryKey: ['project', projectId, 'draft-status'],
    queryFn: () => projectService.getGenerationStatus(projectId as string),
    enabled: Boolean(projectId),
    retry: false,
    // Poll only while a run is in flight; a terminal status stays put until
    // the next generation is kicked off (which invalidates this key).
    refetchInterval: (query) =>
      isTerminal(query.state.data?.status) ? false : 3000,
  });

  const status = q.data ?? null;
  const finished = isTerminal(status?.status);

  // A finished run means the Drafts folder is stale — refresh it once.
  useEffect(() => {
    if (!projectId || !finished) return;
    void qc.invalidateQueries({ queryKey: ['project', projectId, 'drafts'] });
  }, [projectId, finished, status?.task_id, qc]);

  return {
    status,
    cancel: async () => {
      if (!projectId) return;
      await projectService.cancelGeneration(projectId, status?.task_id);
      await qc.invalidateQueries({
        queryKey: ['project', projectId, 'draft-status'],
      });
    },
  };
}
