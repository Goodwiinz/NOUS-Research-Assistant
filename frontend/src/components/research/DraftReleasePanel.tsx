'use client';

/**
 * GOO-307 release status for one exact draft version: a badge, the stale
 * cause, and (for an adjudicator or supervisor) the blocker list and the
 * "Promote to verified" action. `is_current` and a passed review never mean
 * verified; only a promotion does. GOO-316's statements and GOO-315's
 * manuscript releases sit below.
 */

import React from 'react';
import { useBackendCapabilities } from '@/hooks/useBackendCapabilities';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ShieldCheck } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth';
import { draftReleaseQueryKey, useDraftRelease } from '@/hooks/useDraftRelease';
import { listProjectRoles } from '@/services/researchEngineService';
import { ManuscriptReleaseSection } from './ManuscriptReleaseSection';
import { ManuscriptStatementsPanel } from './ManuscriptStatementsPanel';
import { projectService, type Draft } from '@/services/projectService';
import { APIErrorClass } from '@/types/api';
import type {
  ApiReleaseBlocker,
  ApiReleaseStatus,
} from '@/types/api/research-release-contract';

const STATUS: Record<ApiReleaseStatus, { label: string; tone: string }> = {
  candidate: { label: 'Candidate', tone: 'bg-muted text-muted-foreground' },
  verified: { label: 'Verified', tone: 'bg-primary/10 text-primary' },
  stale: { label: 'Stale', tone: 'bg-destructive/10 text-destructive' },
};

const CODE_LABEL: Record<string, string> = {
  unclaimed_assertion: 'Unclaimed sentence',
  unassessed: 'Unassessed',
  model_only: 'Model stance only',
  opposed: 'Opposed',
  unresolved: 'Unresolved',
  legacy_only: 'Legacy citation only',
  superseded_assessment: 'Superseded assessment',
  stale_evidence: 'Stale evidence',
  unattributed_interpretation: 'Unattributed interpretation',
  identity_mismatch: 'Identifier mismatch',
  retracted_source: 'Retracted source',
};

export const ReleaseBadge: React.FC<{ status: ApiReleaseStatus }> = ({
  status,
}) => (
  <span
    aria-label={`Release status: ${STATUS[status].label}`}
    className={`ml-2 px-1.5 py-0.5 rounded text-xs ${STATUS[status].tone}`}
  >
    {STATUS[status].label}
  </span>
);

/** The 409 `release_blocked` body carries the blockers under the envelope. */
function blockersFrom(error: unknown): ApiReleaseBlocker[] | null {
  if (!(error instanceof APIErrorClass)) return null;
  const envelope = error.error.details?.error as
    { message?: { code?: string; blockers?: ApiReleaseBlocker[] } } | undefined;
  const detail = envelope?.message;
  return detail?.code === 'release_blocked' && Array.isArray(detail.blockers)
    ? detail.blockers
    : null;
}

interface DraftReleasePanelProps {
  projectId: string;
  draft: Draft;
}

export const DraftReleasePanel: React.FC<DraftReleasePanelProps> = ({
  projectId,
  draft,
}) => {
  const capabilities = useBackendCapabilities();
  const userId = useAuth().user?.id;
  const queryClient = useQueryClient();
  const release = useDraftRelease(projectId, draft.id, draft.version);
  const { data: roles } = useQuery({
    enabled: capabilities.draftRelease,
    queryKey: ['project', projectId, 'research-engine', 'roles'],
    queryFn: () => listProjectRoles(projectId),
    retry: false,
  });
  const canPromote = (roles ?? []).some(
    (r) =>
      r.user_id === userId &&
      (r.role === 'adjudicator' || r.role === 'supervisor')
  );
  const promote = useMutation({
    mutationFn: () =>
      projectService.promoteDraft(projectId, draft.id, draft.version, {
        content_hash: draft.content_hash ?? release.data?.content_hash ?? '',
        idempotency_key: crypto.randomUUID(),
      }),
    onSettled: () =>
      queryClient.invalidateQueries({
        queryKey: draftReleaseQueryKey(projectId, draft.id, draft.version),
      }),
  });

  const status = release.data?.release_status;
  const cause = release.data?.invalidation?.cause;
  const blockers = blockersFrom(promote.error) ?? release.data?.blockers ?? [];

  if (!capabilities.draftRelease) return null;

  return (
    <div className="px-4 py-2 border-b border-border space-y-2">
      {status === 'stale' && cause && (
        <p role="status" className="text-xs text-destructive">
          Stale — invalidated by {String(cause.family ?? 'an upstream change')}
          {cause.kind ? ` (${String(cause.kind)})` : ''}. Re-assess and promote
          again.
        </p>
      )}
      {canPromote && status !== 'verified' && (
        <div className="space-y-2">
          {blockers.length > 0 && (
            <ul aria-label="Release blockers" className="space-y-1 text-xs">
              {blockers.map((b) => (
                <li
                  key={`${b.code}-${b.start}-${b.claim_version_id ?? ''}`}
                  className="text-muted-foreground"
                >
                  <span className="font-medium text-foreground">
                    {CODE_LABEL[b.code] ?? b.code}:
                  </span>{' '}
                  {b.text}
                </li>
              ))}
            </ul>
          )}
          <button
            type="button"
            onClick={() => promote.mutate()}
            disabled={!release.data || blockers.length > 0 || promote.isPending}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-muted border border-border rounded text-xs text-muted-foreground hover:border-primary hover:text-primary transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ShieldCheck className="h-3 w-3" />
            Promote to verified
          </button>
          {promote.isError && !blockersFrom(promote.error) && (
            <p role="alert" className="text-xs text-destructive">
              {promote.error instanceof Error
                ? promote.error.message
                : 'Promotion failed'}
            </p>
          )}
        </div>
      )}
      <ManuscriptStatementsPanel projectId={projectId} />
      <ManuscriptReleaseSection
        projectId={projectId}
        draft={draft}
        canPromote={canPromote}
      />
    </div>
  );
};

export default DraftReleasePanel;
