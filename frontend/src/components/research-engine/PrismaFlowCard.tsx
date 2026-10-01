'use client';

import { type ReactElement } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import {
  downloadPrismaFlow,
  getPrismaFlow,
} from '@/services/researchEngineService';

/** `{a: 1, b: 2}` -> "a 1 · b 2", or "none". */
const breakdown = (counts: Record<string, number>): string =>
  Object.entries(counts)
    .map(([key, n]) => `${key} ${n}`)
    .join(' · ') || 'none';

/**
 * GOO-303 PRISMA 2020 flow. Every number is recomputed by the server from
 * persisted rows on each read; nothing here is stored or editable.
 */
export function PrismaFlowCard({
  projectId,
}: {
  projectId: string;
}): ReactElement {
  const flow = useQuery({
    queryKey: ['prisma', projectId],
    queryFn: () => getPrismaFlow(projectId),
  });
  const download = useMutation({
    mutationFn: (format: 'json' | 'md') =>
      downloadPrismaFlow(projectId, format),
  });
  const body = flow.data?.body;
  const c = body?.counts;
  const rows: [string, string | number][] = c
    ? [
        ['Records identified', c.records_identified],
        ['By source', breakdown(c.records_by_source)],
        ['By import', breakdown(c.records_by_import)],
        ['Duplicates removed', c.duplicates_removed],
        ['Import records rejected', c.import_rejected],
        ['Records screened', c.records_screened],
        ['Records excluded', c.records_excluded],
        ['Awaiting screening', c.records_awaiting_screening],
        ['Reports sought', c.reports_sought],
        ['Reports not retrieved', c.reports_not_retrieved],
        ['Awaiting retrieval', c.reports_awaiting_retrieval],
        ['Reports assessed', c.reports_assessed],
        ['Excluded by reason', breakdown(c.reports_excluded_by_reason)],
        ['Included reports', c.included_reports],
        ['Included studies', c.included_studies],
        ['Amendments', body.amendments.length],
      ]
    : [];
  const error = flow.error ?? download.error;

  return (
    <section
      aria-labelledby={`prisma-${projectId}`}
      className="rounded-xl border border-border bg-card p-5"
    >
      <h2 id={`prisma-${projectId}`} className="font-medium text-foreground">
        PRISMA flow
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Derived from the recorded searches, reviews and full-text attempts.
        Missing full text is never counted as an exclusion.
      </p>
      {flow.isLoading && (
        <p role="status" className="mt-4 text-sm text-muted-foreground">
          Loading PRISMA flow…
        </p>
      )}
      {c && flow.data && (
        <>
          <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            {rows.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="text-foreground">{value}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-3 text-xs text-muted-foreground">
            {`Export hash ${flow.data.body_sha256.slice(0, 12)}`}
          </p>
          <div className="mt-3 flex gap-2">
            {(['json', 'md'] as const).map((format) => (
              <button
                key={format}
                type="button"
                disabled={download.isPending}
                onClick={() => download.mutate(format)}
                className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
              >
                {format === 'json' ? 'Download JSON' : 'Download Markdown'}
              </button>
            ))}
          </div>
        </>
      )}
      {error && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          {error instanceof Error ? error.message : 'PRISMA flow unavailable.'}
        </p>
      )}
    </section>
  );
}
