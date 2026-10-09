'use client';

import { useLayoutEffect } from 'react';
import { useAuthStore } from '@/stores/authStore';
import { selectCurrentThreadProjectId, useChatStore } from '@/store/chat-store';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';

/** A cache identity, never an authorization decision; the server rechecks access. */
export function useArtifactScope(
  projectOverride?: string | null,
  projectOnly = false
): string | null {
  const user = useAuthStore((s) => s.user);
  const authenticated = useAuthStore((s) => s.isAuthenticated);
  const workspaceId = useChatStore((s) => s.currentWorkspaceId);
  const threadId = useChatStore((s) => s.currentThreadId);
  const projectId = useChatStore(selectCurrentThreadProjectId);
  if (!authenticated || !user?.id || !user.organization_id) return null;
  return JSON.stringify([
    user.id,
    user.organization_id,
    workspaceId,
    projectOverride === undefined ? (projectId ?? null) : projectOverride,
    projectOnly ? null : threadId,
  ]);
}

/** Layout also gates rendering against this identity, preventing an old-scope paint. */
export function useArtifactPanelScope(
  projectId?: string | null
): string | null {
  const scope = useArtifactScope(projectId);
  const setScope = useArtifactPanelStore((s) => s.setScope);
  useLayoutEffect(() => setScope(scope), [scope, setScope]);
  return scope;
}
