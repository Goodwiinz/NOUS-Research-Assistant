'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { ReactElement } from 'react';

import { useAuth } from '@/hooks/useAuth';
import { integrationActionService } from '@/services/integrationActionService';
import type {
  ActionState,
  ApiActionReview,
} from '@/types/api/integration-action-contract';

// A note keeps its own wording; every other action is a library change.
const NOTE_TOOL = 'create_project_note';
type Kind = 'note' | 'change';

const STATE_TEXT: Record<Kind, Record<ActionState, string>> = {
  note: {
    awaiting_approval:
      'Waiting for your decision. Nothing has been created yet.',
    approved: 'Approved. NOUS is creating the note.',
    executing: 'Approved. NOUS is creating the note.',
    succeeded: 'Created.',
    failed: 'Not created.',
    outcome_unknown:
      'NOUS could not confirm whether the note was created. Check the project notes before asking again.',
  },
  change: {
    awaiting_approval: 'Waiting for your decision. Nothing has changed yet.',
    approved: 'Approved. NOUS is making the change.',
    executing: 'Approved. NOUS is making the change.',
    succeeded: 'Done.',
    failed: 'Not done.',
    outcome_unknown:
      'NOUS could not confirm whether the change was made. Check your library before asking again.',
  },
};
// Until the request loads the page cannot tell which kind it is.
const HEADING: Record<Kind | 'loading', string> = {
  note: 'Approve a note',
  change: 'Approve a library change',
  loading: 'Approve a request',
};
const INTRO: Record<Kind | 'loading', string> = {
  note: 'A connected coding session asked to create this note. Approve only if you expect it.',
  change:
    'A connected coding session asked to make this change to your library. Approve only if you expect it.',
  loading:
    'A connected coding session asked for this. Approve only if you expect it.',
};
const APPROVE: Record<Kind, string> = {
  note: 'Approve and create note',
  change: 'Approve',
};
const IN_FLIGHT = new Set<ActionState>(['approved', 'executing']);

// Bidi overrides/isolates, zero-width and BOM characters can make stored text
// read differently from what will be written; show each one as a visible code.
const HIDDEN = /[​-‏‪-‮⁠-⁩﻿]/g;
export function revealHidden(value: string): string {
  return value.replace(
    HIDDEN,
    (ch) =>
      `⟨U+${(ch.codePointAt(0) ?? 0).toString(16).toUpperCase().padStart(4, '0')}⟩`
  );
}

/**
 * A project action names its project (a folder, for a library change); a
 * workspace-level one, its workspace.
 */
function TargetRow({ action }: { action: ApiActionReview }): ReactElement {
  const inWorkspace = action.project_label === null;
  const project = action.tool_name === NOTE_TOOL ? 'Project' : 'Folder';
  const kind = inWorkspace ? 'Workspace' : project;
  const label = inWorkspace ? action.workspace_label : action.project_label;
  const id = inWorkspace ? action.workspace_id : action.project_id;
  return (
    <div>
      <dt className="font-semibold">{kind}</dt>
      <dd>
        {revealHidden(label ?? '')} ({id})
        {!action.project_available && (
          <span role="alert"> This {kind.toLowerCase()} was deleted.</span>
        )}
      </dd>
    </div>
  );
}

/** The note to create: its title, tags and content, as stored. */
function NoteRows({ action }: { action: ApiActionReview }): ReactElement {
  return (
    <>
      <div>
        <dt className="font-semibold">Title</dt>
        <dd>{revealHidden(action.title)}</dd>
      </div>
      {action.tags.length > 0 && (
        <div>
          <dt className="font-semibold">Tags</dt>
          <dd>
            {/* One quoted item per tag so a comma inside a tag stays visible. */}
            <ul className="flex flex-wrap gap-2">
              {action.tags.map((tag, index) => (
                <li key={`${index}:${tag}`} className="rounded border px-2">
                  “{revealHidden(tag)}”
                </li>
              ))}
            </ul>
          </dd>
        </div>
      )}
      <div>
        <dt className="font-semibold">
          Content ({action.content.length.toLocaleString()} characters)
        </dt>
        <dd>
          <pre
            tabIndex={0}
            aria-label="Note content"
            className="max-h-96 overflow-auto whitespace-pre-wrap rounded border p-3 text-sm"
          >
            {revealHidden(action.content)}
          </pre>
        </dd>
      </div>
    </>
  );
}

const storedText = (value: unknown): string =>
  typeof value === 'string' ? value : JSON.stringify(value);

/** One stored value as plain text; a list as one quoted item per entry. */
function StoredValue({ value }: { value: unknown }): ReactElement {
  if (!Array.isArray(value)) {
    return <>{revealHidden(storedText(value))}</>;
  }
  if (value.length === 0) {
    return <>(empty)</>;
  }
  return (
    <ul className="flex flex-wrap gap-2">
      {value.map((item, index) => {
        const text = storedText(item);
        return (
          <li key={`${index}:${text}`} className="rounded border px-2">
            “{revealHidden(text)}”
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The stored arguments exactly as the action will run them, under their own
 * keys, so nothing the summary leaves out is hidden.
 */
function ArgumentRows({ action }: { action: ApiActionReview }): ReactElement {
  return (
    <div>
      <dt className="font-semibold">Arguments</dt>
      <dd>
        <dl className="space-y-1 font-mono text-sm">
          {Object.entries(action.arguments).map(([key, value]) => (
            <div key={key} className="flex flex-wrap gap-2">
              <dt>{key}</dt>
              <dd>
                <StoredValue value={value} />
              </dd>
            </div>
          ))}
        </dl>
      </dd>
    </div>
  );
}

/**
 * Shows the exact action a connected harness asked for (a note to create, or
 * a change to the library) and lets the requester approve or deny it once.
 * The server's one-sentence summary leads; everything is shown as plain text
 * so formatting cannot hide what will be written.
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
  let kind: Kind | undefined;
  if (action) {
    kind = action.tool_name === NOTE_TOOL ? 'note' : 'change';
  }
  // One decision per page: a second click, even after an error, must not
  // race the first; the refetch shows the stored outcome.
  const canDecide = action?.state === 'awaiting_approval' && decision.isIdle;
  const canApprove = canDecide && action?.project_available === true;

  return (
    <section className="mx-auto max-w-2xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">{HEADING[kind ?? 'loading']}</h1>
      <p>{INTRO[kind ?? 'loading']}</p>
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
      {action && kind && (
        <>
          <p className="break-words text-lg font-medium">
            {revealHidden(action.summary)}
          </p>
          <dl className="space-y-3 break-words">
            <TargetRow action={action} />
            {kind === 'note' ? (
              <NoteRows action={action} />
            ) : (
              <ArgumentRows action={action} />
            )}
          </dl>
          <p role="status">
            {STATE_TEXT[kind][action.state]}
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
              disabled={!canApprove}
              onClick={() => decision.mutate(true)}
              className="rounded bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
            >
              {APPROVE[kind]}
            </button>
          </div>
        </>
      )}
    </section>
  );
}
