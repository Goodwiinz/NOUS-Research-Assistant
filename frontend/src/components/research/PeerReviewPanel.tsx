'use client';

/**
 * GOO-314 external peer review for a project's drafts: rounds, reviewers and
 * comments with derived status and anchor state against the viewed version.
 * An `unresolved_anchor` keeps its original quote visible. Authors answer
 * with a change (a later saved version whose anchored diff is shown inline)
 * or a no-change rationale. Only adjudicators see Resolve.
 */

import React, { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, MessageSquare } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth';
import { listProjectRoles } from '@/services/researchEngineService';
import { projectService, type Draft } from '@/services/projectService';
import type {
  ApiAnchorState,
  ApiCommentStatus,
  ApiDiffHunk,
  ApiPeerReviewComment,
  ApiPeerReviewRound,
} from '@/types/api/peer-review-contract';

export interface PassageTarget {
  draftId: string;
  version?: number;
  start: number;
  end: number;
  text: string;
}

const STATUS_TONE: Record<ApiCommentStatus, string> = {
  open: 'bg-destructive/10 text-destructive',
  responded: 'bg-muted text-muted-foreground',
  resolved: 'bg-primary/10 text-primary',
};

const ANCHOR_LABEL: Record<ApiAnchorState, string> = {
  exact: 'Anchored',
  carried: 'Carried',
  unresolved_anchor: 'Unresolved anchor',
  general: 'General',
};

const BUTTON =
  'px-2 py-1 bg-muted border border-border rounded text-xs text-muted-foreground hover:border-primary hover:text-primary transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring';

function roundKey(
  projectId: string,
  roundId: string,
  draftId: string
): string[] {
  return ['project', projectId, 'peer-review', roundId, draftId];
}

const HunkList: React.FC<{
  hunks: ApiDiffHunk[];
  onOpen?: (side: 'old' | 'new', hunk: ApiDiffHunk) => void;
}> = ({ hunks, onOpen }) => (
  <ul aria-label="Anchored diff" className="space-y-1 text-xs">
    {hunks.map((h) => (
      <li key={`${h.old[0]}-${h.new[0]}`} className="space-y-0.5">
        <div className="text-destructive">
          − {h.old_text || '(nothing)'}
          {onOpen && h.old[0] !== h.old[1] && (
            <button
              type="button"
              className="ml-2 underline"
              onClick={() => onOpen('old', h)}
            >
              Old text
            </button>
          )}
        </div>
        <div className="text-primary">
          + {h.new_text || '(removed)'}
          {onOpen && h.new[0] !== h.new[1] && (
            <button
              type="button"
              className="ml-2 underline"
              onClick={() => onOpen('new', h)}
            >
              New text
            </button>
          )}
        </div>
      </li>
    ))}
  </ul>
);

const ResponseForm: React.FC<{
  projectId: string;
  round: ApiPeerReviewRound;
  comment: ApiPeerReviewComment;
  drafts: Array<Pick<Draft, 'id' | 'version'>>;
  onDone: () => void;
}> = ({ projectId, round, comment, drafts, onDone }) => {
  const [kind, setKind] = useState<'change' | 'no_change'>('no_change');
  const [body, setBody] = useState('');
  const [rationale, setRationale] = useState('');
  const [revised, setRevised] = useState('');
  const [evidence, setEvidence] = useState<string[]>([]);
  const later = drafts.filter((d) => d.version > round.draft_version);
  const diff = useQuery({
    enabled: kind === 'change' && Boolean(revised),
    queryKey: ['project', projectId, 'draft-diff', round.draft_id, revised],
    queryFn: () =>
      projectService.diffDrafts(projectId, round.draft_id, revised),
  });
  const claims = useQuery({
    queryKey: ['project', projectId, 'claims', round.draft_id],
    queryFn: () => projectService.listClaims(projectId, round.draft_id),
    retry: false,
  });
  const respond = useMutation({
    mutationFn: () =>
      projectService.respondToPeerReviewComment(
        projectId,
        comment.comment_root_id,
        {
          kind,
          body,
          revised_draft_id: kind === 'change' ? revised : null,
          rationale: kind === 'no_change' ? rationale : null,
          evidence_claim_version_ids: evidence,
          supersedes_response_id: comment.response?.id ?? null,
          idempotency_key: crypto.randomUUID(),
        }
      ),
    onSuccess: onDone,
  });
  const missingRationale = kind === 'no_change' && !rationale.trim();
  const invalid =
    !body.trim() || missingRationale || (kind === 'change' && !revised);

  return (
    <form
      aria-label={`Respond to comment ${comment.number}`}
      className="space-y-2 border-t border-border pt-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (!invalid) respond.mutate();
      }}
    >
      <fieldset className="flex gap-3 text-xs">
        <legend className="sr-only">Response kind</legend>
        {(['change', 'no_change'] as const).map((value) => (
          <label key={value} className="flex items-center gap-1">
            <input
              type="radio"
              name={`kind-${comment.comment_root_id}`}
              checked={kind === value}
              onChange={() => setKind(value)}
            />
            {value === 'change' ? 'Change' : 'No change'}
          </label>
        ))}
      </fieldset>
      <label className="block text-xs">
        Response
        <textarea
          className="mt-1 w-full rounded border border-border bg-background p-1"
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
      </label>
      {kind === 'change' ? (
        <>
          <label className="block text-xs">
            Revised version
            <select
              className="mt-1 w-full rounded border border-border bg-background p-1"
              value={revised}
              onChange={(e) => setRevised(e.target.value)}
            >
              <option value="">Pick a saved later version</option>
              {later.map((d) => (
                <option key={d.id} value={d.id}>
                  v{d.version}
                </option>
              ))}
            </select>
          </label>
          {diff.data && <HunkList hunks={diff.data.hunks} />}
        </>
      ) : (
        <label className="block text-xs">
          Rationale (required)
          <textarea
            className="mt-1 w-full rounded border border-border bg-background p-1"
            value={rationale}
            aria-invalid={missingRationale}
            onChange={(e) => setRationale(e.target.value)}
          />
          {missingRationale && (
            <span role="alert" className="text-destructive">
              A no-change response needs a rationale.
            </span>
          )}
        </label>
      )}
      {(claims.data?.items.length ?? 0) > 0 && (
        <fieldset className="text-xs">
          <legend>Evidence claims</legend>
          {claims.data?.items.map((c) => (
            <label key={c.version.id} className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={evidence.includes(c.version.id)}
                onChange={(e) =>
                  setEvidence((ids) =>
                    e.target.checked
                      ? [...ids, c.version.id]
                      : ids.filter((id) => id !== c.version.id)
                  )
                }
              />
              {c.version.text}
            </label>
          ))}
        </fieldset>
      )}
      <button
        type="submit"
        className={BUTTON}
        disabled={invalid || respond.isPending}
      >
        Submit response
      </button>
      {respond.isError && (
        <p role="alert" className="text-xs text-destructive">
          {respond.error instanceof Error
            ? respond.error.message
            : 'Response failed; reload'}
        </p>
      )}
    </form>
  );
};

