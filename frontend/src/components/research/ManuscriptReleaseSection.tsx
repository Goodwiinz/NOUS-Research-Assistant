'use client';

/**
 * GOO-315 manuscript releases for one exact draft version. A candidate can be
 * built at any time and shows every check as its own row; reporting
 * completeness and reproducibility are reported, never required. Promotion
 * is offered only to adjudicators and supervisors and stays disabled while
 * an obligation fails (the server re-checks everything anyway). Packaging
 * never authorizes an external submission.
 */

import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, PackageCheck, ShieldCheck } from 'lucide-react';
import { projectService, type Draft } from '@/services/projectService';
import type {
  ApiCheckState,
  ApiManuscriptRelease,
  ApiReleaseVerification,
} from '@/types/api/manuscript-release-contract';

const CHECKS: { key: string; label: string }[] = [
  { key: 'claim_support', label: 'Claim support' },
  { key: 'method_adherence', label: 'Method adherence' },
  { key: 'synthesis_appraisal', label: 'Synthesis and appraisal' },
  { key: 'peer_review', label: 'Peer review' },
  { key: 'reporting_completeness', label: 'Reporting completeness' },
  { key: 'experiment_reproducibility', label: 'Experiment reproducibility' },
];
const LABEL: Record<string, string> = Object.fromEntries(
  CHECKS.map((c) => [c.key, c.label])
);

const STATE: Record<ApiCheckState, { label: string; tone: string }> = {
  pass: { label: 'Pass', tone: 'bg-primary/10 text-primary' },
  fail: { label: 'Fail', tone: 'bg-destructive/10 text-destructive' },
  unknown: { label: 'Unknown', tone: 'bg-muted text-muted-foreground' },
  not_applicable: {
    label: 'Not applicable',
    tone: 'bg-muted text-muted-foreground',
  },
};

const STATUS_TONE: Record<ApiManuscriptRelease['status'], string> = {
  candidate: 'bg-muted text-muted-foreground',
  verified: 'bg-primary/10 text-primary',
  stale: 'bg-destructive/10 text-destructive',
};

const CAUSE: Record<string, string> = {
  draft_release_invalidated: 'its verified draft release was invalidated',
  upstream_changed: 'an upstream input changed',
};

const BUTTON =
  'flex items-center gap-1.5 px-2 py-1 bg-muted border border-border rounded text-xs text-muted-foreground hover:border-primary hover:text-primary transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring';

export const manuscriptReleasesQueryKey = (projectId: string) =>
  ['project', projectId, 'manuscript-releases'] as const;

