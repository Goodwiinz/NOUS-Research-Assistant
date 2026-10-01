'use client';

import { useState, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@/hooks/useAuth';
import {
  executeSynthesis,
  exportSynthesis,
  listEvidence,
  listSynthesis,
  previewSynthesis,
  type EvidenceTable,
  type ProjectRoleAssignment,
  type SynthesisExclusion,
  type SynthesisPreview,
  type SynthesisResult,
  type SynthesisRoles,
} from '@/services/researchEngineService';
import { listFormVersions } from '@/services/scispaceService';

interface SynthesisPanelProps {
  projectId: string;
  roles: ProjectRoleAssignment[];
  readOnly?: boolean;
}

type Role = keyof SynthesisRoles;

const ROLES: { id: Role; label: string }[] = [
  { id: 'mean_i', label: 'Intervention mean' },
  { id: 'sd_i', label: 'Intervention SD' },
  { id: 'n_i', label: 'Intervention n' },
  { id: 'mean_c', label: 'Control mean' },
  { id: 'sd_c', label: 'Control SD' },
  { id: 'n_c', label: 'Control n' },
];
const ROLE_LABEL = Object.fromEntries(ROLES.map((r) => [r.id, r.label]));
const ARM: Record<string, string> = { i: 'intervention', c: 'control' };
const RUN_REASONS: Record<string, string> = {
  unit_mismatch: 'Means and SDs do not share one unit',
  timepoint_mismatch: 'A field is not at the selected timepoint',
  role_not_in_table: 'A field is not in this evidence table',
  wrong_field_type: 'A field is not numeric',
  insufficient_studies: 'Fewer than two usable studies',
};
const Z = 1.959963984540054;
const BADGE = 'rounded-full px-2 py-0.5 text-xs font-medium';
const BUTTON =
  'rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-muted disabled:opacity-50';
const INPUT = 'rounded-md border border-border bg-background px-2 py-1';

const synthesisKey = (projectId: string): readonly string[] =>
  ['project', projectId, 'research-engine', 'synthesis'] as const;
const evidenceKey = (projectId: string): readonly string[] =>
  ['project', projectId, 'research-engine', 'evidence'] as const;

/** A structured reason code as a human label. */
export function reasonLabel(reason: string): string {
  const [code, arg = ''] = reason.split(':');
  switch (code) {
    case 'missing_input':
      return `Missing ${ROLE_LABEL[arg] ?? arg}`;
    case 'conflicting_reports':
      return `Reports disagree on ${ROLE_LABEL[arg] ?? arg}`;
    case 'invalid_sample_size':
      return `Invalid sample size (${ARM[arg] ?? arg})`;
    case 'invalid_variance':
      return `Invalid SD (${ARM[arg] ?? arg})`;
    case 'non_numeric':
      return `Not a number: ${ROLE_LABEL[arg] ?? arg}`;
    case 'study_link_unresolved':
      return 'Study link unresolved';
    case 'no_report_identity':
      return 'No report identity';
    default:
      return RUN_REASONS[code] ?? reason;
  }
}

function unitLabel(unit: string | null | undefined): string {
  if (!unit) return 'Unlinked document';
  const [kind, id = ''] = unit.split(':');
  return `${kind === 'study' ? 'Study' : 'Report'} ${id.slice(0, 8)}`;
}

function num(value: number | null | undefined, digits = 3): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits);
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : 'Request failed.';
}

function newKey(): string {
  return crypto.randomUUID();
}

