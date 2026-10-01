'use client';

/**
 * GOO-318 archive deposit of one verified manuscript release to the Zenodo
 * sandbox. The approval binds this exact package hash and account; holding
 * a role never authorizes by itself, and the requester may not approve. The
 * phase stepper is driven only by the server's derived status: an
 * `ambiguous` deposit reads "Checking with Zenodo…", never published, and
 * the DOI link appears only once the read-back verified it.
 */

import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Archive, RotateCcw, ShieldCheck, ShieldOff } from 'lucide-react';
import { projectService } from '@/services/projectService';
import type { ApiManuscriptRelease } from '@/types/api/manuscript-release-contract';
import type {
  ApiDeposit,
  ApiDepositStatus,
} from '@/types/api/research-deposit-contract';

const PHASES: { key: ApiDepositStatus; label: string }[] = [
  { key: 'prepared', label: 'Prepared' },
  { key: 'draft_created', label: 'Draft created' },
  { key: 'files_uploaded', label: 'Files uploaded' },
  { key: 'published', label: 'Published' },
  { key: 'verified', label: 'Verified' },
];
const IN_FLIGHT = new Set(['pending', 'processing']);
const REASONS: Record<string, string> = {
  approval_invalid: 'the approval is no longer in force',
  project_unavailable: 'the project is archived or deleted',
  requester_unauthorized: 'the requester lost the release role',
  readback_mismatch: 'Zenodo did not return the exact files or DOI',
};

const BUTTON =
  'flex items-center gap-1.5 px-2 py-1 bg-muted border border-border rounded text-xs text-muted-foreground hover:border-primary hover:text-primary transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring';

export const depositsQueryKey = (projectId: string) =>
  ['project', projectId, 'deposits'] as const;

function statusText(deposit: ApiDeposit): string {
  if (deposit.status === 'ambiguous') return 'Checking with Zenodo…';
  if (deposit.status === 'failed') {
    return `Failed — ${REASONS[deposit.last_reason ?? ''] ?? deposit.last_reason ?? 'see attempts'}`;
  }
  const phase = PHASES.find((p) => p.key === deposit.status);
  return phase ? phase.label : deposit.status;
}

const Stepper: React.FC<{ deposit: ApiDeposit }> = ({ deposit }) => {
  const reached = PHASES.findIndex((p) => p.key === deposit.status);
  return (
    <ol aria-label="Deposit phases" className="flex flex-wrap gap-1 text-xs">
      {PHASES.map((phase, index) => {
        const done = reached >= index;
        return (
          <li
            key={phase.key}
            aria-label={`${phase.label}: ${done ? 'done' : 'not yet'}`}
            className={`px-1.5 py-0.5 rounded ${
              done
                ? 'bg-primary/10 text-primary'
                : 'bg-muted text-muted-foreground'
            }`}
          >
            {phase.label}
          </li>
        );
      })}
    </ol>
  );
};

interface DepositPanelProps {
  projectId: string;
  release: ApiManuscriptRelease;
  canRelease: boolean;
}