const ReleaseCard: React.FC<{
  projectId: string;
  release: ApiManuscriptRelease;
  canPromote: boolean;
}> = ({ projectId, release, canPromote }) => {
  const queryClient = useQueryClient();
  const [verification, setVerification] =
    React.useState<ApiReleaseVerification | null>(null);
  const promote = useMutation({
    mutationFn: () =>
      projectService.promoteManuscriptRelease(projectId, release.id, {
        expected_snapshot_hash: release.snapshot_hash,
        expected_content_hash: release.content_hash,
        idempotency_key: crypto.randomUUID(),
      }),
    onSettled: () =>
      queryClient.invalidateQueries({
        queryKey: manuscriptReleasesQueryKey(projectId),
      }),
  });
  const verify = useMutation({
    mutationFn: () =>
      projectService.verifyManuscriptRelease(projectId, release.id),
    onSuccess: setVerification,
  });
  const failing = release.failing_obligations ?? [];
  const label = `${release.stage === 'verified' ? 'Verified' : 'Candidate'} release ${release.id.slice(0, 8)}`;

  return (
    <li
      aria-label={label}
      className="border border-border rounded p-2 space-y-2"
    >
      <div className="flex items-center gap-2 text-xs">
        <span className="font-medium text-foreground">{label}</span>
        <span
          aria-label={`Manuscript release status: ${release.status}`}
          className={`px-1.5 py-0.5 rounded ${STATUS_TONE[release.status]}`}
        >
          {release.status}
        </span>
        <span className="text-muted-foreground">
          sha256 {release.package_sha256.slice(0, 12)}
        </span>
      </div>
      {release.status === 'stale' && (
        <p role="status" className="text-xs text-destructive">
          Stale — {CAUSE[release.stale_cause ?? ''] ?? 'an upstream change'}.
          The package bytes still verify.
        </p>
      )}
      <ul aria-label="Release checks" className="space-y-1 text-xs">
        {CHECKS.map(({ key, label: name }) => {
          const check = release.checks[key];
          if (!check) return null;
          return (
            <li key={key} aria-label={`${name}: ${STATE[check.state].label}`}>
              <span className="text-foreground">{name}</span>{' '}
              <span className={`px-1 rounded ${STATE[check.state].tone}`}>
                {STATE[check.state].label}
              </span>
              {check.items && check.items.length > 0 && (
                <ul className="ml-4 list-disc text-muted-foreground">
                  {check.items.map((item, index) => (
                    <li key={`${item.code}-${item.ref ?? index}`}>
                      {item.code}
                      {item.detail ? ` — ${item.detail}` : ''}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
      <div className="flex flex-wrap items-center gap-2">
        {canPromote && release.stage === 'candidate' && (
          <button
            type="button"
            onClick={() => promote.mutate()}
            disabled={failing.length > 0 || promote.isPending}
            className={BUTTON}
          >
            <ShieldCheck className="h-3 w-3" />
            Promote to verified release
          </button>
        )}
        <button
          type="button"
          onClick={() =>
            void projectService.downloadManuscriptPackage(projectId, release.id)
          }
          className={BUTTON}
        >
          <Download className="h-3 w-3" />
          Download package
        </button>
        <button
          type="button"
          onClick={() => verify.mutate()}
          disabled={verify.isPending}
          className={BUTTON}
        >
          <PackageCheck className="h-3 w-3" />
          Verify
        </button>
      </div>
      {canPromote && release.stage === 'candidate' && failing.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Failing obligations:{' '}
          {failing.map((key) => LABEL[key] ?? key).join(', ')}
        </p>
      )}
      {promote.isError && (
        <p role="alert" className="text-xs text-destructive">
          {promote.error instanceof Error
            ? promote.error.message
            : 'Promotion failed'}
        </p>
      )}
      {verification && (
        <p role="status" className="text-xs text-muted-foreground">
          Package {verification.package_sha256_ok ? 'verified' : 'mismatch'};
          bundle {verification.bundle_ok ? 'ok' : 'failed'}; references{' '}
          {verification.reference_mapping.map((m) => m.key).join(', ') ||
            'none'}
          {verification.references_ok ? '' : ' (mismatch)'}.
        </p>
      )}
    </li>
  );
};

interface ManuscriptReleaseSectionProps {
  projectId: string;
  draft: Draft;
  canPromote: boolean;
}

export const ManuscriptReleaseSection: React.FC<
  ManuscriptReleaseSectionProps
> = ({ projectId, draft, canPromote }) => {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: manuscriptReleasesQueryKey(projectId),
    queryFn: () => projectService.listManuscriptReleases(projectId),
    retry: false,
  });
  const create = useMutation({
    mutationFn: () =>
      projectService.createCandidateRelease(projectId, {
        draft_id: draft.id,
        expected_content_hash: draft.content_hash ?? '',
        idempotency_key: crypto.randomUUID(),
      }),
    onSettled: () =>
      queryClient.invalidateQueries({
        queryKey: manuscriptReleasesQueryKey(projectId),
      }),
  });
  const releases = (data?.releases ?? []).filter(
    (r) => r.draft_id === draft.id && r.draft_version === draft.version
  );

  return (
    <section aria-label="Manuscript releases" className="space-y-2">
      <div className="flex items-center gap-2">
        <h3 className="text-xs font-medium text-foreground">
          Manuscript releases
        </h3>
        <button
          type="button"
          onClick={() => create.mutate()}
          disabled={!draft.content_hash || create.isPending}
          className={BUTTON}
        >
          Build candidate
        </button>
      </div>
      <p className="text-xs text-muted-foreground">
        Not authorized for external submission.
      </p>
      {create.isError && (
        <p role="alert" className="text-xs text-destructive">
          {create.error instanceof Error
            ? create.error.message
            : 'Candidate build failed'}
        </p>
      )}
      {releases.length > 0 && (
        <ul className="space-y-2">
          {releases.map((release) => (
            <ReleaseCard
              key={release.id}
              projectId={projectId}
              release={release}
              canPromote={canPromote}
            />
          ))}
        </ul>
      )}
    </section>
  );
};

export default ManuscriptReleaseSection;