const CommentCard: React.FC<{
  projectId: string;
  round: ApiPeerReviewRound;
  comment: ApiPeerReviewComment;
  drafts: Array<Pick<Draft, 'id' | 'version'>>;
  canResolve: boolean;
  onChanged: () => void;
  onOpenPassage?: (target: PassageTarget) => void;
}> = ({
  projectId,
  round,
  comment,
  drafts,
  canResolve,
  onChanged,
  onOpenPassage,
}) => {
  const [responding, setResponding] = useState(false);
  const response = comment.response;
  const resolve = useMutation({
    mutationFn: () =>
      projectService.decidePeerReviewComment(
        projectId,
        comment.comment_root_id,
        {
          kind: 'resolved',
          response_id: response?.id ?? null,
          supersedes_decision_id: comment.resolution?.id ?? null,
          idempotency_key: crypto.randomUUID(),
        }
      ),
    onSuccess: onChanged,
  });
  const version = (id?: string | null): number | undefined =>
    drafts.find((d) => d.id === id)?.version;
  const open = (side: 'old' | 'new', h: ApiDiffHunk): void => {
    const draftId =
      side === 'old' ? response?.base_draft_id : response?.revised_draft_id;
    const [start, end] = side === 'old' ? h.old : h.new;
    if (draftId && onOpenPassage) {
      onOpenPassage({
        draftId,
        version: version(draftId),
        start,
        end,
        text: side === 'old' ? h.old_text : h.new_text,
      });
    }
  };

  return (
    <li className="rounded border border-border p-2 space-y-1 text-xs">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-medium text-foreground">
          Comment {comment.number}
        </span>
        <span
          aria-label={`Status: ${comment.status}`}
          className={`px-1.5 py-0.5 rounded ${STATUS_TONE[comment.status]}`}
        >
          {comment.status}
        </span>
        <span
          aria-label={`Anchor: ${ANCHOR_LABEL[comment.anchor_state]}`}
          className={`px-1.5 py-0.5 rounded ${
            comment.anchor_state === 'unresolved_anchor'
              ? 'bg-destructive/10 text-destructive'
              : 'bg-muted text-muted-foreground'
          }`}
        >
          {comment.anchor_state === 'unresolved_anchor' && (
            <AlertTriangle aria-hidden className="inline h-3 w-3 mr-0.5" />
          )}
          {ANCHOR_LABEL[comment.anchor_state]}
        </span>
      </div>
      <p className="text-foreground">{comment.current.body}</p>
      {comment.current.quote && (
        <blockquote className="border-l-2 border-border pl-2 text-muted-foreground">
          {comment.anchor_state === 'unresolved_anchor' && (
            <span role="status" className="block text-destructive">
              The commented passage no longer appears once in this version.
              Original quote (v{round.draft_version}):
            </span>
          )}
          “{comment.current.quote}”
        </blockquote>
      )}
      {response && (
        <div className="space-y-1">
          <p>
            <span className="font-medium text-foreground">
              Response ({response.kind === 'change' ? 'change' : 'no change'}):
            </span>{' '}
            {response.body}
          </p>
          {response.kind === 'no_change' && (
            <p>Rationale: {response.rationale}</p>
          )}
          {response.kind === 'change' && (
            <HunkList hunks={response.hunks ?? []} onOpen={open} />
          )}
          {(response.evidence ?? []).map((e) => (
            <p key={e.claim_version_id}>
              Evidence: {e.text} ({e.assessment_stance ?? 'unassessed'})
            </p>
          ))}
        </div>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          className={BUTTON}
          onClick={() => setResponding((v) => !v)}
        >
          {response ? 'Revise response' : 'Respond'}
        </button>
        {canResolve && response && comment.status !== 'resolved' && (
          <button
            type="button"
            className={BUTTON}
            disabled={resolve.isPending}
            onClick={() => resolve.mutate()}
          >
            Resolve
          </button>
        )}
      </div>
      {responding && (
        <ResponseForm
          projectId={projectId}
          round={round}
          comment={comment}
          drafts={drafts}
          onDone={() => {
            setResponding(false);
            onChanged();
          }}
        />
      )}
    </li>
  );
};

const RoundSection: React.FC<{
  projectId: string;
  round: ApiPeerReviewRound;
  draftId: string;
  drafts: Array<Pick<Draft, 'id' | 'version'>>;
  canResolve: boolean;
  onOpenPassage?: (target: PassageTarget) => void;
}> = ({ projectId, round, draftId, drafts, canResolve, onOpenPassage }) => {
  const queryClient = useQueryClient();
  const key = roundKey(projectId, round.id, draftId);
  const detail = useQuery({
    queryKey: key,
    queryFn: () =>
      projectService.getPeerReviewRound(projectId, round.id, draftId),
  });
  const reviewers = [
    ...new Set((detail.data?.comments ?? []).map((c) => c.reviewer_label)),
  ];
  const onChanged = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: key });

  return (
    <section aria-label={`Round ${round.label}`} className="space-y-2">
      <div className="flex items-center gap-2">
        <h4 className="text-xs font-semibold text-foreground">
          {round.label} — reviewed v{round.draft_version}
        </h4>
        {(['markdown', 'json'] as const).map((format) => (
          <button
            key={format}
            type="button"
            className={BUTTON}
            onClick={() =>
              void projectService.downloadPeerReviewExport(
                projectId,
                round.id,
                format
              )
            }
          >
            Export {format === 'markdown' ? 'MD' : 'JSON'}
          </button>
        ))}
      </div>
      {reviewers.map((label) => (
        <div key={label} className="space-y-1">
          <h5 className="text-xs text-muted-foreground">{label}</h5>
          <ul className="space-y-1">
            {detail.data?.comments
              .filter((c) => c.reviewer_label === label)
              .map((c) => (
                <CommentCard
                  key={c.comment_root_id}
                  projectId={projectId}
                  round={round}
                  comment={c}
                  drafts={drafts}
                  canResolve={canResolve}
                  onChanged={onChanged}
                  onOpenPassage={onOpenPassage}
                />
              ))}
          </ul>
        </div>
      ))}
    </section>
  );
};