function Exclusions({
  items,
}: {
  items: SynthesisExclusion[];
}): ReactElement | null {
  if (items.length === 0) return null;
  return (
    <div>
      <h4 className="text-sm font-medium text-foreground">Excluded</h4>
      <ul className="mt-1 space-y-0.5 text-sm text-muted-foreground">
        {items.map((e, index) => (
          <li key={`${e.unit ?? 'run'}-${e.reason}-${index}`}>
            {unitLabel(e.unit)}: {reasonLabel(e.reason)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ResultView({ result }: { result: SynthesisResult }): ReactElement {
  const total = result.included.reduce((s, u) => s + (u.w_random ?? 0), 0);
  const runFailures = result.excluded.filter((e) => e.reason in RUN_REASONS);
  return (
    <div className="space-y-3 rounded-md border border-border p-3">
      {result.stale && (
        <p role="alert" className="text-sm text-destructive">
          Stale: its evidence table or protocol changed since this ran. Execute
          again to record a successor.
        </p>
      )}
      {result.status === 'validation_failed' ? (
        <div role="status" className="text-sm text-destructive">
          Validation failed:{' '}
          {runFailures.map((e) => reasonLabel(e.reason)).join('; ')}
        </div>
      ) : (
        <>
          <table className="w-full text-left text-sm">
            <thead className="text-muted-foreground">
              <tr>
                <th className="py-1 font-normal">Unit</th>
                <th className="py-1 font-normal">g</th>
                <th className="py-1 font-normal">95% CI</th>
                <th className="py-1 font-normal">Weight</th>
              </tr>
            </thead>
            <tbody>
              {result.included.map((u) => {
                const half = u.v == null ? null : Z * Math.sqrt(u.v);
                return (
                  <tr key={u.unit} className="border-t border-border">
                    <td className="py-1">{unitLabel(u.unit)}</td>
                    <td className="py-1">{num(u.g)}</td>
                    <td className="py-1">
                      {u.g == null || half === null
                        ? '—'
                        : `${num(u.g - half)} to ${num(u.g + half)}`}
                    </td>
                    <td className="py-1">
                      {total > 0 && u.w_random != null
                        ? `${((100 * u.w_random) / total).toFixed(1)}%`
                        : '—'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
            <dt className="text-muted-foreground">Pooled SMD (g)</dt>
            <dd>
              {num(result.estimate)} ({num(result.ci_low)} to{' '}
              {num(result.ci_high)})
            </dd>
            <dt className="text-muted-foreground">Q (df)</dt>
            <dd>
              {num(result.q)} ({result.df ?? '—'})
            </dd>
            <dt className="text-muted-foreground">I²</dt>
            <dd>
              {result.i2 == null ? '—' : `${(result.i2 * 100).toFixed(1)}%`}
            </dd>
            <dt className="text-muted-foreground">τ²</dt>
            <dd>{num(result.tau2, 4)}</dd>
          </dl>
        </>
      )}
      <Exclusions
        items={result.excluded.filter((e) => !(e.reason in RUN_REASONS))}
      />
      <details className="text-xs text-muted-foreground">
        <summary>Provenance</summary>
        <p>Estimator {result.estimator_version}</p>
        <p>Config {result.config_hash}</p>
        <p>Inputs {result.input_hash}</p>
        <p>Result {result.result_hash}</p>
        <p>Table version {result.table_version_id}</p>
      </details>
    </div>
  );
}

/**
 * GOO-311: the protocol-selected SMD (Hedges' g) with DerSimonian-Laird
 * random effects over one frozen evidence table. Preview shows exactly which
 * units enter and why the others do not; Execute (reviewers) records the
 * result. Intervention minus control; the server derives staleness.
 * ``# ponytail: no forest plot; add one when a reviewer asks for it.``
 */
export function SynthesisPanel({
  projectId,
  roles,
  readOnly = false,
}: SynthesisPanelProps): ReactElement {
  const userId = useAuth().user?.id;
  const canReview =
    !readOnly &&
    roles.some((r) => r.user_id === userId && r.role === 'reviewer');
  const queryClient = useQueryClient();
  const listing = useQuery({
    queryKey: synthesisKey(projectId),
    queryFn: () => listSynthesis(projectId),
  });
  const evidence = useQuery({
    queryKey: evidenceKey(projectId),
    queryFn: () => listEvidence(projectId),
  });
  const selection = listing.data?.selection ?? null;
  const tables: EvidenceTable[] =
    evidence.data?.outcomes
      .find(
        (o) =>
          o.outcome_key === selection?.outcome_key &&
          o.timepoint === selection?.timepoint
      )
      ?.tables.filter((t) => !t.superseded) ?? [];
  const [tablePicked, setTablePicked] = useState<string | null>(null);
  const table = tables.find((t) => t.id === tablePicked) ?? tables[0];
  const versions = useQuery({
    queryKey: ['matrix', table?.matrix_id, 'form-versions'],
    queryFn: () => listFormVersions(table?.matrix_id as string),
    enabled: table !== undefined,
  });
  const form = versions.data?.find((v) => v.id === table?.form_version_id);
  const fields = (form?.fields ?? []).flatMap((f) =>
    typeof f.field_id === 'string' &&
    typeof f.name === 'string' &&
    (table?.field_ids ?? []).includes(f.field_id)
      ? [{ id: f.field_id, name: f.name }]
      : []
  );
  const [mapping, setMapping] = useState<Partial<SynthesisRoles>>({});
  const complete = ROLES.every((r) => mapping[r.id]);
  const [preview, setPreview] = useState<SynthesisPreview | null>(null);
  const [key, setKey] = useState(newKey);
  const previewing = useMutation({
    mutationFn: () =>
      previewSynthesis(
        projectId,
        (table as EvidenceTable).id,
        mapping as SynthesisRoles
      ),
    onSuccess: setPreview,
  });
  const executing = useMutation({
    mutationFn: (p: SynthesisPreview) =>
      executeSynthesis(projectId, {
        table_version_id: p.table_version_id,
        roles: mapping as SynthesisRoles,
        expected_input_hash: p.input_hash,
        supersedes_result_id: p.tip_id ?? null,
        idempotency_key: key,
      }),
    onSuccess: () => {
      setKey(newKey());
      setPreview(null);
      void queryClient.invalidateQueries({ queryKey: synthesisKey(projectId) });
    },
  });
  const results = listing.data?.results ?? [];
  const latest = [...results].reverse().find((r) => !r.superseded);
  const blocked = (preview?.run_failures.length ?? 0) > 0;
  return (
    <section className="rounded-xl border border-border bg-card p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="font-medium text-foreground">
            Quantitative synthesis
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {selection
              ? `Standardized mean difference (Hedges' g), DerSimonian-Laird random effects, for ${selection.outcome_key} at ${selection.timepoint}. Intervention minus control.`
              : (listing.data?.selection_error ??
                'The protocol selects no quantitative synthesis.')}
          </p>
        </div>
        {results.length > 0 && (
          <button
            type="button"
            className={BUTTON}
            onClick={() => void exportSynthesis(projectId)}
          >
            Export
          </button>
        )}
      </div>
      {listing.isLoading && (
        <p role="status" className="mt-4 text-sm text-muted-foreground">
          Loading synthesis…
        </p>
      )}
      {listing.error && (
        <p role="alert" className="mt-4 text-sm text-destructive">
          {errorText(listing.error)}
        </p>
      )}
      {selection && tables.length === 0 && (
        <p className="mt-4 text-sm text-muted-foreground">
          Freeze an evidence table for this outcome first.
        </p>
      )}
      {selection && table && (
        <div className="mt-4 space-y-4">
          <div className="flex flex-wrap gap-4 text-sm">
            <label className="text-muted-foreground">
              Evidence table{' '}
              <select
                className={INPUT}
                value={table.id}
                onChange={(event) => {
                  setTablePicked(event.target.value);
                  setPreview(null);
                }}
              >
                {tables.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.id.slice(0, 8)}
                    {t.stale ? ' (stale)' : ''}
                  </option>
                ))}
              </select>
            </label>
            {ROLES.map((r) => (
              <label key={r.id} className="text-muted-foreground">
                {r.label}{' '}
                <select
                  className={INPUT}
                  value={mapping[r.id] ?? ''}
                  onChange={(event) => {
                    setMapping((m) => ({ ...m, [r.id]: event.target.value }));
                    setPreview(null);
                  }}
                >
                  <option value="">Choose a field…</option>
                  {fields.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.name}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              className={BUTTON}
              disabled={!complete || previewing.isPending}
              onClick={() => previewing.mutate()}
            >
              Preview inputs
            </button>
            {canReview && (
              <button
                type="button"
                className={BUTTON}
                disabled={!preview || blocked || executing.isPending}
                onClick={() => preview && executing.mutate(preview)}
              >
                Execute
              </button>
            )}
          </div>
          {(previewing.error || executing.error) && (
            <p role="alert" className="text-sm text-destructive">
              {errorText(previewing.error ?? executing.error)}
            </p>
          )}
          {preview && (
            <div className="space-y-2">
              {blocked && (
                <ul role="alert" className="text-sm text-destructive">
                  {preview.run_failures.map((f, index) => (
                    <li key={`${f.reason}-${index}`}>
                      {reasonLabel(f.reason)}
                      {f.detail ? ` (${f.detail})` : ''}
                    </li>
                  ))}
                </ul>
              )}
              <div>
                <h4 className="text-sm font-medium text-foreground">
                  Included ({preview.included.length})
                </h4>
                <ul className="mt-1 space-y-0.5 text-sm text-muted-foreground">
                  {preview.included.map((u) => (
                    <li key={u.unit}>
                      {unitLabel(u.unit)}{' '}
                      <span className={`${BADGE} bg-muted`}>
                        {u.report_ids.length}{' '}
                        {u.report_ids.length === 1 ? 'report' : 'reports'}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
              <Exclusions items={preview.excluded} />
            </div>
          )}
        </div>
      )}
      {latest && (
        <div className="mt-4">
          <ResultView result={latest} />
        </div>
      )}
    </section>
  );
}
