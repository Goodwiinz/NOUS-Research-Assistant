'use client';

import { useState, type FormEvent, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@/hooks/useAuth';
import {
  assessCertainty,
  createEvidenceTable,
  exportEvidence,
  listAppraisals,
  listEvidence,
  previewEvidenceTable,
  recordContradiction,
  type Certainty,
  type CertaintyLevel,
  type CertaintyRatings,
  type Contradiction,
  type ContradictionCreate,
  type EvidenceCell,
  type EvidenceOutcome,
  type EvidenceTable,
  type EvidenceTablePreview,
  type ProjectRoleAssignment,
  type StanceSuggestionGroup,
} from '@/services/researchEngineService';
import { listFormVersions, listMatrices } from '@/services/scispaceService';

interface EvidenceTablePanelProps {
  projectId: string;
  roles: ProjectRoleAssignment[];
  readOnly?: boolean;
}

type Domain = keyof CertaintyRatings;
type Rating = CertaintyRatings[Domain];
type Field = { id: string; name: string; timepoint: string | null };

const DOMAINS: { id: Domain; label: string }[] = [
  { id: 'risk_of_bias', label: 'Risk of bias' },
  { id: 'inconsistency', label: 'Inconsistency' },
  { id: 'indirectness', label: 'Indirectness' },
  { id: 'imprecision', label: 'Imprecision' },
  { id: 'publication_bias', label: 'Publication bias' },
];
const LEVELS: CertaintyLevel[] = ['very_low', 'low', 'moderate', 'high'];
const LEVEL_LABEL: Record<CertaintyLevel, string> = {
  very_low: 'Very low',
  low: 'Low',
  moderate: 'Moderate',
  high: 'High',
};
const CELL_TEXT: Record<EvidenceCell['state'], string> = {
  value: '',
  missingness: 'Declared missing',
  missing: 'Not reported / missing',
  conflict: 'Reports disagree',
};
const STATUS_LABEL: Record<Contradiction['status'], string> = {
  unresolved: 'Unresolved',
  resolved: 'Resolved',
  acknowledged: 'Acknowledged disagreement',
};
const BADGE = 'rounded-full px-2 py-0.5 text-xs font-medium';
const STALE = `${BADGE} bg-destructive/10 text-destructive`;
const BUTTON =
  'rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-muted disabled:opacity-50';
const INPUT = 'rounded-md border border-border bg-background px-2 py-1';

const evidenceKey = (projectId: string): readonly string[] =>
  ['project', projectId, 'research-engine', 'evidence'] as const;

/** GRADE's arithmetic; any unknown rating keeps the level unknown. */
export function deriveLevel(
  start: 'high' | 'low',
  ratings: CertaintyRatings
): CertaintyLevel | null {
  const values = DOMAINS.map((d) => ratings[d.id]);
  if (values.some((v) => v === null || v === undefined)) return null;
  const drop = values.reduce<number>((sum, v) => sum + Math.abs(v ?? 0), 0);
  return LEVELS[Math.max(LEVELS.indexOf(start) - drop, 0)];
}

function short(id: string): string {
  return id.slice(0, 8);
}

function unitLabel(unit: string): string {
  const [kind, id = ''] = unit.split(':');
  return `${kind === 'study' ? 'Study' : 'Report'} ${short(id)}`;
}

function show(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : 'Request failed.';
}

function fieldsOf(raw: Record<string, unknown>[] | undefined): Field[] {
  return (raw ?? []).flatMap((f) =>
    typeof f.field_id === 'string' && typeof f.name === 'string'
      ? [
          {
            id: f.field_id,
            name: f.name,
            timepoint: typeof f.timepoint === 'string' ? f.timepoint : null,
          },
        ]
      : []
  );
}

/**
 * GOO-310: freeze an evidence table per declared outcome and timepoint, record
 * contradictions and assess GRADE certainty. The preview shows one row per
 * analysis unit; a missing value is its own state, never a blank. Machine and
 * legacy cells and model stance rows are shown apart, labelled unreviewed, and
 * never enter a version. Staleness is derived by the server on every read.
 */
export function EvidenceTablePanel({
  projectId,
  roles,
  readOnly = false,
}: EvidenceTablePanelProps): ReactElement {
  const userId = useAuth().user?.id;
  const mine = new Set(
    roles.filter((r) => r.user_id === userId).map((r) => r.role)
  );
  const canReview = mine.has('reviewer') && !readOnly;
  const canAdjudicate = mine.has('adjudicator') && !readOnly;
  const listing = useQuery({
    queryKey: evidenceKey(projectId),
    queryFn: () => listEvidence(projectId),
  });
  const matrices = useQuery({
    queryKey: ['project', projectId, 'matrices'],
    queryFn: () => listMatrices(projectId),
  });
  const [picked, setPicked] = useState<string | null>(null);
  const [matrixPicked, setMatrixPicked] = useState<string | null>(null);
  const [fieldsPicked, setFieldsPicked] = useState<string[] | null>(null);
  const outcomes = listing.data?.outcomes ?? [];
  const outcome =
    outcomes.find((o) => `${o.outcome_key}|${o.timepoint}` === picked) ??
    outcomes[0];
  const matrixId = matrixPicked ?? matrices.data?.matrices[0]?.id ?? null;
  const versions = useQuery({
    queryKey: ['matrix', matrixId, 'form-versions'],
    queryFn: () => listFormVersions(matrixId as string),
    enabled: matrixId !== null,
  });
  const current = [...(versions.data ?? [])].sort(
    (a, b) => b.version_no - a.version_no
  )[0];
  const fields = fieldsOf(current?.fields).filter(
    (f) => f.timepoint === outcome?.timepoint
  );
  const fieldIds = fieldsPicked ?? fields.map((f) => f.id);
  const ready = Boolean(outcome && matrixId && fieldIds.length > 0);
  const preview = useQuery({
    queryKey: [
      ...evidenceKey(projectId),
      'preview',
      outcome?.outcome_key,
      outcome?.timepoint,
      matrixId,
      ...fieldIds,
    ],
    queryFn: () =>
      previewEvidenceTable(projectId, {
        outcome_key: (outcome as EvidenceOutcome).outcome_key,
        timepoint: (outcome as EvidenceOutcome).timepoint,
        matrix_id: matrixId as string,
        field_ids: fieldIds,
      }),
    enabled: ready,
  });
  const names = new Map(fields.map((f) => [f.id, f.name]));
  const tip = outcome?.tables.find((t) => !t.superseded);
  return (
    <section className="rounded-xl border border-border bg-card p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="font-medium text-foreground">
            Evidence tables and certainty
          </h2>
          {listing.data?.certainty_method && (
            <p className="mt-1 text-sm text-muted-foreground">
              {listing.data.certainty_method.method.toUpperCase()}{' '}
              {listing.data.certainty_method.version}; structure only
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => void exportEvidence(projectId)}
          className={BUTTON}
        >
          Export
        </button>
      </div>
      {listing.isLoading ? (
        <p role="status" className="mt-4 text-sm text-muted-foreground">
          Loading evidence…
        </p>
      ) : listing.error ? (
        <p role="alert" className="mt-4 text-sm text-destructive">
          {errorText(listing.error)}
        </p>
      ) : !outcome ? (
        <p className="mt-4 text-sm text-muted-foreground">
          The approved protocol declares no outcomes.
        </p>
      ) : (
        <div className="mt-4 space-y-5">
          <div className="flex flex-wrap gap-4 text-sm">
            <label className="text-muted-foreground">
              Outcome
              <select
                value={`${outcome.outcome_key}|${outcome.timepoint}`}
                onChange={(e) => {
                  setPicked(e.target.value);
                  setFieldsPicked(null);
                }}
                className={`ml-2 ${INPUT}`}
              >
                {outcomes.map((o) => (
                  <option
                    key={`${o.outcome_key}|${o.timepoint}`}
                    value={`${o.outcome_key}|${o.timepoint}`}
                  >
                    {o.outcome_key} · {o.timepoint}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-muted-foreground">
              Matrix
              <select
                value={matrixId ?? ''}
                onChange={(e) => {
                  setMatrixPicked(e.target.value);
                  setFieldsPicked(null);
                }}
                className={`ml-2 ${INPUT}`}
              >
                {(matrices.data?.matrices ?? []).map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <fieldset className="text-sm">
            <legend className="text-muted-foreground">
              Fields at {outcome.timepoint}
            </legend>
            {fields.length === 0 ? (
              <p className="text-muted-foreground">
                No field of this matrix is at {outcome.timepoint}.
              </p>
            ) : (
              fields.map((f) => (
                <label key={f.id} className="mr-4 inline-flex gap-1">
                  <input
                    type="checkbox"
                    checked={fieldIds.includes(f.id)}
                    onChange={(e) =>
                      setFieldsPicked(
                        e.target.checked
                          ? [...fieldIds, f.id]
                          : fieldIds.filter((id) => id !== f.id)
                      )
                    }
                  />
                  {f.name}
                </label>
              ))
            )}
          </fieldset>
          {preview.error && (
            <p role="alert" className="text-sm text-destructive">
              {errorText(preview.error)}
            </p>
          )}
          {preview.data && (
            <PreviewSection
              projectId={projectId}
              preview={preview.data}
              names={names}
              tip={tip}
              canFreeze={canReview || canAdjudicate}
            />
          )}
          <ContradictionSection
            projectId={projectId}
            outcome={outcome}
            tip={tip}
            names={names}
            suggestions={preview.data?.stance_suggestions ?? []}
            canRecord={canReview || canAdjudicate}
            canAdjudicate={canAdjudicate}
          />
          <CertaintySection
            projectId={projectId}
            outcome={outcome}
            tip={tip}
            canAssess={canReview}
          />
        </div>
      )}
    </section>
  );
}

function CellView({
  cell,
  label,
}: {
  cell: EvidenceCell;
  label: string;
}): ReactElement {
  const text =
    cell.state === 'value'
      ? show(cell.value)
      : cell.state === 'missingness'
        ? `${CELL_TEXT.missingness}: ${cell.missingness ?? ''}`
        : CELL_TEXT[cell.state];
  return (
    <td className="border-t border-border p-2 align-top">
      <span
        aria-label={`${label}: ${text}`}
        className={
          cell.state === 'conflict'
            ? 'text-destructive'
            : cell.state === 'value'
              ? 'text-foreground'
              : 'text-muted-foreground'
        }
      >
        {text}
      </span>
      {cell.tips.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs text-muted-foreground">
          {cell.tips.map((t) => (
            <li key={t.accepted_value_id}>
              {t.missingness ?? show(t.value)} · report {short(t.report_id)} ·
              document {short(t.document_id)} · revision{' '}
              {t.source_hash.slice(0, 12)}
            </li>
          ))}
        </ul>
      )}
    </td>
  );
}

function EvidenceGrid({
  rows,
  fieldIds,
  names,
}: {
  rows: EvidenceTablePreview['rows'];
  fieldIds: string[];
  names: Map<string, string>;
}): ReactElement {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-muted-foreground">
            <th className="p-2 font-medium">Unit</th>
            {fieldIds.map((id) => (
              <th key={id} className="p-2 font-medium">
                {names.get(id) ?? short(id)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.row_key}>
              <th className="border-t border-border p-2 align-top font-medium text-foreground">
                {unitLabel(row.unit)}
              </th>
              {fieldIds.map((id) => {
                const cell = row.cells[id];
                return cell ? (
                  <CellView
                    key={id}
                    cell={cell}
                    label={`${unitLabel(row.unit)} ${names.get(id) ?? id}`}
                  />
                ) : (
                  <td key={id} className="border-t border-border p-2" />
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PreviewSection({
  projectId,
  preview,
  names,
  tip,
  canFreeze,
}: {
  projectId: string;
  preview: EvidenceTablePreview;
  names: Map<string, string>;
  tip: EvidenceTable | undefined;
  canFreeze: boolean;
}): ReactElement {
  const queryClient = useQueryClient();
  const freeze = useMutation({
    mutationFn: () =>
      createEvidenceTable(projectId, {
        outcome_key: preview.outcome_key,
        timepoint: preview.timepoint,
        matrix_id: preview.matrix_id,
        field_ids: preview.field_ids,
        supersedes_table_id: preview.tip_id ?? null,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: evidenceKey(projectId) }),
  });
  const unchanged = tip !== undefined && !preview.differs_from_tip;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium text-foreground">Preview</h3>
        {preview.differs_from_tip && (
          <span className={`${BADGE} bg-muted text-muted-foreground`}>
            Differs from the frozen version
          </span>
        )}
        {canFreeze && (
          <button
            type="button"
            disabled={freeze.isPending || unchanged}
            onClick={() => freeze.mutate()}
            className={BUTTON}
          >
            {tip ? 'Rebuild table' : 'Freeze table version'}
          </button>
        )}
      </div>
      {freeze.error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(freeze.error)}
        </p>
      )}
      <EvidenceGrid
        rows={preview.rows}
        fieldIds={preview.field_ids}
        names={names}
      />
      {preview.excluded.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Excluded:{' '}
          {preview.excluded
            .map(
              (e) =>
                `document ${short(e.document_id)} (${
                  e.reason === 'study_link_unresolved'
                    ? 'study link unresolved'
                    : 'no report identity'
                })`
            )
            .join(', ')}
        </p>
      )}
      <details className="text-sm">
        <summary className="cursor-pointer text-muted-foreground">
          Unreviewed (machine/legacy) · {preview.unreviewed_cells.length}
        </summary>
        <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
          {preview.unreviewed_cells.map((c) => (
            <li key={`${c.document_id}|${c.field_id}`}>
              {c.column_name} · document {short(c.document_id)} ·{' '}
              {c.missingness ?? show(c.value)} · {c.source}, unreviewed
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}

function tipValues(tip: EvidenceTable | undefined): Map<string, string> {
  const values = new Map<string, string>();
  for (const row of tip?.rows ?? []) {
    for (const cell of Object.values(row.cells)) {
      for (const t of cell.tips) {
        values.set(
          t.accepted_value_id,
          `${t.missingness ?? show(t.value)} (${unitLabel(row.unit)}, report ${short(t.report_id)})`
        );
      }
    }
  }
  return values;
}

function ContradictionSection({
  projectId,
  outcome,
  tip,
  names,
  suggestions,
  canRecord,
  canAdjudicate,
}: {
  projectId: string;
  outcome: EvidenceOutcome;
  tip: EvidenceTable | undefined;
  names: Map<string, string>;
  suggestions: StanceSuggestionGroup[];
  canRecord: boolean;
  canAdjudicate: boolean;
}): ReactElement {
  const [opening, setOpening] = useState<string[] | null>(null);
  const values = tipValues(tip);
  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-foreground">Contradictions</h3>
      {suggestions.map((group) => (
        <div
          key={group.claim_hash}
          className="rounded-md border border-dashed border-border p-3 text-sm"
        >
          <p className="text-xs font-medium text-muted-foreground">
            Model suggestion — unreviewed
          </p>
          <p className="text-foreground">
            {group.claim_text ?? group.claim_hash}
          </p>
          <ul className="text-xs text-muted-foreground">
            {group.suggestions.map((s) => (
              <li key={s.id}>
                {s.stance} · document {short(s.source_id)} · {s.model_version}
              </li>
            ))}
          </ul>
          {canRecord && tip && (
            <button
              type="button"
              onClick={() => setOpening(group.suggestions.map((s) => s.id))}
              className={`mt-2 ${BUTTON}`}
            >
              Open contradiction
            </button>
          )}
        </div>
      ))}
      {canRecord && tip && opening === null && (
        <button type="button" onClick={() => setOpening([])} className={BUTTON}>
          Open contradiction
        </button>
      )}
      {opening !== null && tip && (
        <OpenForm
          projectId={projectId}
          tip={tip}
          names={names}
          suggestionIds={opening}
          onDone={() => setOpening(null)}
        />
      )}
      {outcome.contradictions.length === 0 ? (
        <p className="text-sm text-muted-foreground">No contradictions.</p>
      ) : (
        <ul className="space-y-3">
          {outcome.contradictions.map((group) => (
            <GroupCard
              key={group.contradiction_id}
              projectId={projectId}
              group={group}
              values={values}
              names={names}
              canRecord={canRecord}
              canAdjudicate={canAdjudicate}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function OpenForm({
  projectId,
  tip,
  names,
  suggestionIds,
  onDone,
}: {
  projectId: string;
  tip: EvidenceTable;
  names: Map<string, string>;
  suggestionIds: string[];
  onDone: () => void;
}): ReactElement {
  const queryClient = useQueryClient();
  const [fieldId, setFieldId] = useState(tip.field_ids[0]);
  const [members, setMembers] = useState<string[]>([]);
  const [explanation, setExplanation] = useState('');
  const tips = tip.rows.flatMap((row) =>
    (row.cells[fieldId]?.tips ?? []).map((t) => ({ row, t }))
  );
  const open = useMutation({
    mutationFn: () =>
      recordContradiction(projectId, {
        kind: 'opened',
        table_version_id: tip.id,
        field_id: fieldId,
        accepted_value_ids: members,
        stance_classification_ids: suggestionIds,
        explanation,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: evidenceKey(projectId) });
      onDone();
    },
  });
  const submit = (e: FormEvent): void => {
    e.preventDefault();
    open.mutate();
  };
  return (
    <form
      onSubmit={submit}
      className="space-y-2 rounded-md bg-muted/40 p-3 text-sm"
    >
      {suggestionIds.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Cites {suggestionIds.length} model suggestion(s), kept as an
          unreviewed snapshot.
        </p>
      )}
      <label className="block text-muted-foreground">
        Field
        <select
          value={fieldId}
          onChange={(e) => {
            setFieldId(e.target.value);
            setMembers([]);
          }}
          className={`ml-2 ${INPUT}`}
        >
          {tip.field_ids.map((id) => (
            <option key={id} value={id}>
              {names.get(id) ?? short(id)}
            </option>
          ))}
        </select>
      </label>
      <fieldset>
        <legend className="text-muted-foreground">Conflicting values</legend>
        {tips.map(({ row, t }) => (
          <label key={t.accepted_value_id} className="block">
            <input
              type="checkbox"
              checked={members.includes(t.accepted_value_id)}
              onChange={(e) =>
                setMembers(
                  e.target.checked
                    ? [...members, t.accepted_value_id]
                    : members.filter((m) => m !== t.accepted_value_id)
                )
              }
            />{' '}
            {t.missingness ?? show(t.value)} · {unitLabel(row.unit)} · report{' '}
            {short(t.report_id)}
          </label>
        ))}
      </fieldset>
      <textarea
        aria-label="Explanation"
        required
        maxLength={4000}
        value={explanation}
        onChange={(e) => setExplanation(e.target.value)}
        className={`block w-full ${INPUT}`}
      />
      {open.error && (
        <p role="alert" className="text-destructive">
          {errorText(open.error)}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={members.length < 2 || !explanation || open.isPending}
          className={BUTTON}
        >
          Record contradiction
        </button>
        <button type="button" onClick={onDone} className={BUTTON}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function GroupCard({
  projectId,
  group,
  values,
  names,
  canRecord,
  canAdjudicate,
}: {
  projectId: string;
  group: Contradiction;
  values: Map<string, string>;
  names: Map<string, string>;
  canRecord: boolean;
  canAdjudicate: boolean;
}): ReactElement {
  const queryClient = useQueryClient();
  const [explanation, setExplanation] = useState('');
  const record = useMutation({
    mutationFn: (kind: ContradictionCreate['kind']) =>
      recordContradiction(projectId, {
        kind,
        contradiction_id: group.contradiction_id,
        previous_id: group.rows[group.rows.length - 1]?.id,
        explanation,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () => {
      setExplanation('');
      void queryClient.invalidateQueries({ queryKey: evidenceKey(projectId) });
    },
  });
  const opened = group.rows[0];
  return (
    <li className="rounded-lg border border-border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground">
          {names.get(group.field_id) ?? short(group.field_id)}
        </span>
        <span
          className={`${BADGE} ${
            group.status === 'unresolved'
              ? 'bg-destructive/10 text-destructive'
              : 'bg-primary/10 text-primary'
          }`}
        >
          {STATUS_LABEL[group.status]}
        </span>
        {group.stale && <span className={STALE}>Stale</span>}
      </div>
      <ul className="mt-1 text-xs text-muted-foreground">
        {(opened?.accepted_value_ids ?? []).map((id) => (
          <li key={id}>{values.get(id) ?? `accepted value ${short(id)}`}</li>
        ))}
      </ul>
      <ol className="mt-2 space-y-1">
        {group.rows.map((row) => (
          <li key={row.id} className="text-foreground">
            <span className="font-medium">{row.kind}</span> ·{' '}
            {row.actor_name ?? short(row.actor_id)} ({row.actor_role}):{' '}
            {row.explanation}
            {row.suggestion && (
              <span className="ml-1 text-xs text-muted-foreground">
                (cites model suggestion — unreviewed)
              </span>
            )}
          </li>
        ))}
      </ol>
      {group.dissent.length > 0 && (
        <div className="mt-2 rounded-md bg-muted/40 p-2">
          <p className="text-xs font-medium text-muted-foreground">Dissent</p>
          <ul className="text-xs text-foreground">
            {group.dissent.map((d) => (
              <li key={d.id}>
                {d.superseded ? 'Superseded ' : ''}
                {d.kind} by {short(d.actor_id)}: {d.explanation}
              </li>
            ))}
          </ul>
        </div>
      )}
      {canRecord && (
        <div className="mt-2 space-y-2">
          <textarea
            aria-label="Contradiction note"
            maxLength={4000}
            value={explanation}
            onChange={(e) => setExplanation(e.target.value)}
            className={`block w-full ${INPUT}`}
          />
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={!explanation || record.isPending}
              onClick={() => record.mutate('dissent')}
              className={BUTTON}
            >
              Add dissent
            </button>
            {canAdjudicate && (
              <>
                <button
                  type="button"
                  disabled={!explanation || record.isPending}
                  onClick={() => record.mutate('resolved')}
                  className={BUTTON}
                >
                  Resolve
                </button>
                <button
                  type="button"
                  disabled={!explanation || record.isPending}
                  onClick={() => record.mutate('acknowledged')}
                  className={BUTTON}
                >
                  Acknowledge disagreement
                </button>
              </>
            )}
          </div>
          {record.error && (
            <p role="alert" className="text-destructive">
              {errorText(record.error)}
            </p>
          )}
        </div>
      )}
    </li>
  );
}

function CertaintySection({
  projectId,
  outcome,
  tip,
  canAssess,
}: {
  projectId: string;
  outcome: EvidenceOutcome;
  tip: EvidenceTable | undefined;
  canAssess: boolean;
}): ReactElement {
  const groups = outcome.contradictions.filter(
    (g) => g.table_version_id === tip?.id
  );
  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-foreground">Certainty (GRADE)</h3>
      {outcome.certainty.length > 0 && (
        <ul className="space-y-2">
          {outcome.certainty.map((c) => (
            <CertaintyRow key={c.id} row={c} />
          ))}
        </ul>
      )}
      {canAssess && tip && !tip.stale && (
        <CertaintyForm
          projectId={projectId}
          outcome={outcome}
          tip={tip}
          contradictionIds={groups.map((g) => g.contradiction_id)}
          unresolved={groups.filter((g) => g.status === 'unresolved').length}
          dissent={groups.flatMap((g) => g.dissent)}
          supersedes={outcome.certainty.find((c) => !c.superseded)?.id ?? null}
        />
      )}
    </div>
  );
}

function CertaintyRow({ row }: { row: Certainty }): ReactElement {
  const unresolved = row.unresolved_contradictions ?? [];
  const dissent = row.dissent ?? [];
  return (
    <li className="rounded-md bg-muted/40 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground">
          Certainty: {row.level ? LEVEL_LABEL[row.level] : 'Unknown'}
        </span>
        {row.superseded && (
          <span className={`${BADGE} bg-muted text-muted-foreground`}>
            Superseded
          </span>
        )}
        {row.stale && <span className={STALE}>Stale</span>}
      </div>
      <p className="text-muted-foreground">
        {row.assessor_name ?? short(row.assessed_by_id)}: {row.rationale}
      </p>
      {unresolved.length > 0 && (
        <p className="text-destructive">
          {unresolved.length} unresolved contradiction(s)
        </p>
      )}
      {dissent.length > 0 && (
        <p className="text-muted-foreground">
          Dissent: {dissent.map((d) => d.explanation).join('; ')}
        </p>
      )}
    </li>
  );
}

function CertaintyForm({
  projectId,
  outcome,
  tip,
  contradictionIds,
  unresolved,
  dissent,
  supersedes,
}: {
  projectId: string;
  outcome: EvidenceOutcome;
  tip: EvidenceTable;
  contradictionIds: string[];
  unresolved: number;
  dissent: Contradiction['dissent'];
  supersedes: string | null;
}): ReactElement {
  const queryClient = useQueryClient();
  const [start, setStart] = useState<'high' | 'low'>('high');
  const [ratings, setRatings] = useState<CertaintyRatings>({});
  const [rationale, setRationale] = useState('');
  const appraisals = useQuery({
    queryKey: ['project', projectId, 'research-engine', 'appraisals'],
    queryFn: () => listAppraisals(projectId),
  });
  const level = deriveLevel(start, ratings);
  // The governing rows GOO-309 reports for each unit; the server re-derives
  // the same set and refuses a mismatch.
  const units = new Set(tip.rows.map((r) => r.unit));
  const appraisalIds = (appraisals.data?.results ?? [])
    .filter(
      (r) =>
        units.has(r.target_key) &&
        r.outcome_key === outcome.outcome_key &&
        r.timepoint === outcome.timepoint &&
        (r.status === 'agreed' || r.status === 'adjudicated')
    )
    .flatMap((r) =>
      r.rows
        .filter(
          (row) =>
            !row.superseded &&
            row.kind ===
              (r.status === 'adjudicated' ? 'adjudicated' : 'independent')
        )
        .map((row) => row.id)
    );
  const assess = useMutation({
    mutationFn: () =>
      assessCertainty(projectId, {
        table_version_id: tip.id,
        starting_level: start,
        ratings,
        level,
        appraisal_assessment_ids:
          ratings.risk_of_bias === null || ratings.risk_of_bias === undefined
            ? []
            : appraisalIds,
        contradiction_ids:
          ratings.inconsistency === null || ratings.inconsistency === undefined
            ? []
            : contradictionIds,
        rationale,
        supersedes_certainty_id: supersedes,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: evidenceKey(projectId) }),
  });
  const submit = (e: FormEvent): void => {
    e.preventDefault();
    assess.mutate();
  };
  return (
    <form
      onSubmit={submit}
      className="space-y-2 rounded-md border border-border p-3 text-sm"
    >
      <label className="block text-muted-foreground">
        Starting level
        <select
          value={start}
          onChange={(e) => setStart(e.target.value as 'high' | 'low')}
          className={`ml-2 ${INPUT}`}
        >
          <option value="high">High</option>
          <option value="low">Low</option>
        </select>
      </label>
      {DOMAINS.map((d) => (
        <label key={d.id} className="block text-muted-foreground">
          {d.label}
          <select
            value={
              ratings[d.id] === null || ratings[d.id] === undefined
                ? ''
                : String(ratings[d.id])
            }
            onChange={(e) =>
              setRatings({
                ...ratings,
                [d.id]:
                  e.target.value === ''
                    ? null
                    : (Number(e.target.value) as Rating),
              })
            }
            className={`ml-2 ${INPUT}`}
          >
            <option value="">Unknown</option>
            <option value="0">No concern (0)</option>
            <option value="-1">Serious (−1)</option>
            <option value="-2">Very serious (−2)</option>
          </select>
        </label>
      ))}
      <p className="text-foreground" aria-live="polite">
        Derived level:{' '}
        <span className="font-medium">
          {level ? LEVEL_LABEL[level] : 'Unknown'}
        </span>
        {unresolved > 0 && (
          <span className="ml-2 text-destructive">
            {unresolved} unresolved contradiction(s)
          </span>
        )}
      </p>
      {dissent.length > 0 && (
        <p className="text-muted-foreground">
          Dissent: {dissent.map((d) => d.explanation).join('; ')}
        </p>
      )}
      <textarea
        aria-label="Rationale"
        required
        maxLength={4000}
        value={rationale}
        onChange={(e) => setRationale(e.target.value)}
        className={`block w-full ${INPUT}`}
      />
      {assess.error && (
        <p role="alert" className="text-destructive">
          {errorText(assess.error)}
        </p>
      )}
      <button
        type="submit"
        disabled={!rationale || assess.isPending}
        className={BUTTON}
      >
        Record certainty
      </button>
    </form>
  );
}
