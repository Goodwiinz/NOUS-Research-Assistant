'use client';

import { useState, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@/hooks/useAuth';
import {
  createReviewVersion,
  ensureReviewWork,
  exportReviewVersion,
  getReviewAccounting,
  listReviewVersions,
  type ProjectRoleAssignment,
  type ReviewVersion,
} from '@/services/researchEngineService';

interface ReviewVersionsPanelProps {
  projectId: string;
  roles: ProjectRoleAssignment[];
  readOnly?: boolean;
}

/** Why a report is neither carried nor queued. */
const ATTENTION_LABEL: Record<string, string> = {
  not_in_delta: 'Not covered by the accepted delta',
  provider_failed: 'A provider that could return it failed',
  provider_capped: 'A provider hit its result cap',
  not_returned: 'Not returned by this search',
  no_doi_publication_check_not_performed:
    'No DOI, so no publication check was performed',
  merge_unresolved: 'Identity changed since the baseline (unresolved)',
};
const MISSING_LABEL: Record<string, string> = {
  decision_missing: 'No recorded decision',
  attribution_missing: 'Decision has no recorded actor',
};
const STAGE_LABEL: Record<string, string> = {
  title_abstract: 'Title/abstract',
  full_text: 'Full text',
};
const WORK_LABEL: Record<string, string> = {
  none: 'No new work',
  queued: 'Queued',
  queue_missing: 'Queue not created yet',
  queue_mismatch: 'Queue does not match the required reports',
  waiting_on_title_abstract: 'Waits for title/abstract decisions',
};
const BUTTON =
  'rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-muted disabled:opacity-50';
const INPUT =
  'rounded-md border border-border bg-background px-2 py-1.5 text-sm';
const ACCOUNTING_ROWS: [string, string][] = [
  ['studies_in_previous_version', 'Studies in previous version'],
  ['new_records_identified', 'New records identified'],
  ['duplicates_removed', 'Duplicates removed'],
  ['changed_sources', 'Changed sources'],
  ['corrected_retracted', 'Corrected or retracted sources'],
  ['amended_out', 'Previously included, now excluded'],
  ['amended_in', 'Previously excluded, now included'],
  ['new_studies_included', 'New studies included'],
  ['total_studies_included', 'Total studies included'],
];

function short(id: string): string {
  return id.slice(0, 8);
}

function Accounting({
  projectId,
  versionId,
}: {
  projectId: string;
  versionId: string;
}): ReactElement {
  const accounting = useQuery({
    queryKey: ['project', projectId, 'review-accounting', versionId],
    queryFn: () => getReviewAccounting(projectId, versionId),
    retry: false,
  });
  if (accounting.isLoading) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Loading accounting…
      </p>
    );
  }
  if (accounting.error) {
    return (
      <p role="alert" className="text-sm text-destructive">
        Update accounting does not reconcile.
      </p>
    );
  }
  const boxes = accounting.data?.boxes as Record<string, unknown> | null;
  if (!boxes) {
    return (
      <p className="text-sm text-muted-foreground">
        {accounting.data?.error ?? 'No parent version to compare with.'}
      </p>
    );
  }
  return (
    <table className="mt-2 text-sm" aria-label="Update accounting">
      <tbody className="divide-y divide-border">
        {ACCOUNTING_ROWS.map(([key, label]) => (
          <tr key={key}>
            <th className="py-1 pr-4 text-left font-normal text-muted-foreground">
              {label}
            </th>
            <td className="py-1 font-mono">{String(boxes[key] ?? 0)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function VersionRow({
  projectId,
  version,
  canSupervise,
}: {
  projectId: string;
  version: ReviewVersion;
  canSupervise: boolean;
}): ReactElement {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const retry = useMutation({
    mutationFn: () => ensureReviewWork(projectId, version.id),
    onSuccess: () =>
      void queryClient.invalidateQueries({
        queryKey: ['project', projectId, 'review-versions'],
      }),
  });
  const required = version.work.reduce(
    (sum, w) => sum + w.required_report_ids.length,
    0
  );
  const missingQueue = version.work.some((w) => w.status === 'queue_missing');
  const stale = Object.entries(version.stale_counts ?? {});
  return (
    <li className="space-y-2 py-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium text-foreground">
          Version {version.version_number}
        </span>
        {version.is_tip && (
          <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
            Current
          </span>
        )}
        <span className="text-muted-foreground">
          {version.parent_review_version_id
            ? `update of a delta (${version.delta_hash?.slice(0, 8)})`
            : 'baseline'}
        </span>
        <span className="ml-auto text-muted-foreground">
          {version.carried.length} decisions carried, {required} reports to
          review
        </span>
      </div>
      <p className="text-sm text-muted-foreground">{version.rationale}</p>
      {version.work
        .filter((w) => w.status !== 'none')
        .map((w) => (
          <p key={w.stage} className="text-sm">
            {STAGE_LABEL[w.stage]}: {WORK_LABEL[w.status]}
            {w.queue_id && (
              <>
                {' — '}
                <a href="#select" className="underline">
                  queue {short(w.queue_id)}
                </a>{' '}
                ({w.resolved_count} resolved, {w.unresolved_count} open)
              </>
            )}
          </p>
        ))}
      {version.needs_attention.length > 0 && (
        <div className="text-sm">
          <p className="font-medium text-foreground">Needs attention</p>
          <ul aria-label="Needs attention">
            {version.needs_attention.map((item) => (
              <li key={item.report_id} className="text-muted-foreground">
                <span className="font-mono text-xs">{item.report_id}</span>:{' '}
                {ATTENTION_LABEL[item.reason ?? ''] ??
                  item.reason ??
                  'Unclassified'}
              </li>
            ))}
          </ul>
        </div>
      )}
      {version.missing_history.length > 0 && (
        <div className="text-sm">
          <p className="font-medium text-foreground">Missing history</p>
          <ul aria-label="Missing history">
            {version.missing_history.map((item) => (
              <li
                key={`${item.report_id}-${item.stage}`}
                className="text-muted-foreground"
              >
                <span className="font-mono text-xs">{item.report_id}</span> (
                {STAGE_LABEL[item.stage]}): {MISSING_LABEL[item.kind]}
              </li>
            ))}
          </ul>
        </div>
      )}
      {stale.length > 0 && (
        <ul className="flex flex-wrap gap-2" aria-label="Stale items">
          {stale.map(([kind, count]) => (
            <li
              key={kind}
              className="rounded-full bg-destructive/10 px-2 py-0.5 text-xs text-destructive"
            >
              Stale {kind}: {count}
            </li>
          ))}
        </ul>
      )}
      {version.release && (
        <p className="text-sm text-muted-foreground">
          Release {short(version.release.release_id)}
          {version.release.supersedes_release_id &&
            ` supersedes ${short(version.release.supersedes_release_id)}`}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className={BUTTON}
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          {open ? 'Hide accounting' : 'Show accounting'}
        </button>
        <button
          type="button"
          className={BUTTON}
          onClick={() => void exportReviewVersion(projectId, version.id)}
        >
          Export version
        </button>
        {canSupervise && missingQueue && (
          <button
            type="button"
            className={BUTTON}
            onClick={() => retry.mutate()}
            disabled={retry.isPending}
          >
            Create review work
          </button>
        )}
      </div>
      {open && <Accounting projectId={projectId} versionId={version.id} />}
      {retry.error && (
        <p role="alert" className="text-sm text-destructive">
          {retry.error instanceof Error
            ? retry.error.message
            : 'Failed to create the review work.'}
        </p>
      )}
    </li>
  );
}

/**
 * GOO-320: superseding review versions. The baseline freezes the current
 * decisions; each update accepts one search delta, carries unchanged
 * decisions by reference, and queues changed, new or corrected reports for
 * review. Unknown reports are listed for attention, never counted as
 * carried.
 */
export function ReviewVersionsPanel({
  projectId,
  roles,
  readOnly = false,
}: ReviewVersionsPanelProps): ReactElement {
  const queryClient = useQueryClient();
  const userId = useAuth().user?.id;
  const canSupervise =
    !readOnly &&
    roles.some((r) => r.user_id === userId && r.role === 'supervisor');
  const reviewers = roles
    .filter((r) => r.role === 'reviewer')
    .map((r) => r.user_id);
  const queryKey = ['project', projectId, 'review-versions'] as const;
  const listing = useQuery({
    queryKey,
    queryFn: () => listReviewVersions(projectId),
    retry: false,
  });
  const [execution, setExecution] = useState('');
  const [rationale, setRationale] = useState('');
  const versions = listing.data?.versions ?? [];
  const tip = versions[versions.length - 1];
  const deltas = listing.data?.deltas ?? [];
  const picked = deltas.find((d) => d.execution_id === execution);
  const create = useMutation({
    mutationFn: () =>
      createReviewVersion(projectId, {
        parent_review_version_id: picked ? tip?.id : null,
        execution_id: picked?.execution_id ?? null,
        delta_hash: picked?.delta_hash ?? null,
        reviewer_user_ids: picked ? reviewers : [],
        carry_with_uncertainty: [],
        rationale: rationale.trim(),
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () => {
      setExecution('');
      setRationale('');
      void queryClient.invalidateQueries({ queryKey });
    },
  });
  const ready = Boolean(rationale.trim()) && (!tip || Boolean(picked));

  return (
    <section className="rounded-xl border border-border bg-card p-5">
      <h3 className="font-medium text-foreground">Review versions</h3>
      <p className="mt-1 text-sm text-muted-foreground">
        Each update accepts one search delta. Unchanged decisions are carried
        with their original reviewers; changed evidence is reviewed again.
      </p>
      {listing.isLoading && (
        <p role="status" className="mt-3 text-sm text-muted-foreground">
          Loading review versions…
        </p>
      )}
      {listing.error && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          Failed to load review versions.
        </p>
      )}
      {listing.data && versions.length === 0 && (
        <p className="mt-3 text-sm text-muted-foreground">
          No review version yet.
        </p>
      )}
      <ol className="mt-2 divide-y divide-border">
        {versions.map((version) => (
          <VersionRow
            key={version.id}
            projectId={projectId}
            version={version}
            canSupervise={canSupervise}
          />
        ))}
      </ol>
      {canSupervise && listing.data && (
        <form
          className="mt-4 grid gap-2 sm:grid-cols-2"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          {tip && (
            <label className="text-sm">
              <span className="block text-muted-foreground">
                Accepted search delta
              </span>
              <select
                className={`${INPUT} w-full`}
                value={execution}
                onChange={(event) => setExecution(event.target.value)}
              >
                <option value="">Choose a delta</option>
                {deltas.map((delta) => (
                  <option key={delta.execution_id} value={delta.execution_id}>
                    {delta.scheduled_local.replace('T', ' ')}:{' '}
                    {delta.counts.new} new, {delta.counts.changed} changed,{' '}
                    {delta.counts.corrected_retracted} corrected or retracted,{' '}
                    {delta.counts.unknown} unknown
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="text-sm">
            <span className="block text-muted-foreground">Rationale</span>
            <input
              className={`${INPUT} w-full`}
              value={rationale}
              onChange={(event) => setRationale(event.target.value)}
            />
          </label>
          <button
            type="submit"
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 sm:col-span-2 sm:justify-self-start"
            disabled={!ready || create.isPending}
          >
            {tip ? 'Create update from delta' : 'Freeze baseline version'}
          </button>
          {create.error && (
            <p role="alert" className="text-sm text-destructive sm:col-span-2">
              {create.error instanceof Error
                ? create.error.message
                : 'Failed to create the review version.'}
            </p>
          )}
        </form>
      )}
    </section>
  );
}
