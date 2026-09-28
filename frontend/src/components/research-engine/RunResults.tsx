import type { ReactElement } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FileQuestion,
} from 'lucide-react';
import {
  getRunExportUrl,
  type RunResponse,
  type StepResponse,
} from '@/services/researchEngineService';
import type { RunStepState } from '@/store/research-engine-store';

type FinalStatus = 'verified' | 'unverified' | 'no_evidence';
type ResultStep = StepResponse | RunStepState;

interface RunResultsProps {
  run: RunResponse;
  steps: ResultStep[];
  finalStatus?: FinalStatus | null;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function text(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.flatMap((item) => {
        const parsed = record(item);
        return parsed ? [parsed] : [];
      })
    : [];
}

function reviewItems(
  artifact: Record<string, unknown>,
  kind: 'screening' | 'extraction'
): Record<string, unknown>[] {
  const review = records(artifact.reviews)
    .reverse()
    .find((item) => item.review_kind === kind && item.decision === 'approve');
  return records(record(review?.decision_payload)?.items);
}

function countLabel(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? '' : 's'}`;
}

function resultMetrics(artifact: Record<string, unknown>): string[] {
  const metrics: string[] = [];
  const coverage = record(artifact.coverage);
  const providers = record(coverage?.providers);
  if (providers && Object.keys(providers).length > 0) {
    const values = Object.values(providers).map(record);
    const succeeded = values.filter((item) => item?.status === 'ok').length;
    metrics.push(`${succeeded} of ${values.length} providers succeeded`);
  }

  const screeningDecisions = reviewItems(artifact, 'screening');
  let included = screeningDecisions.filter(
    (item) => item.decision === 'include'
  ).length;
  let excluded = screeningDecisions.filter(
    (item) => item.decision === 'exclude'
  ).length;
  if (screeningDecisions.length === 0) {
    const includedIds = new Set(stringList(artifact.included_source_ids));
    const screenedIds = new Set(
      records(artifact.screening).flatMap((item) =>
        typeof item.source_id === 'string' ? [item.source_id] : []
      )
    );
    included = includedIds.size;
    excluded = [...screenedIds].filter((id) => !includedIds.has(id)).length;
  }
  if (included + excluded > 0) {
    metrics.push(countLabel(included, 'included source'));
    metrics.push(countLabel(excluded, 'excluded source'));
  }

  const extractionDecisions = reviewItems(artifact, 'extraction');
  if (extractionDecisions.length > 0) {
    metrics.push(
      countLabel(
        extractionDecisions.filter((item) => item.decision === 'accept').length,
        'accepted extraction'
      )
    );
    metrics.push(
      countLabel(
        extractionDecisions.filter((item) => item.decision === 'reject').length,
        'rejected extraction'
      )
    );
  }

  const claims = records(record(artifact.verification)?.claims);
  if (claims.length > 0) {
    const verified = claims.filter(
      (claim) => claim.status === 'supported'
    ).length;
    metrics.push(`${verified} of ${claims.length} claims verified`);
  }
  return metrics;
}

function outputOf(step: ResultStep): Record<string, unknown> | null {
  return record(step.output);
}

function inferFinalStatus(
  steps: ResultStep[],
  explicit?: FinalStatus | null
): FinalStatus | null {
  if (explicit) return explicit;
  const exportOutput = outputOf(
    [...steps].reverse().find((step) => step.step_type === 'export') ??
      ({} as ResultStep)
  );
  const exported =
    record(exportOutput?.exported) ?? record(exportOutput?.artifact);
  const exportedStatus =
    text(exported?.final_status) ?? text(exportOutput?.final_status);
  if (
    exportedStatus === 'verified' ||
    exportedStatus === 'unverified' ||
    exportedStatus === 'no_evidence'
  ) {
    return exportedStatus;
  }

  const noEvidence = steps.some((step) => {
    const output = outputOf(step);
    if (!output) return false;
    if (text(output.final_status) === 'no_evidence') return true;
    if (
      step.step_type === 'screen' &&
      Array.isArray(output.included_source_ids)
    ) {
      return output.included_source_ids.length === 0;
    }
    return false;
  });
  return noEvidence ? 'no_evidence' : null;
}

function DownloadLink({
  runId,
  format,
  label,
}: {
  runId: string;
  format: 'markdown' | 'json' | 'csv';
  label: string;
}): ReactElement {
  return (
    <a
      href={getRunExportUrl(runId, format)}
      className="inline-flex items-center gap-2 rounded-lg border border-border bg-background px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-muted focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
    >
      <Download aria-hidden="true" className="h-4 w-4" />
      {label}
    </a>
  );
}

export function RunResults({
  run,
  steps,
  finalStatus,
}: RunResultsProps): ReactElement | null {
  if (run.status !== 'completed') return null;
  const status = inferFinalStatus(steps, finalStatus);
  if (!status) return null;

  const exportOutput = outputOf(
    [...steps].reverse().find((step) => step.step_type === 'export') ??
      ({} as ResultStep)
  );
  const artifact =
    record(exportOutput?.exported) ?? record(exportOutput?.artifact) ?? {};
  const limitations = stringList(artifact.limitations);
  const metrics = resultMetrics(artifact);
  const noEvidenceStep = [...steps]
    .reverse()
    .find((step) => ['screen', 'extract'].includes(step.step_type));
  const noEvidenceReason =
    text(outputOf(noEvidenceStep ?? ({} as ResultStep))?.no_evidence_reason) ??
    'No evidence remained after the recorded screening and extraction decisions.';

  if (status === 'no_evidence') {
    return (
      <section
        aria-labelledby="run-results-title"
        className="mt-6 rounded-xl border border-border bg-card p-5 shadow-xs"
      >
        <div className="flex items-start gap-3">
          <FileQuestion
            aria-hidden="true"
            className="mt-0.5 h-5 w-5 text-muted-foreground"
          />
          <div>
            <h2 id="run-results-title" className="text-lg font-semibold">
              No evidence brief
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {noEvidenceReason}
            </p>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap gap-3" aria-label="Audit downloads">
          <DownloadLink
            runId={run.id}
            format="json"
            label="Download audit JSON"
          />
          <DownloadLink
            runId={run.id}
            format="csv"
            label="Download extraction CSV"
          />
        </div>
      </section>
    );
  }

  const unverified = status === 'unverified';
  return (
    <section
      aria-labelledby="run-results-title"
      className="mt-6 rounded-xl border border-border bg-card p-5 shadow-xs"
    >
      <div className="flex items-start gap-3">
        {unverified ? (
          <AlertTriangle
            aria-hidden="true"
            className="mt-0.5 h-5 w-5 text-(--nous-helios)"
          />
        ) : (
          <CheckCircle2
            aria-hidden="true"
            className="mt-0.5 h-5 w-5 text-(--nous-terra)"
          />
        )}
        <div>
          <h2 id="run-results-title" className="text-lg font-semibold">
            {unverified ? 'Unverified draft' : 'Verified brief'}
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {unverified
              ? 'This draft remains unverified in every download.'
              : 'Verification passed for this persisted brief.'}
          </p>
        </div>
      </div>

      {unverified && (
        <div
          role="alert"
          className="mt-4 rounded-lg border border-(--nous-helios)/30 bg-(--nous-helios)/10 p-3 text-sm text-foreground"
        >
          Verification did not pass. Treat the conclusions as a draft and
          inspect the recorded checks before use.
        </div>
      )}

      {metrics.length > 0 && (
        <ul
          aria-label="Run evidence summary"
          className="mt-4 grid gap-2 text-sm text-foreground sm:grid-cols-2 lg:grid-cols-3"
        >
          {metrics.map((metric) => (
            <li
              key={metric}
              className="rounded-lg border border-border bg-muted/30 px-3 py-2"
            >
              {metric}
            </li>
          ))}
        </ul>
      )}

      {limitations.length > 0 && (
        <div className="mt-4">
          <h3 className="text-sm font-medium text-foreground">
            Unresolved limitations
          </h3>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
            {limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-4 flex flex-wrap gap-3" aria-label="Brief downloads">
        <DownloadLink
          runId={run.id}
          format="markdown"
          label={
            unverified ? 'Download unverified Markdown' : 'Download Markdown'
          }
        />
        <DownloadLink
          runId={run.id}
          format="json"
          label={unverified ? 'Download unverified JSON' : 'Download JSON'}
        />
        <DownloadLink
          runId={run.id}
          format="csv"
          label={unverified ? 'Download unverified CSV' : 'Download CSV'}
        />
      </div>
    </section>
  );
}
