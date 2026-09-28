'use client';

import { useMemo, useState, type ReactElement } from 'react';
import { AlertCircle, CheckCircle2, Loader2, Play } from 'lucide-react';
import {
  resumeRun,
  submitReview,
  type PendingReviewResponse,
  type StageReviewRequest,
} from '@/services/researchEngineService';
import { APIErrorClass } from '@/types/api';

export interface ReviewSourceRecord {
  source_id: string;
  title?: string;
  authors?: string[];
  abstract?: string | null;
  evidence_level?: string;
  venue?: string | null;
  published_at?: string | null;
  publication_year?: number | string | null;
  year?: number | string | null;
  doi?: string | null;
  url?: string | null;
  [key: string]: unknown;
}

interface ReviewPanelProps {
  runId: string;
  review: PendingReviewResponse;
  sourceRecords: ReviewSourceRecord[];
  onRefresh: () => void | Promise<void>;
}

type DraftDecision = {
  source_id: string;
  part_id: string;
  decision: string;
  reason: string;
};

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.flatMap((item) => {
        const parsed = record(item);
        return parsed ? [parsed] : [];
      })
    : [];
}

function text(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function displayEvidenceLevel(level?: string): string {
  return (level ?? 'unknown').replace(/_/g, ' ');
}

function publicationYear(source?: ReviewSourceRecord): string | undefined {
  const explicit = source?.publication_year ?? source?.year;
  if (typeof explicit === 'number') return String(explicit);
  if (typeof explicit === 'string' && explicit.trim()) return explicit;
  const publishedAt = source?.published_at;
  if (typeof publishedAt !== 'string') return undefined;
  const match = /^\d{4}/.exec(publishedAt);
  return match?.[0];
}

function initialDrafts(review: PendingReviewResponse): DraftDecision[] {
  const descriptor = review.descriptor;
  const output = record(review.stage_output);
  if (!descriptor || !output) return [];
  const key =
    descriptor.review_kind === 'screening' ? 'screening' : 'extractions';
  const acceptedPayload = record(review.accepted_review?.decision_payload);
  const acceptedItems = records(acceptedPayload?.items);
  const acceptedByIdentity = new Map(
    acceptedItems.map((item) => [
      `${text(item.source_id)}:${text(item.part_id)}`,
      item,
    ])
  );

  return records(output[key]).flatMap((item) => {
    const sourceId = text(item.source_id);
    const partId = text(item.part_id);
    if (!sourceId || !partId) return [];
    const accepted = acceptedByIdentity.get(`${sourceId}:${partId}`);
    return [
      {
        source_id: sourceId,
        part_id: partId,
        decision: text(accepted?.decision) ?? 'unresolved',
        reason: text(accepted?.reason) ?? '',
      },
    ];
  });
}

function finalStatus(review: PendingReviewResponse): string | undefined {
  const output = record(review.stage_output);
  return (
    text(record(output?.exported)?.final_status) ??
    text(record(output?.artifact)?.final_status) ??
    text(output?.final_status)
  );
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return 'Unable to display this persisted record.';
  }
}

