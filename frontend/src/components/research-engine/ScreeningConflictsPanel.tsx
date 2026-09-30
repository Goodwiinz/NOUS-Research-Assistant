'use client';

import { useState, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@/hooks/useAuth';
import { APIErrorClass } from '@/types/api';
import {
  adjudicateScreening,
  listScreeningConflicts,
  listScreeningQueues,
  reopenScreening,
  type ProjectRoleAssignment,
  type ScreeningConflict,
  type ScreeningDecision,
  type ScreeningQueue,
} from '@/services/researchEngineService';

interface ScreeningConflictsPanelProps {
  projectId: string;
  roles: ProjectRoleAssignment[];
  readOnly?: boolean;
}

const DECISIONS: { value: ScreeningDecision; label: string }[] = [
  { value: 'include', label: 'Include' },
  { value: 'exclude', label: 'Exclude' },
  { value: 'uncertain', label: 'Uncertain' },
];

const STAGE_LABEL: Record<string, string> = {
  title_abstract: 'Title/abstract',
  full_text: 'Full text',
};

const STALE = 'Inputs changed — reload conflicts';
// The server's stale-tip 409s (screening_service INPUTS_STALE / RESOLUTION_STALE).
const STALE_DETAILS = new Set([
  'Adjudication inputs are stale',
  'Resolution changed; reload the queue',
]);

function errorText(error: unknown): string {
  if (
    error instanceof APIErrorClass &&
    error.error.status_code === 409 &&
    STALE_DETAILS.has(error.message)
  ) {
    return STALE;
  }
  return error instanceof Error ? error.message : 'Adjudication failed.';
}

/**
 * GOO-302: revealed conflicts, for a human holding the ADJUDICATOR role only.
 * Owners, editors and supervisors are not implicit adjudicators; the server
 * enforces that, this panel just does not render for them.
 */
export function ScreeningConflictsPanel({
  projectId,
  roles,
  readOnly = false,
}: ScreeningConflictsPanelProps): ReactElement | null {
  const userId = useAuth().user?.id;
  const isAdjudicator = roles.some(
    (role) => role.user_id === userId && role.role === 'adjudicator'
  );
  // Same cache owner as ScreeningQueuePanel's queue list.
  const queues = useQuery({
    queryKey: ['screening-queues', projectId],
    queryFn: () => listScreeningQueues(projectId),
    enabled: isAdjudicator,
  });
  if (!isAdjudicator) return null;
  const withConflicts = (queues.data ?? []).filter(
    (queue) => (queue.conflict_count ?? 0) > 0
  );
  return (
    <section className="rounded-xl border border-border bg-card p-5">
      <h2 className="font-medium text-foreground">Screening conflicts</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Revealed disagreements awaiting an adjudicator. Observations are never
        edited; your ruling is recorded against the exact inputs shown.
      </p>
      {queues.isLoading ? (
        <p role="status" className="mt-4 text-sm text-muted-foreground">
          Loading conflicts…
        </p>
      ) : queues.error ? (
        <p role="alert" className="mt-4 text-sm text-destructive">
          {errorText(queues.error)}
        </p>
      ) : withConflicts.length === 0 ? (
        <p className="mt-4 text-sm text-muted-foreground">No open conflicts.</p>
      ) : (
        withConflicts.map((queue) => (
          <QueueConflicts
            key={queue.id}
            projectId={projectId}
            queue={queue}
            readOnly={readOnly}
          />
        ))
      )}
    </section>
  );
}

function QueueConflicts({
  projectId,
  queue,
  readOnly,
}: {
  projectId: string;
  queue: ScreeningQueue;
  readOnly: boolean;
}): ReactElement {
  const conflicts = useQuery({
    queryKey: ['screening-queues', projectId, queue.id, 'conflicts'],
    queryFn: () => listScreeningConflicts(projectId, queue.id),
  });
  return (
    <div className="mt-4">
      <h3 className="text-sm font-medium text-foreground">
        {STAGE_LABEL[queue.stage]} queue ·{' '}
        {new Date(queue.created_at).toLocaleDateString()}
      </h3>
      {conflicts.isLoading && (
        <p role="status" className="mt-2 text-sm text-muted-foreground">
          Loading conflicts…
        </p>
      )}
      {conflicts.error && (
        <p role="alert" className="mt-2 text-sm text-destructive">
          {errorText(conflicts.error)}
        </p>
      )}
      <ul className="mt-2 divide-y divide-border">
        {(conflicts.data ?? []).map((conflict) => (
          <ConflictRow
            key={conflict.report_id}
            projectId={projectId}
            queue={queue}
            conflict={conflict}
            readOnly={readOnly}
          />
        ))}
      </ul>
    </div>
  );
}

function ConflictRow({
  projectId,
  queue,
  conflict,
  readOnly,
}: {
  projectId: string;
  queue: ScreeningQueue;
  conflict: ScreeningConflict;
  readOnly: boolean;
}): ReactElement {
  const queryClient = useQueryClient();
  const [decision, setDecision] = useState<ScreeningDecision | null>(null);
  const [reason, setReason] = useState('');
  const [rationale, setRationale] = useState('');
  const title = conflict.title_snapshot;
  const fullText = queue.stage === 'full_text';
  const needsReason = fullText && decision === 'exclude';
  const refresh = (): void => {
    void queryClient.invalidateQueries({
      queryKey: ['screening-queues', projectId],
    });
  };
  // One idempotency key per click; see ScreeningQueuePanel.
  const adjudicate = useMutation({
    mutationFn: ({ key, chosen }: { key: string; chosen: ScreeningDecision }) =>
      adjudicateScreening(projectId, queue.id, conflict.report_id, {
        resolution_id: conflict.resolution.id,
        input_observation_ids: conflict.resolution.input_observation_ids,
        criteria_hash: conflict.resolution.criteria_hash,
        decision: chosen,
        exclusion_reason: needsReason ? reason : null,
        rationale: rationale.trim(),
        idempotency_key: key,
      }),
    onSuccess: refresh,
    // A stale conflict refetches so the adjudicator sees the new inputs.
    onError: refresh,
  });
  const reopen = useMutation({
    mutationFn: (key: string) =>
      reopenScreening(projectId, queue.id, conflict.report_id, {
        resolution_id: conflict.resolution.id,
        rationale: rationale.trim(),
        idempotency_key: key,
      }),
    onSuccess: refresh,
    onError: refresh,
  });
  const busy = adjudicate.isPending || reopen.isPending;
  const locked = readOnly || busy;
  const hasRationale = rationale.trim().length > 0;
  const error = adjudicate.error ?? reopen.error;
  const reset = (): void => {
    adjudicate.reset();
    reopen.reset();
  };

  return (
    <li className="space-y-2 py-3 text-sm">
      <div className="font-medium text-foreground">{title}</div>
      <div className="text-xs text-muted-foreground">
        {Object.entries(conflict.identifiers).map(([kind, values]) => (
          <span key={kind} className="mr-3">
            {`${kind}: ${values.join(', ')}`}
          </span>
        ))}
      </div>
      <div
        aria-label={`Observations for ${title}`}
        className="grid grid-cols-1 gap-2 sm:grid-cols-2"
      >
        {conflict.observations.map((observation) => (
          <div
            key={observation.id}
            className="rounded-md border border-border p-2 text-xs"
          >
            <div className="text-muted-foreground">
              Reviewer {observation.reviewer_id}
            </div>
            <div className="font-medium text-foreground">
              {observation.decision}
              {observation.exclusion_reason
                ? ` — ${observation.exclusion_reason}`
                : ''}
            </div>
            {observation.note && (
              <p className="mt-1 text-muted-foreground">{observation.note}</p>
            )}
          </div>
        ))}
      </div>
      <div
        className="flex gap-2"
        role="group"
        aria-label={`Ruling for ${title}`}
      >
        {DECISIONS.map(({ value, label }) => (
          <button
            key={value}
            type="button"
            aria-pressed={decision === value}
            aria-label={`Rule ${label} for ${title}`}
            disabled={locked}
            onClick={() => setDecision(value)}
            className="rounded-md border border-border px-2 py-1 text-xs aria-pressed:bg-muted disabled:opacity-50"
          >
            {label}
          </button>
        ))}
      </div>
      {needsReason && (
        <select
          aria-label={`Exclusion reason for ${title}`}
          value={reason}
          disabled={locked}
          onChange={(event) => setReason(event.target.value)}
          className="rounded-md border border-border bg-background px-2 py-1 text-xs"
        >
          <option value="">Exclusion reason…</option>
          {conflict.exclusion_reasons.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      )}
      <textarea
        aria-label={`Rationale for ${title}`}
        value={rationale}
        disabled={locked}
        onChange={(event) => setRationale(event.target.value)}
        placeholder="Rationale (required)"
        rows={2}
        className="w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
      />
      <div className="flex gap-2">
        <button
          type="button"
          aria-label={`Adjudicate ${title}`}
          disabled={
            locked || !decision || !hasRationale || (needsReason && !reason)
          }
          onClick={() => {
            if (!decision) return;
            reset();
            adjudicate.mutate({ key: crypto.randomUUID(), chosen: decision });
          }}
          className="rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground disabled:opacity-50"
        >
          Adjudicate
        </button>
        <button
          type="button"
          aria-label={`Reopen ${title}`}
          disabled={locked || !hasRationale}
          onClick={() => {
            reset();
            reopen.mutate(crypto.randomUUID());
          }}
          className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
        >
          Reopen
        </button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(error)}
        </p>
      )}
    </li>
  );
}