export const DepositPanel: React.FC<DepositPanelProps> = ({
  projectId,
  release,
  canRelease,
}) => {
  const queryClient = useQueryClient();
  const key = depositsQueryKey(projectId);
  const [rationale, setRationale] = React.useState('');
  const { data } = useQuery({
    queryKey: key,
    queryFn: () => projectService.listDeposits(projectId),
    retry: false,
    refetchInterval: (query) =>
      (query.state.data?.deposits ?? []).some((d) =>
        IN_FLIGHT.has(d.queue_status ?? '')
      )
        ? 5000
        : false,
  });
  const settle = (): void => {
    void queryClient.invalidateQueries({ queryKey: key });
    void queryClient.invalidateQueries({
      queryKey: ['project', projectId, 'manuscript-releases'],
    });
  };
  const approve = useMutation({
    mutationFn: () =>
      projectService.approveDeposit(projectId, {
        release_id: release.id,
        package_sha256: release.package_sha256,
        rationale,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () => setRationale(''),
    onSettled: settle,
  });
  const approval = (data?.approvals ?? []).find(
    (a) => a.release_id === release.id && a.in_force
  );
  const revoke = useMutation({
    mutationFn: () =>
      projectService.revokeDepositApproval(projectId, approval?.id ?? '', {
        rationale: 'Revoked from the release view',
        idempotency_key: crypto.randomUUID(),
      }),
    onSettled: settle,
  });
  const request = useMutation({
    mutationFn: () =>
      projectService.requestDeposit(projectId, {
        release_id: release.id,
        package_sha256: release.package_sha256,
        idempotency_key: crypto.randomUUID(),
      }),
    onSettled: settle,
  });
  const deposit = (data?.deposits ?? []).find(
    (d) => d.release_id === release.id
  );
  const requeue = useMutation({
    mutationFn: () =>
      projectService.requeueDeposit(projectId, deposit?.operation_id ?? ''),
    onSettled: settle,
  });
  const configured = data?.configured ?? false;
  const error = [approve, revoke, request, requeue].find(
    (m) => m.isError
  )?.error;
  const stopped =
    deposit &&
    !IN_FLIGHT.has(deposit.queue_status ?? '') &&
    deposit.status !== 'verified' &&
    deposit.status !== 'failed';

  return (
    <section aria-label="Archive deposit" className="space-y-1 text-xs">
      <div className="flex items-center gap-2">
        <Archive className="h-3 w-3" aria-hidden="true" />
        <span className="font-medium text-foreground">Zenodo deposit</span>
        <span className="px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
          Sandbox
        </span>
        {data?.account_ref && (
          <span className="text-muted-foreground">{data.account_ref}</span>
        )}
      </div>
      {!configured && (
        <p className="text-muted-foreground">
          Archive deposits are not configured.
        </p>
      )}
      {approval ? (
        <p aria-label="Deposit approval">
          Approved by {approval.actor_role}{' '}
          {approval.approved_by_id.slice(0, 8)} for package{' '}
          {approval.package_sha256.slice(0, 12)} ({approval.account_ref}).
        </p>
      ) : (
        <p aria-label="Deposit approval" className="text-muted-foreground">
          No deposit approval in force for this package.
        </p>
      )}
      {canRelease && (
        <div className="flex flex-wrap items-center gap-2">
          {approval ? (
            <button
              type="button"
              onClick={() => revoke.mutate()}
              disabled={revoke.isPending}
              className={BUTTON}
            >
              <ShieldOff className="h-3 w-3" />
              Revoke approval
            </button>
          ) : (
            <>
              <label className="sr-only" htmlFor={`rationale-${release.id}`}>
                Approval rationale
              </label>
              <input
                id={`rationale-${release.id}`}
                value={rationale}
                onChange={(event) => setRationale(event.target.value)}
                placeholder="Why this package may be deposited"
                className="px-2 py-1 bg-background border border-border rounded"
              />
              <button
                type="button"
                onClick={() => approve.mutate()}
                disabled={!configured || !rationale.trim() || approve.isPending}
                className={BUTTON}
              >
                <ShieldCheck className="h-3 w-3" />
                Approve deposit
              </button>
            </>
          )}
          {!deposit && (
            <button
              type="button"
              onClick={() => request.mutate()}
              disabled={!configured || !approval || request.isPending}
              className={BUTTON}
            >
              <Archive className="h-3 w-3" />
              Deposit to Zenodo sandbox
            </button>
          )}
          {stopped && (
            <button
              type="button"
              onClick={() => requeue.mutate()}
              disabled={requeue.isPending}
              className={BUTTON}
            >
              <RotateCcw className="h-3 w-3" />
              Retry
            </button>
          )}
        </div>
      )}
      {deposit && (
        <div className="space-y-1">
          <Stepper deposit={deposit} />
          <p role="status" aria-label="Deposit status">
            {statusText(deposit)}
            {deposit.last_reason &&
              deposit.status !== 'failed' &&
              ` (stopped: ${REASONS[deposit.last_reason] ?? deposit.last_reason})`}
          </p>
          {deposit.status === 'verified' && deposit.doi && deposit.doi_url && (
            <a
              href={deposit.doi_url}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline"
            >
              DOI {deposit.doi}
            </a>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="text-destructive">
          {error instanceof Error ? error.message : 'Deposit action failed'}
        </p>
      )}
    </section>
  );
};

export default DepositPanel;