export function ReviewPanel({
  runId,
  review,
  sourceRecords,
  onRefresh,
}: ReviewPanelProps): ReactElement | null {
  const descriptor = review.descriptor;
  const [drafts, setDrafts] = useState<DraftDecision[]>(() =>
    initialDrafts(review)
  );
  const [submitting, setSubmitting] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState<{
    label: string;
    message: string;
  } | null>(null);

  const sourcesById = useMemo(
    () => new Map(sourceRecords.map((source) => [source.source_id, source])),
    [sourceRecords]
  );
  const stageOutput = record(review.stage_output);
  const reviewItems = descriptor
    ? records(
        stageOutput?.[
          descriptor.review_kind === 'screening' ? 'screening' : 'extractions'
        ]
      )
    : [];
  const requiredDecision =
    descriptor?.review_kind === 'screening' ? 'exclude' : 'reject';
  const approvalBlocked =
    descriptor?.review_kind === 'final'
      ? finalStatus(review) !== 'verified'
      : drafts.length === 0 ||
        drafts.some(
          (draft) =>
            draft.decision === 'unresolved' ||
            (draft.decision === requiredDecision && !draft.reason.trim())
        );

  if (!review.pending || !descriptor || !stageOutput) return null;

  const title =
    descriptor.review_kind === 'screening'
      ? 'Screening review'
      : descriptor.review_kind === 'extraction'
        ? 'Extraction review'
        : 'Final review';

  const updateDraft = (
    sourceId: string,
    partId: string,
    update: Partial<DraftDecision>
  ): void => {
    setDrafts((current) =>
      current.map((draft) =>
        draft.source_id === sourceId && draft.part_id === partId
          ? { ...draft, ...update }
          : draft
      )
    );
  };

  const payload = (): Record<string, unknown> => {
    if (descriptor.review_kind === 'final') return {};
    return {
      items: drafts.map(({ source_id, part_id, decision, reason }) => ({
        source_id,
        part_id,
        decision,
        ...(reason.trim() ? { reason: reason.trim() } : {}),
      })),
    };
  };

  const submit = async (decision: 'approve' | 'decline'): Promise<void> => {
    setError(null);
    setSubmitting(true);
    const request = {
      review_kind: descriptor.review_kind,
      output_hash: descriptor.output_hash,
      decision,
      decision_payload: payload(),
    } as StageReviewRequest;
    try {
      await submitReview(runId, descriptor.step_index, request);
    } catch (caught) {
      if (caught instanceof APIErrorClass && caught.error.status_code === 409) {
        let refreshed = true;
        try {
          await onRefresh();
        } catch {
          refreshed = false;
        }
        setError({
          label: 'Review changed',
          message: refreshed
            ? 'This review changed on the server. The current review has been refreshed.'
            : 'This review changed on the server. Reload the page to get the current review.',
        });
      } else {
        setError({
          label: 'Review submission failed',
          message: 'The review could not be submitted. Please try again.',
        });
      }
      setSubmitting(false);
      return;
    }

    try {
      await onRefresh();
    } catch {
      setError({
        label: 'Review accepted',
        message:
          'The server accepted the review, but the latest run state could not be loaded. Reload the page before continuing.',
      });
    } finally {
      setSubmitting(false);
    }
  };

  const resumeApproved = async (): Promise<void> => {
    setError(null);
    setResuming(true);
    try {
      await resumeRun(runId);
      await onRefresh();
    } catch {
      setError({
        label: 'Resume failed',
        message: 'The approved run could not be resumed. Please try again.',
      });
    } finally {
      setResuming(false);
    }
  };

  const accepted = review.accepted_review;
  const finalCanNeverBeApproved =
    descriptor.review_kind === 'final' && finalStatus(review) !== 'verified';

  return (
    <section
      aria-labelledby="run-review-title"
      className="mb-6 rounded-xl border border-primary/30 bg-card p-5 shadow-xs"
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2
            id="run-review-title"
            className="text-lg font-semibold text-foreground"
          >
            {title}
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Review the original persisted output before deciding.
          </p>
        </div>
      </div>

      <p className="mt-4 rounded-lg border border-border bg-muted/40 p-3 text-sm text-muted-foreground">
        Choices on this page are drafts until you submit them. Reloading
        restores only the state accepted by the server.
      </p>

      {error && (
        <div
          role="alert"
          aria-label={error.label}
          className="mt-4 flex gap-2 rounded-lg border border-(--nous-mars)/30 bg-(--nous-mars)/10 p-3 text-sm text-foreground"
        >
          <AlertCircle
            aria-hidden="true"
            className="mt-0.5 h-4 w-4 shrink-0 text-(--nous-mars)"
          />
          {error.message}
        </div>
      )}

      {finalCanNeverBeApproved && (
        <div
          role="alert"
          className="mt-4 rounded-lg border border-(--nous-mars)/30 bg-(--nous-mars)/10 p-3 text-sm text-foreground"
        >
          This artifact cannot receive final approval because verification did
          not finish with a verified result.
        </div>
      )}

      {descriptor.review_kind === 'final' && (
        <div className="mt-4 space-y-3">
          <div className="rounded-lg border border-border bg-muted/30 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Persisted verified brief
            </p>
            <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap text-sm text-foreground">
              {text(stageOutput.markdown) ?? text(stageOutput.content) ?? ''}
            </pre>
          </div>
          {record(stageOutput.exported)?.claims !== undefined && (
            <details className="rounded-lg border border-border p-3">
              <summary className="cursor-pointer text-sm font-medium">
                Claim coverage and checks
              </summary>
              <pre className="mt-2 overflow-auto whitespace-pre-wrap text-xs">
                {safeJson(record(stageOutput.exported)?.claims)}
              </pre>
            </details>
          )}
        </div>
      )}

      {descriptor.review_kind !== 'final' && (
        <div className="mt-4 space-y-4">
          {reviewItems.map((item) => {
            const sourceId = text(item.source_id) ?? '';
            const partId = text(item.part_id) ?? '';
            const source = sourcesById.get(sourceId);
            const sourceTitle = source?.title || sourceId;
            const draft = drafts.find(
              (candidate) =>
                candidate.source_id === sourceId && candidate.part_id === partId
            );
            if (!draft) return null;
            const isScreening = descriptor.review_kind === 'screening';
            const decisions = isScreening
              ? ['include', 'exclude', 'unresolved']
              : ['accept', 'reject', 'unresolved'];
            return (
              <fieldset
                key={`${sourceId}:${partId}`}
                aria-label={
                  isScreening
                    ? sourceTitle
                    : `Extraction from ${sourceTitle}, part ${partId}`
                }
                className="rounded-xl border border-border p-4"
              >
                <legend className="px-1 text-sm font-semibold text-foreground">
                  {sourceTitle}
                </legend>
                <div className="space-y-2 text-sm">
                  <p className="text-muted-foreground">
                    Evidence level:{' '}
                    {displayEvidenceLevel(source?.evidence_level)}
                  </p>
                  {isScreening ? (
                    <>
                      {source?.authors && source.authors.length > 0 && (
                        <p className="text-muted-foreground">
                          Authors: {source.authors.join(', ')}
                        </p>
                      )}
                      {source?.venue && (
                        <p className="text-muted-foreground">
                          Venue: {source.venue}
                        </p>
                      )}
                      {publicationYear(source) && (
                        <p className="text-muted-foreground">
                          Published: {publicationYear(source)}
                        </p>
                      )}
                      {source?.doi && (
                        <p className="text-muted-foreground">
                          DOI: {source.doi}
                        </p>
                      )}
                      <p className="text-foreground">
                        {source?.abstract || 'Abstract is not available.'}
                      </p>
                      <p className="text-muted-foreground">
                        Machine recommendation:{' '}
                        {item.included === true ? 'include' : 'exclude'}
                        {text(item.reason) ? ` — ${text(item.reason)}` : ''}
                      </p>
                    </>
                  ) : (
                    <>
                      <pre className="overflow-auto whitespace-pre-wrap rounded-lg bg-muted/40 p-3 text-xs text-foreground">
                        {safeJson(item.data)}
                      </pre>
                      {records(item.evidence).map((evidence, index) => (
                        <blockquote
                          key={`${partId}-evidence-${index}`}
                          className="border-l-2 border-primary/40 pl-3 text-foreground"
                        >
                          <p>
                            {text(evidence.quote) ?? 'No quotation supplied.'}
                          </p>
                          {text(evidence.page_reference) && (
                            <cite className="mt-1 block text-xs not-italic text-muted-foreground">
                              Page {text(evidence.page_reference)}
                            </cite>
                          )}
                        </blockquote>
                      ))}
                    </>
                  )}
                </div>

                <div className="mt-4 flex flex-wrap gap-4">
                  {decisions.map((decision) => (
                    <label
                      key={decision}
                      className="flex items-center gap-2 text-sm capitalize text-foreground"
                    >
                      <input
                        type="radio"
                        name={`decision-${sourceId}-${partId}`}
                        value={decision}
                        checked={draft.decision === decision}
                        onChange={() =>
                          updateDraft(sourceId, partId, { decision })
                        }
                        className="h-4 w-4 accent-primary"
                      />
                      {decision}
                    </label>
                  ))}
                </div>

                {draft.decision === requiredDecision && (
                  <label className="mt-3 block text-sm font-medium text-foreground">
                    {isScreening
                      ? 'Reason for exclusion'
                      : 'Reason for rejection'}
                    <textarea
                      value={draft.reason}
                      onChange={(event) =>
                        updateDraft(sourceId, partId, {
                          reason: event.target.value,
                        })
                      }
                      rows={2}
                      required
                      className="mt-1 block w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
                    />
                  </label>
                )}
              </fieldset>
            );
          })}
        </div>
      )}

      {accepted ? (
        <div className="mt-5 rounded-lg border border-border bg-muted/30 p-4">
          <div className="flex items-center gap-2 text-sm font-medium text-foreground">
            <CheckCircle2
              aria-hidden="true"
              className="h-4 w-4 text-(--nous-terra)"
            />
            Server accepted this {accepted.decision} decision.
          </div>
          {accepted.decision === 'approve' ? (
            <button
              type="button"
              onClick={resumeApproved}
              disabled={resuming}
              className="mt-3 inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50"
            >
              {resuming ? (
                <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
              ) : (
                <Play aria-hidden="true" className="h-4 w-4" />
              )}
              Resume approved run
            </button>
          ) : (
            <p className="mt-2 text-sm text-muted-foreground">
              This run remains paused. Start a new run after revising the
              blueprint.
            </p>
          )}
        </div>
      ) : (
        <div className="mt-5 flex flex-wrap gap-3">
          {!finalCanNeverBeApproved && (
            <button
              type="button"
              onClick={() => submit('approve')}
              disabled={approvalBlocked || submitting}
              className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submitting && (
                <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin" />
              )}
              Approve {descriptor.review_kind} review
            </button>
          )}
          <button
            type="button"
            onClick={() => submit('decline')}
            disabled={submitting}
            className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50"
          >
            Decline review
          </button>
        </div>
      )}
    </section>
  );
}
