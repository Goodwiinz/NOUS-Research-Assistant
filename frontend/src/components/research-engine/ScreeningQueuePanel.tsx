'use client';

import { useState, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@/hooks/useAuth';
import { APIErrorClass } from '@/types/api';
import {
  assignScreeningReviewer,
  createScreeningQueue,
  getMyScreeningQueue,
  listScreeningHistory,
  listScreeningQueues,
  revokeScreeningAssignment,
  submitScreeningObservation,
  type IdentityEvent,
  type MyScreeningItem,
  type MyScreeningQueue,
  type ProjectRoleAssignment,
  type ScreeningDecision,
  type ScreeningResolution,
} from '@/services/researchEngineService';

interface ScreeningQueuePanelProps {
  projectId: string;
  approvedProtocolVersionId?: string;
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

/** Active assignments (id -> reviewer) replayed from the queue's ledger. */
function activeAssignments(events: IdentityEvent[]): Map<string, string> {
  const active = new Map<string, string>();
  for (const event of events) {
    const { assignment_id: id, reviewer_id: reviewer } = event.payload;
    if (typeof id !== 'string' || typeof reviewer !== 'string') continue;
    if (event.event_type === 'screening.assigned') active.set(id, reviewer);
    if (event.event_type === 'screening.unassigned') active.delete(id);
  }
  return active;
}

/** The resolution badge text: never an editable value, only the server's. */
function resolutionLabel(resolution: ScreeningResolution): string {
  const { basis, outcome, exclusion_reason: reason } = resolution;
  const decided = `${outcome ?? ''}${reason ? ` — ${reason}` : ''}`;
  if (basis === 'single') return `Decided: ${decided}`;
  if (basis === 'agreement') return `Agreed: ${decided}`;
  if (basis === 'adjudicated') return `Adjudicated: ${decided}`;
  if (basis === 'reopened') return 'Reopened';
  return 'Conflict';
}

/** GOO-302 reveal state: peers' decisions appear only once the server reveals. */
function RevealState({ item }: { item: MyScreeningItem }): ReactElement {
  if (item.reveal_state !== 'revealed' || !item.resolution) {
    return (
      <p className="text-xs text-muted-foreground">
        Other reviewers&apos; decisions are hidden until reveal.
      </p>
    );
  }
  return (
    <div className="space-y-1 text-xs">
      <span className="inline-block rounded bg-muted px-2 py-0.5 font-medium text-foreground">
        {resolutionLabel(item.resolution)}
      </span>
      {(item.others ?? []).length > 0 && (
        <ul
          aria-label={`Other reviewers' decisions for ${item.title_snapshot}`}
          className="text-muted-foreground"
        >
          {(item.others ?? []).map((other) => (
            <li key={other.id}>
              {`Reviewer ${other.reviewer_id}: ${other.decision}`}
              {other.exclusion_reason ? ` (${other.exclusion_reason})` : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const RESOLVED_TITLE = 'Resolved — an adjudicator must reopen';

const errorText = (error: unknown): string =>
  error instanceof Error ? error.message : 'Screening request failed.';

export function ScreeningQueuePanel({
  projectId,
  approvedProtocolVersionId,
  roles,
  readOnly = false,
}: ScreeningQueuePanelProps): ReactElement {
  const queryClient = useQueryClient();
  const queryKey = ['screening-queues', projectId] as const;
  const userId = useAuth().user?.id;
  const mine = new Set(
    roles.filter((role) => role.user_id === userId).map((role) => role.role)
  );
  const isSupervisor = mine.has('supervisor') && !readOnly;
  const isReviewer = mine.has('reviewer');
  const reviewers = [
    ...new Set(
      roles.filter((r) => r.role === 'reviewer').map((r) => r.user_id)
    ),
  ];

  const [selected, setSelected] = useState<string | null>(null);
  const [assignee, setAssignee] = useState('');
  const [revokeReason, setRevokeReason] = useState('');
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});

  const queues = useQuery({
    queryKey,
    queryFn: () => listScreeningQueues(projectId),
  });
  const queueId = selected ?? queues.data?.[0]?.id ?? null;
  const queue = queues.data?.find((item) => item.id === queueId);
  const myQueue = useQuery({
    queryKey: [...queryKey, queueId, 'mine'],
    queryFn: () => getMyScreeningQueue(projectId, queueId ?? ''),
    enabled: isReviewer && Boolean(queueId),
    retry: false,
  });
  const history = useQuery({
    queryKey: [...queryKey, queueId, 'history'],
    queryFn: () => listScreeningHistory(projectId, queueId ?? ''),
    enabled: isSupervisor && Boolean(queueId),
  });
  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: [...queryKey] });
  };

  // One idempotency key per click. Mutations are not retried; a transport-level
  // resend of the same request carries the same key and the server replays it,
  // while a second click is a new request with a new key.
  const create = useMutation({
    mutationFn: (key: string) =>
      createScreeningQueue(projectId, {
        protocol_version_id: approvedProtocolVersionId ?? '',
        stage: 'title_abstract',
        idempotency_key: key,
      }),
    onSuccess: (created) => {
      setSelected(created.id);
      refresh();
    },
  });
  const assign = useMutation({
    mutationFn: (key: string) =>
      assignScreeningReviewer(projectId, queueId ?? '', {
        reviewer_user_id: assignee,
        idempotency_key: key,
      }),
    onSuccess: () => {
      setAssignee('');
      refresh();
    },
  });
  const revoke = useMutation({
    mutationFn: ({
      assignmentId,
      key,
    }: {
      assignmentId: string;
      key: string;
    }) =>
      revokeScreeningAssignment(projectId, queueId ?? '', assignmentId, {
        reason: revokeReason.trim(),
        idempotency_key: key,
      }),
    onSuccess: () => {
      setRevokeReason('');
      refresh();
    },
  });
  const submit = useMutation({
    mutationFn: ({
      view,
      item,
      decision,
      key,
    }: {
      view: MyScreeningQueue;
      item: MyScreeningItem;
      decision: ScreeningDecision;
      key: string;
    }) =>
      submitScreeningObservation(projectId, view.queue.id, {
        report_id: item.report_id,
        assignment_id: view.assignment_id,
        criteria_hash: view.queue.criteria_hash,
        decision,
        exclusion_reason:
          view.queue.stage === 'full_text' && decision === 'exclude'
            ? reasons[item.report_id]
            : null,
        note: notes[item.report_id]?.trim() || null,
        supersedes_observation_id: item.observation?.id ?? null,
        idempotency_key: key,
      }),
    onSuccess: (_observation, { item }) => {
      // The submitted note/reason belong to that decision; start the row clean.
      const clear = (all: Record<string, string>): Record<string, string> => {
        const { [item.report_id]: _dropped, ...rest } = all;
        return rest;
      };
      setNotes(clear);
      setReasons(clear);
      refresh();
    },
  });

  const mutations = [create, assign, revoke, submit];
  const busy = mutations.some((mutation) => mutation.isPending);
  const error =
    mutations.find((mutation) => mutation.error)?.error ??
    queues.error ??
    history.error;
  // A reviewer who is not assigned to the selected queue gets a 403: that is a
  // normal state, not an error.
  const notAssigned =
    myQueue.error instanceof APIErrorClass &&
    myQueue.error.error.status_code === 403;
  const clearErrors = (): void => mutations.forEach((m) => m.reset());
  const active = activeAssignments(history.data ?? []);
  const view = myQueue.data;
  const stale = view?.queue.stale ?? queue?.stale ?? null;

  return (
    <section className="rounded-xl border border-border bg-card p-5">
      <h2 className="font-medium text-foreground">Screening queues</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Each queue freezes the reports at one approved protocol version.
        Reviewers screen independently and only see their own decisions until a
        report is revealed.
      </p>

      {queues.isLoading ? (
        <p role="status" className="mt-4 text-sm text-muted-foreground">
          Loading screening queues…
        </p>
      ) : (queues.data ?? []).length === 0 ? (
        <p className="mt-4 text-sm text-muted-foreground">
          No screening queues yet.
        </p>
      ) : (
        <ul className="mt-4 divide-y divide-border text-sm">
          {(queues.data ?? []).map((item) => (
            <li key={item.id} className="flex items-center gap-3 py-2">
              <button
                type="button"
                aria-pressed={item.id === queueId}
                aria-label={`Open ${STAGE_LABEL[item.stage]} queue from ${new Date(item.created_at).toLocaleDateString()}`}
                onClick={() => setSelected(item.id)}
                className="rounded-md border border-border px-2 py-1 text-xs aria-pressed:bg-muted"
              >
                {STAGE_LABEL[item.stage]}
              </button>
              <span className="text-muted-foreground">
                {item.report_count} reports · {item.observation_count} decisions
                · {new Date(item.created_at).toLocaleDateString()}
              </span>
              {item.stale && (
                <span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                  stale
                </span>
              )}
            </li>
          ))}
        </ul>
      )}

      {isSupervisor && (
        <div className="mt-4 space-y-3 rounded-md border border-border p-3">
          <h3 className="text-sm font-medium text-foreground">Supervise</h3>
          {/* ponytail: full-text queue creation is API-only until GOO-302
              supplies the included set. */}
          <button
            type="button"
            disabled={!approvedProtocolVersionId || busy}
            onClick={() => {
              clearErrors();
              create.mutate(crypto.randomUUID());
            }}
            className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
          >
            Create title/abstract queue
          </button>
          {!approvedProtocolVersionId && (
            <p className="text-xs text-muted-foreground">
              Approve a protocol before creating a queue.
            </p>
          )}
          {queueId && (
            <>
              <div className="flex gap-2">
                <select
                  aria-label="Reviewer to assign"
                  value={assignee}
                  onChange={(event) => setAssignee(event.target.value)}
                  className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-xs"
                >
                  <option value="">Assign a reviewer…</option>
                  {reviewers.map((reviewer) => (
                    <option key={reviewer} value={reviewer}>
                      {reviewer}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={!assignee || busy}
                  onClick={() => {
                    clearErrors();
                    assign.mutate(crypto.randomUUID());
                  }}
                  className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
                >
                  Assign
                </button>
              </div>
              {active.size > 0 && (
                <div className="space-y-2">
                  <input
                    aria-label="Reason for revoking an assignment"
                    value={revokeReason}
                    onChange={(event) => setRevokeReason(event.target.value)}
                    placeholder="Reason for revoking"
                    className="w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
                  />
                  <ul className="space-y-1 text-xs text-muted-foreground">
                    {[...active].map(([assignmentId, reviewer]) => (
                      <li
                        key={assignmentId}
                        className="flex items-center gap-2"
                      >
                        <span>{reviewer}</span>
                        <button
                          type="button"
                          aria-label={`Revoke assignment of ${reviewer}`}
                          disabled={!revokeReason.trim() || busy}
                          onClick={() => {
                            clearErrors();
                            revoke.mutate({
                              assignmentId,
                              key: crypto.randomUUID(),
                            });
                          }}
                          className="rounded-md border border-border px-2 py-0.5 disabled:opacity-50"
                        >
                          Revoke
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {isReviewer && queueId && (
        <div className="mt-4">
          <h3 className="text-sm font-medium text-foreground">
            My queue
            {view && (
              <span className="ml-2 font-normal text-muted-foreground">
                {view.counts.screened} / {view.counts.total} screened
              </span>
            )}
          </h3>
          {stale && (
            <p
              role="status"
              className="mt-2 rounded-md border border-border bg-muted/50 p-2 text-sm text-muted-foreground"
            >
              This queue is stale: {stale}. A supervisor must reconcile it.
            </p>
          )}
          {myQueue.isLoading && (
            <p role="status" className="mt-2 text-sm text-muted-foreground">
              Loading your queue…
            </p>
          )}
          {notAssigned ? (
            <p className="mt-2 text-sm text-muted-foreground">
              You are not assigned to this queue.
            </p>
          ) : (
            myQueue.error && (
              <p role="alert" className="mt-2 text-sm text-destructive">
                {errorText(myQueue.error)}
              </p>
            )
          )}
          {view && (
            <ul className="mt-2 divide-y divide-border">
              {view.items.map((item) => {
                const title = item.title_snapshot;
                const current = item.observation;
                const fullText = view.queue.stage === 'full_text';
                const reason = reasons[item.report_id] ?? '';
                // Reveal is irreversible: a resolved report needs a reopen.
                const resolved =
                  Boolean(item.resolution) &&
                  item.resolution?.basis !== 'reopened';
                const locked = readOnly || Boolean(stale) || busy || resolved;
                return (
                  <li key={item.report_id} className="space-y-2 py-3 text-sm">
                    <div className="font-medium text-foreground">{title}</div>
                    {item.abstract && (
                      <p className="line-clamp-4 text-muted-foreground">
                        {item.abstract}
                      </p>
                    )}
                    <div className="text-xs text-muted-foreground">
                      {Object.entries(item.identifiers).map(
                        ([kind, values]) => (
                          <span key={kind} className="mr-3">
                            {`${kind}: ${values.join(', ')}`}
                          </span>
                        )
                      )}
                    </div>
                    {current && (
                      <p className="text-xs text-muted-foreground">
                        Your decision: {current.decision}
                        {current.exclusion_reason
                          ? ` (${current.exclusion_reason})`
                          : ''}
                      </p>
                    )}
                    <RevealState item={item} />
                    {fullText && (
                      <select
                        aria-label={`Exclusion reason for ${title}`}
                        value={reason}
                        disabled={locked}
                        onChange={(event) =>
                          setReasons((all) => ({
                            ...all,
                            [item.report_id]: event.target.value,
                          }))
                        }
                        className="rounded-md border border-border bg-background px-2 py-1 text-xs"
                      >
                        <option value="">Exclusion reason…</option>
                        {view.queue.exclusion_reasons.map((option) => (
                          <option key={option} value={option}>
                            {option}
                          </option>
                        ))}
                      </select>
                    )}
                    <textarea
                      aria-label={`Note for ${title}`}
                      value={notes[item.report_id] ?? ''}
                      disabled={locked}
                      onChange={(event) =>
                        setNotes((all) => ({
                          ...all,
                          [item.report_id]: event.target.value,
                        }))
                      }
                      placeholder="Note (optional)"
                      rows={2}
                      className="w-full rounded-md border border-border bg-background px-2 py-1 text-xs"
                    />
                    <div className="flex gap-2">
                      {DECISIONS.map(({ value, label }) => (
                        <button
                          key={value}
                          type="button"
                          title={resolved ? RESOLVED_TITLE : undefined}
                          aria-label={`${current ? 'Change to ' : ''}${label} ${title}`}
                          disabled={
                            locked ||
                            (fullText && value === 'exclude' && !reason)
                          }
                          onClick={() => {
                            clearErrors();
                            submit.mutate({
                              view,
                              item,
                              decision: value,
                              key: crypto.randomUUID(),
                            });
                          }}
                          className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
                        >
                          {current ? `Change: ${label}` : label}
                        </button>
                      ))}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}

      {error && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          {errorText(error)}
        </p>
      )}
    </section>
  );
}
