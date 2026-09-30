'use client';

import { useState, type FormEvent, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  chaseCitations,
  downloadCorpus,
  getCorpusCoverage,
  getImport,
  importSearchResults,
  listImports,
  listReports,
  type ImportDeclaration,
  type ImportFormat,
  type ImportReceipt,
} from '@/services/researchEngineService';

interface CorpusPanelProps {
  projectId: string;
  readOnly?: boolean;
}

type Direction = 'backward' | 'forward';

/** The server's per-chase cap (step_executor.MAX_CONNECTOR_RESULTS). */
const CHASE_LIMIT = 50;

const FORMATS: { value: ImportFormat; label: string }[] = [
  { value: 'ris', label: 'RIS' },
  { value: 'csv', label: 'CSV' },
  { value: 'nbib', label: 'NBIB (MEDLINE)' },
];

const inputClass =
  'w-full rounded-md border border-border bg-background px-2 py-1 text-sm';

/** A human label for one receipt row (used to give controls row context). */
function receiptLabel(receipt: ImportReceipt): string {
  const declared = receipt.declared;
  return 'database' in declared
    ? `${declared.database} v${receipt.version}`
    : `${declared.direction} citation chase v${receipt.version}`;
}

function declaredDate(receipt: ImportReceipt): string {
  const declared = receipt.declared;
  if (!('database' in declared)) return '—';
  return declared.search_date ?? 'not declared';
}

function observedDate(receipt: ImportReceipt): string {
  const value =
    receipt.observed.imported_at ?? receipt.observed.started_at ?? null;
  return typeof value === 'string' ? value.slice(0, 10) : '—';
}

