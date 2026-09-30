'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { ReactElement } from 'react';

import { useAuth } from '@/hooks/useAuth';
import { integrationActionService } from '@/services/integrationActionService';
import type { ActionState } from '@/types/api/integration-action-contract';

const STATE_TEXT: Record<ActionState, string> = {
  awaiting_approval: 'Waiting for your decision. Nothing has been created yet.',
  approved: 'Approved. NOUS is creating the note.',
  executing: 'Approved. NOUS is creating the note.',
  succeeded: 'Created.',
  failed: 'Not created.',
  outcome_unknown:
    'NOUS could not confirm whether the note was created. Check the project notes before asking again.',
};
const IN_FLIGHT = new Set<ActionState>(['approved', 'executing']);

/**
 * Shows the exact note a connected harness asked to create and lets the
 * requester approve or deny it once. Content is shown as plain text so
 * formatting cannot hide what will be written.
 */
export function IntegrationActionApproval({
  invocationId,
}: {
  invocationId: string;
}): ReactElement {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const queryKey = ['integration-action', user?.id, invocationId];
  const review = useQuery({
    queryKey,
    enabled: Boolean(user?.id && invocationId),
    queryFn: () => integrationActionService.review(invocationId),
    refetchInterval: (query) =>
      query.state.data && IN_FLIGHT.has(query.state.data.state) ? 2_000 : false,
    retry: false,
    staleTime: 0,
  });
  const decision = useMutation({
    mutationFn: (approved: boolean) =>
      integrationActionService.decide(invocationId, approved),
    onSettled: () => queryClient.invalidateQueries({ queryKey }),
  });
  const action = review.data;
  // One decision per page: a second click, even after an error, must not
  // race the first; the refetch shows the stored outcome.
  const canDecide = action?.state === 'awaiting_approval' && decision.isIdle;

  return (
    <section className="mx-auto max-w-2xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Approve a note</h1>
      <p>
        A connected coding session asked to create this note. Approve only if
        you expect it.
      </p>
      {review.isPending && <p role="status">Loading request…</p>}
      {review.isError && (
        <p role="alert">
          This request could not be loaded. It may not exist or may belong to
          another account.
        </p>
      )}
      {decision.isError && (
        <p role="alert">
          Your decision was not recorded. The request may already have been
          decided.
        </p>
      )}
      {action && (
        <>
          <dl className="space-y-3 break-words">
            <div>
              <dt className="font-semibold">Project</dt>
              <dd>{action.project_label}</dd>
            </div>
            <div>
              <dt className="font-semibold">Title</dt>
              <dd>{action.title}</dd>
            </div>
            {action.tags.length > 0 && (
              <div>
                <dt className="font-semibold">Tags</dt>
                <dd>{action.tags.join(', ')}</dd>
              </div>
            )}
            <div>
              <dt className="font-semibold">Content</dt>
              <dd>
                <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded border p-3 text-sm">
                  {action.content}
                </pre>
              </dd>
            </div>
          </dl>
          <p role="status">
            {STATE_TEXT[action.state]}
            {action.state === 'failed' && action.last_error
              ? ` ${action.last_error}.`
              : ''}
          </p>
          <div className="flex gap-3">
            <button
              type="button"
              disabled={!canDecide}
              onClick={() => decision.mutate(false)}
              className="rounded border px-4 py-2 disabled:opacity-50"
            >
              Deny
            </button>
            <button
              type="button"
              disabled={!canDecide}
              onClick={() => decision.mutate(true)}
              className="rounded bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
            >
              Approve and create note
            </button>
          </div>
        </>
      )}
    </section>
  );
}
