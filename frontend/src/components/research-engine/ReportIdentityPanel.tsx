'use client';

import { useState, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  linkStudy,
  listReportHistory,
  listReports,
  mergeReports,
  type IdentityEvent,
  type ResearchReport,
} from '@/services/researchEngineService';

interface ReportIdentityPanelProps {
  projectId: string;
  readOnly?: boolean;
}

type LinkDecision = 'confirmed' | 'disputed';

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null;

/** `evidence.conflicts` is server JSON; show only well-formed kind/value pairs. */
function conflictsOf(evidence: Record<string, unknown>): string[] {
  const conflicts = evidence.conflicts;
  if (!Array.isArray(conflicts)) return [];
  return conflicts.flatMap((item: unknown) =>
    isRecord(item) &&
    typeof item.kind === 'string' &&
    typeof item.value === 'string'
      ? [`${item.kind} ${item.value}`]
      : []
  );
}

function mentionsReport(event: IdentityEvent, reportId: string): boolean {
  return Object.values(event.payload).some(
    (value) =>
      value === reportId || (Array.isArray(value) && value.includes(reportId))
  );
}

export function ReportIdentityPanel({
  projectId,
  readOnly = false,
}: ReportIdentityPanelProps): ReactElement {
  const queryClient = useQueryClient();
  const queryKey = ['research-reports', projectId] as const;
  const [rationale, setRationale] = useState<Record<string, string>>({});
  const [mergeTarget, setMergeTarget] = useState<Record<string, string>>({});

  const reports = useQuery({
    queryKey,
    queryFn: () => listReports(projectId),
  });
  const history = useQuery({
    queryKey: [...queryKey, 'history'],
    queryFn: () => listReportHistory(projectId),
  });
  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: [...queryKey] });
  };

  const link = useMutation({
    mutationFn: ({
      report,
      status,
    }: {
      report: ResearchReport;
      status: LinkDecision;
    }) =>
      linkStudy(projectId, report.id, {
        study_id: report.study_id ?? null,
        status,
        rationale: (rationale[report.id] ?? '').trim(),
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: refresh,
  });
  const merge = useMutation({
    mutationFn: (report: ResearchReport) =>
      mergeReports(projectId, {
        surviving_report_id: mergeTarget[report.id] ?? '',
        merged_report_ids: [report.id],
        rationale: (rationale[report.id] ?? '').trim(),
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: refresh,
  });

  const rows = reports.data ?? [];
  const live = rows.filter((report) => !report.merged_into_report_id);
  const error = link.error ?? merge.error ?? reports.error ?? history.error;
  // A new attempt clears the previous one's error so it can't outlive a success.
  const clearErrors = (): void => {
    link.reset();
    merge.reset();
  };
  const busy = link.isPending || merge.isPending;

  return (
    <section className="rounded-xl border border-border bg-card p-5">
      <h2 className="font-medium text-foreground">Reports and studies</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Reports are matched by identifiers only. Reviewers propose study links;
        adjudicators confirm, dispute, or merge.
      </p>
      {reports.isLoading ? (
        <p role="status" className="mt-4 text-sm text-muted-foreground">
          Loading reports…
        </p>
      ) : rows.length === 0 ? (
        <p className="mt-4 text-sm text-muted-foreground">
          No reports yet. They appear after a search step completes.
        </p>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-muted-foreground">
              <tr>
                <th className="py-2 pr-3 font-medium">Report</th>
                <th className="py-2 pr-3 font-medium">Identifiers</th>
                <th className="py-2 pr-3 font-medium">Sources</th>
                <th className="py-2 pr-3 font-medium">Study</th>
                {!readOnly && <th className="py-2 font-medium">Decision</th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-border align-top">
              {rows.map((report) => {
                const merged = Boolean(report.merged_into_report_id);
                const reason = (rationale[report.id] ?? '').trim();
                return (
                  <tr key={report.id}>
                    <td className="py-2 pr-3 text-foreground">
                      <div>{report.title_snapshot}</div>
                      <details className="mt-1 text-xs text-muted-foreground">
                        <summary className="cursor-pointer">Evidence</summary>
                        <ul className="mt-1 space-y-1">
                          {report.observations.map((observation) => (
                            <li key={observation.source_id}>
                              <span>
                                {observation.match_method} match — source{' '}
                                {observation.source_id}
                              </span>
                              {conflictsOf(observation.evidence).map(
                                (conflict) => (
                                  <div
                                    key={conflict}
                                    className="text-destructive"
                                  >
                                    Conflict: {conflict}
                                  </div>
                                )
                              )}
                            </li>
                          ))}
                          {(history.data ?? [])
                            .filter((event) => mentionsReport(event, report.id))
                            .map((event) => (
                              <li key={event.seq}>
                                #{event.seq} {event.event_type} by{' '}
                                {event.actor_role}
                                {event.reason ? ` — ${event.reason}` : ''}
                              </li>
                            ))}
                        </ul>
                      </details>
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">
                      {Object.entries(report.identifiers).map(
                        ([kind, values]) => (
                          <div
                            key={kind}
                          >{`${kind}: ${values.join(', ')}`}</div>
                        )
                      )}
                    </td>
                    <td className="py-2 pr-3 text-muted-foreground">
                      {report.observations.length}
                    </td>
                    <td className="py-2 pr-3">
                      <span className="rounded bg-muted px-2 py-1 text-xs text-muted-foreground">
                        {merged
                          ? 'merged'
                          : (report.study_link_status ?? 'unlinked')}
                      </span>
                    </td>
                    {!readOnly && (
                      <td className="py-2">
                        {!merged && (
                          <div className="flex min-w-64 flex-col gap-2">
                            <input
                              aria-label={`Rationale for ${report.title_snapshot}`}
                              value={rationale[report.id] ?? ''}
                              onChange={(event) =>
                                setRationale((current) => ({
                                  ...current,
                                  [report.id]: event.target.value,
                                }))
                              }
                              placeholder="Rationale"
                              className="rounded-md border border-border bg-background px-2 py-1 text-sm"
                            />
                            <div className="flex gap-2">
                              {/* Dispute needs an existing study; the API rejects it otherwise. */}
                              {(report.study_id
                                ? (['confirmed', 'disputed'] as const)
                                : (['confirmed'] as const)
                              ).map((status: LinkDecision) => (
                                <button
                                  key={status}
                                  type="button"
                                  aria-label={`${status === 'confirmed' ? 'Confirm' : 'Dispute'} ${report.title_snapshot}`}
                                  disabled={!reason || busy}
                                  onClick={() => {
                                    clearErrors();
                                    link.mutate({ report, status });
                                  }}
                                  className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
                                >
                                  {status === 'confirmed'
                                    ? 'Confirm'
                                    : 'Dispute'}
                                </button>
                              ))}
                            </div>
                            <div className="flex gap-2">
                              <select
                                aria-label={`Merge ${report.title_snapshot} into`}
                                value={mergeTarget[report.id] ?? ''}
                                onChange={(event) =>
                                  setMergeTarget((current) => ({
                                    ...current,
                                    [report.id]: event.target.value,
                                  }))
                                }
                                className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-xs"
                              >
                                <option value="">Merge into…</option>
                                {live
                                  .filter((other) => other.id !== report.id)
                                  .map((other) => (
                                    <option key={other.id} value={other.id}>
                                      {`${other.title_snapshot} · ${other.id.slice(0, 8)}`}
                                    </option>
                                  ))}
                              </select>
                              <button
                                type="button"
                                disabled={
                                  !reason || !mergeTarget[report.id] || busy
                                }
                                aria-label={`Merge ${report.title_snapshot}`}
                                onClick={() => {
                                  clearErrors();
                                  merge.mutate(report);
                                }}
                                className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
                              >
                                Merge
                              </button>
                            </div>
                          </div>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          {error instanceof Error ? error.message : 'Failed to update reports.'}
        </p>
      )}
    </section>
  );
}