interface PeerReviewPanelProps {
  projectId: string;
  draft: Pick<Draft, 'id' | 'version'>;
  onOpenPassage?: (target: PassageTarget) => void;
}

export const PeerReviewPanel: React.FC<PeerReviewPanelProps> = ({
  projectId,
  draft,
  onOpenPassage,
}) => {
  const userId = useAuth().user?.id;
  const rounds = useQuery({
    queryKey: ['project', projectId, 'peer-review', 'rounds'],
    queryFn: () => projectService.listPeerReviewRounds(projectId),
    retry: false,
  });
  const drafts = useQuery({
    enabled: (rounds.data?.rounds.length ?? 0) > 0,
    queryKey: ['project', projectId, 'drafts', 'versions'],
    queryFn: () => projectService.listDrafts(projectId),
  });
  const { data: roles } = useQuery({
    queryKey: ['project', projectId, 'research-engine', 'roles'],
    queryFn: () => listProjectRoles(projectId),
    retry: false,
  });
  const canResolve = (roles ?? []).some(
    (r) => r.user_id === userId && r.role === 'adjudicator'
  );
  const list = rounds.data?.rounds ?? [];
  if (list.length === 0) return null;

  return (
    <div className="px-4 py-2 border-b border-border space-y-2">
      <h3 className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
        <MessageSquare aria-hidden className="h-3 w-3" />
        Peer review
      </h3>
      {list.map((round) => (
        <RoundSection
          key={round.id}
          projectId={projectId}
          round={round}
          draftId={draft.id}
          drafts={drafts.data?.drafts ?? []}
          canResolve={canResolve}
          onOpenPassage={onOpenPassage}
        />
      ))}
    </div>
  );
};

export default PeerReviewPanel;