function RejectedRecords({
  projectId,
  receipt,
}: {
  projectId: string;
  receipt: ImportReceipt;
}): ReactElement {
  const [open, setOpen] = useState(false);
  const detail = useQuery({
    queryKey: ['research-imports', projectId, receipt.id],
    queryFn: () => getImport(projectId, receipt.id),
    enabled: open,
  });
  const rejected = (detail.data?.records ?? []).filter(
    (record) => record.status === 'rejected'
  );
  return (
    <details
      className="text-xs text-muted-foreground"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer">
        {`Rejected records for ${receiptLabel(receipt)} (${receipt.rejected_count})`}
      </summary>
      {detail.isLoading ? (
        <p role="status" className="mt-1">
          Loading records…
        </p>
      ) : detail.error ? (
        <p role="alert" className="mt-1 text-destructive">
          Could not load the records for {receiptLabel(receipt)}.
        </p>
      ) : rejected.length === 0 ? (
        <p className="mt-1">No rejected records.</p>
      ) : (
        <ul className="mt-1 space-y-1">
          {rejected.map((record) => (
            <li key={record.id}>
              <span className="text-foreground">
                Record {record.record_index + 1}: {record.rejection_reason}
              </span>
              {record.raw != null && (
                <pre className="mt-1 whitespace-pre-wrap rounded bg-muted p-2">
                  {record.raw}
                </pre>
              )}
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}

export function CorpusPanel({
  projectId,
  readOnly = false,
}: CorpusPanelProps): ReactElement {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [format, setFormat] = useState<ImportFormat>('ris');
  const [database, setDatabase] = useState('');
  const [queryText, setQueryText] = useState('');
  const [searchDate, setSearchDate] = useState('');
  const [redistribution, setRedistribution] =
    useState<ImportDeclaration['redistribution']>('restricted');
  const [seedReportId, setSeedReportId] = useState('');
  const [direction, setDirection] = useState<Direction>('backward');
  const [notice, setNotice] = useState<string | null>(null);

  const receipts = useQuery({
    queryKey: ['research-imports', projectId],
    queryFn: () => listImports(projectId),
  });
  const coverage = useQuery({
    queryKey: ['research-coverage', projectId],
    queryFn: () => getCorpusCoverage(projectId),
  });
  const reports = useQuery({
    queryKey: ['research-reports', projectId],
    queryFn: () => listReports(projectId),
    enabled: !readOnly,
  });
  const refresh = (): void => {
    for (const key of [
      'research-imports',
      'research-coverage',
      'research-reports',
    ]) {
      void queryClient.invalidateQueries({ queryKey: [key, projectId] });
    }
  };

  const importFile = useMutation({
    mutationFn: ({ upload }: { upload: File }) =>
      importSearchResults(projectId, upload, format, {
        database: database.trim(),
        query_text: queryText.trim() || null,
        search_date: searchDate || null,
        redistribution,
      }),
    onSuccess: (receipt) => {
      setNotice(
        receipt.replayed
          ? `This file was already imported; showing the existing receipt (${receiptLabel(receipt)}).`
          : `Imported ${receiptLabel(receipt)}: ${receipt.accepted_count} accepted, ${receipt.rejected_count} rejected.`
      );
      refresh();
    },
  });
  const chase = useMutation({
    mutationFn: () =>
      chaseCitations(projectId, {
        seed_report_id: seedReportId,
        direction,
        max_results: CHASE_LIMIT,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: (receipt) => {
      setNotice(
        `Citation chase recorded (${String(receipt.observed.completion ?? 'unknown')}): ${receipt.accepted_count} records.`
      );
      refresh();
    },
  });
  const download = useMutation({
    mutationFn: (kind: 'json' | 'zip') => downloadCorpus(projectId, kind),
  });

  const busy = importFile.isPending || chase.isPending;
  const canImport = Boolean(file) && database.trim().length > 0 && !busy;
  const error =
    importFile.error ??
    chase.error ??
    download.error ??
    receipts.error ??
    coverage.error;
  const resetErrors = (): void => {
    importFile.reset();
    chase.reset();
    download.reset();
    setNotice(null);
  };
  const submitImport = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!file || !canImport) return;
    resetErrors();
    importFile.mutate({ upload: file });
  };
  const liveReports = (reports.data ?? []).filter(
    (report) => !report.merged_into_report_id
  );
  // citation_chasing is a free-form server object; read only well-formed parts.
  const chasing = coverage.data?.citation_chasing;
  const chasingRequired = chasing?.required === true;
  const missingDirections = Array.isArray(chasing?.missing_directions)
    ? chasing.missing_directions.filter(
        (value: unknown): value is string => typeof value === 'string'
      )
    : [];

  return (
    <section
      aria-labelledby="corpus-panel-heading"
      className="rounded-xl border border-border bg-card p-5"
    >
      <h2 id="corpus-panel-heading" className="font-medium text-foreground">
        Search imports and corpus
      </h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Import results exported from databases without an API. Every record is
        kept, accepted or rejected, and each file becomes an immutable receipt.
      </p>

      {!readOnly && (
        <form
          onSubmit={submitImport}
          className="mt-4 grid gap-3 sm:grid-cols-2"
          aria-label="Import search results"
        >
          <label className="text-sm">
            <span className="block text-muted-foreground">Result file</span>
            <input
              type="file"
              accept=".ris,.csv,.nbib,.txt"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              className={inputClass}
            />
          </label>
          <label className="text-sm">
            <span className="block text-muted-foreground">File format</span>
            <select
              value={format}
              onChange={(event) =>
                setFormat(event.target.value as ImportFormat)
              }
              className={inputClass}
            >
              {FORMATS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="block text-muted-foreground">
              Database searched (declared by you, required)
            </span>
            <input
              required
              value={database}
              onChange={(event) => setDatabase(event.target.value)}
              placeholder="e.g. Embase (Ovid)"
              className={inputClass}
            />
          </label>
          <label className="text-sm">
            <span className="block text-muted-foreground">
              Search date (declared by you)
            </span>
            <input
              type="date"
              value={searchDate}
              onChange={(event) => setSearchDate(event.target.value)}
              className={inputClass}
            />
          </label>
          <label className="text-sm sm:col-span-2">
            <span className="block text-muted-foreground">
              Search query (declared by you)
            </span>
            <textarea
              value={queryText}
              onChange={(event) => setQueryText(event.target.value)}
              rows={2}
              className={inputClass}
            />
          </label>
          <label className="text-sm">
            <span className="block text-muted-foreground">
              Redistribution of original records
            </span>
            <select
              value={redistribution}
              onChange={(event) =>
                setRedistribution(
                  event.target.value as ImportDeclaration['redistribution']
                )
              }
              className={inputClass}
            >
              <option value="restricted">
                Restricted (keep originals private)
              </option>
              <option value="allowed">Allowed in exports</option>
            </select>
          </label>
          <div className="flex items-end">
            <button
              type="submit"
              disabled={!canImport}
              className="rounded-md border border-border px-3 py-1 text-sm disabled:opacity-50"
            >
              {importFile.isPending ? 'Importing…' : 'Import results'}
            </button>
          </div>
        </form>
      )}

      {notice && (
        <p role="status" className="mt-3 text-sm text-foreground">
          {notice}
        </p>
      )}

      {receipts.isLoading ? (
        <p role="status" className="mt-4 text-sm text-muted-foreground">
          Loading imports…
        </p>
      ) : (receipts.data ?? []).length === 0 ? (
        <p className="mt-4 text-sm text-muted-foreground">No imports yet.</p>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">
              Import and citation-chase receipts
            </caption>
            <thead className="text-xs text-muted-foreground">
              <tr>
                <th className="py-2 pr-3 font-medium">Receipt</th>
                <th className="py-2 pr-3 font-medium">Declared search date</th>
                <th className="py-2 pr-3 font-medium">Recorded on</th>
                <th className="py-2 pr-3 font-medium">Accepted</th>
                <th className="py-2 pr-3 font-medium">Rejected</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border align-top">
              {(receipts.data ?? []).map((receipt) => (
                <tr key={receipt.id}>
                  <td className="py-2 pr-3 text-foreground">
                    <div>{receiptLabel(receipt)}</div>
                    {receipt.rejected_count > 0 && (
                      <RejectedRecords
                        projectId={projectId}
                        receipt={receipt}
                      />
                    )}
                  </td>
                  <td className="py-2 pr-3 text-muted-foreground">
                    {declaredDate(receipt)}
                  </td>
                  <td className="py-2 pr-3 text-muted-foreground">
                    {observedDate(receipt)}
                  </td>
                  <td className="py-2 pr-3">{receipt.accepted_count}</td>
                  <td className="py-2 pr-3">{receipt.rejected_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!readOnly && (
        <div
          role="group"
          aria-label="Citation chase"
          className="mt-4 flex flex-wrap items-end gap-2"
        >
          <label className="text-sm">
            <span className="block text-muted-foreground">
              Seed report for citation chase
            </span>
            <select
              value={seedReportId}
              onChange={(event) => setSeedReportId(event.target.value)}
              className={inputClass}
            >
              <option value="">Choose a report…</option>
              {liveReports.map((report) => (
                <option key={report.id} value={report.id}>
                  {report.title_snapshot}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="block text-muted-foreground">Direction</span>
            <select
              value={direction}
              onChange={(event) =>
                setDirection(event.target.value as Direction)
              }
              className={inputClass}
            >
              <option value="backward">Backward (references)</option>
              <option value="forward">Forward (citing works)</option>
            </select>
          </label>
          <button
            type="button"
            disabled={!seedReportId || busy}
            onClick={() => {
              resetErrors();
              chase.mutate();
            }}
            className="rounded-md border border-border px-3 py-1 text-sm disabled:opacity-50"
          >
            {chase.isPending ? 'Chasing…' : 'Chase citations (OpenAlex)'}
          </button>
        </div>
      )}

      {coverage.data && (
        <div className="mt-4 text-sm text-muted-foreground">
          <p>
            Searched: {coverage.data.searched.length} provider searches, imports
            and chases.
            {coverage.data.not_searched.length > 0 &&
              ` Not searched: ${coverage.data.not_searched.join(', ')}.`}
          </p>
          {chasingRequired && (
            <p>
              Protocol requires citation chasing
              {missingDirections.length > 0
                ? `; missing: ${missingDirections.join(', ')}.`
                : '; all required directions recorded.'}
            </p>
          )}
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {(['json', 'zip'] as const).map((kind) => (
          <button
            key={kind}
            type="button"
            disabled={download.isPending}
            onClick={() => {
              resetErrors();
              download.mutate(kind);
            }}
            className="rounded-md border border-border px-3 py-1 text-sm disabled:opacity-50"
          >
            {`Download corpus (${kind.toUpperCase()})`}
          </button>
        ))}
        <span className="text-xs text-muted-foreground">
          Coverage is not exhaustive.
        </span>
      </div>

      {error && (
        <p role="alert" className="mt-3 text-sm text-destructive">
          {error instanceof Error ? error.message : 'Corpus request failed.'}
        </p>
      )}
    </section>
  );
}
