'use client';

import { useState, type FormEvent, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@/hooks/useAuth';
import {
  adjudicateAppraisal,
  exportAppraisals,
  listAppraisals,
  submitAppraisal,
  type Appraisal,
  type AppraisalDesign,
  type AppraisalDomain,
  type AppraisalInstrument,
  type AppraisalResult,
  type AppraisalStatus,
  type AppraisalSubmit,
  type ProjectRoleAssignment,
} from '@/services/researchEngineService';

interface AppraisalPanelProps {
  projectId: string;
  roles: ProjectRoleAssignment[];
  readOnly?: boolean;
}

type Judgment = NonNullable<AppraisalDomain['judgment']>;
type SignalAnswer = NonNullable<
  NonNullable<AppraisalDomain['signals']>[string]
>;

const STATUS: Record<AppraisalStatus, { label: string; tone: string }> = {
  awaiting_independent: {
    label: 'Awaiting',
    tone: 'bg-muted text-muted-foreground',
  },
  agreed: { label: 'Agreed', tone: 'bg-primary/10 text-primary' },
  conflict: { label: 'Conflict', tone: 'bg-destructive/10 text-destructive' },
  adjudicated: { label: 'Adjudicated', tone: 'bg-primary/10 text-primary' },
};
const STATUS_TEXT: Record<AppraisalStatus, string> = {
  awaiting_independent: 'Awaiting independent assessments',
  agreed: 'Assessors agree',
  conflict: 'Assessors disagree; needs an adjudicator',
  adjudicated: 'Resolved by an adjudicator',
};
const JUDGMENT_LABEL: Record<Judgment, string> = {
  low: 'Low',
  some_concerns: 'Some concerns',
  high: 'High',
};
const DESIGN_LABEL: Record<AppraisalDesign, string> = {
  randomized_parallel_group: 'Randomized, parallel group',
  randomized_cluster: 'Randomized, cluster',
  randomized_crossover: 'Randomized, crossover',
  non_randomized_intervention: 'Non-randomized intervention',
  cohort: 'Cohort',
  case_control: 'Case-control',
  cross_sectional: 'Cross-sectional',
  other: 'Other',
};

const queryKey = (projectId: string): readonly string[] =>
  ['project', projectId, 'research-engine', 'appraisals'] as const;

function unitLabel(targetKey: string): string {
  const [kind, id = ''] = targetKey.split(':');
  return `${kind === 'study' ? 'Study' : 'Report'} ${id.slice(0, 8)}`;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : 'Request failed.';
}

/**
 * GOO-309: RoB 2 appraisal per result (unit, outcome, timepoint). The server
 * applies the reveal rule, so a peer's answers never reach this panel before
 * the viewer's own; only the instrument's structure is shown, never question
 * text. Unanswered stays "Unknown" and is posted as null.
 */
export function AppraisalPanel({
  projectId,
  roles,
  readOnly = false,
}: AppraisalPanelProps): ReactElement {
  const userId = useAuth().user?.id;
  const mine = new Set(
    roles.filter((r) => r.user_id === userId).map((r) => r.role)
  );
  const listing = useQuery({
    queryKey: queryKey(projectId),
    queryFn: () => listAppraisals(projectId),
  });
  const data = listing.data;
  return (
    <section className="rounded-xl border border-border bg-card p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="font-medium text-foreground">Risk of bias</h2>
          {data?.instrument && (
            <p className="mt-1 text-sm text-muted-foreground">
              {data.instrument.key.toUpperCase()} {data.instrument.version} ·{' '}
              {data.instrument.licence}; {data.instrument.encoding}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => void exportAppraisals(projectId)}
          className="rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-muted"
        >
          Export
        </button>
      </div>
      {listing.isLoading ? (
        <p role="status" className="mt-4 text-sm text-muted-foreground">
          Loading appraisals…
        </p>
      ) : listing.error ? (
        <p role="alert" className="mt-4 text-sm text-destructive">
          {errorText(listing.error)}
        </p>
      ) : !data?.instrument || !data.protocol_version_id ? (
        <p className="mt-4 text-sm text-muted-foreground">
          The approved protocol declares no appraisal instrument.
        </p>
      ) : data.results.length === 0 ? (
        <p className="mt-4 text-sm text-muted-foreground">
          No results to appraise yet.
        </p>
      ) : (
        <ul className="mt-4 space-y-4">
          {data.results.map((result) => (
            <ResultCard
              key={`${result.target_key}|${result.outcome_key}|${result.timepoint}`}
              projectId={projectId}
              protocolVersionId={data.protocol_version_id as string}
              instrument={data.instrument as AppraisalInstrument}
              result={result}
              userId={userId}
              canReview={mine.has('reviewer') && !readOnly}
              canAdjudicate={mine.has('adjudicator') && !readOnly}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function ResultCard({
  projectId,
  protocolVersionId,
  instrument,
  result,
  userId,
  canReview,
  canAdjudicate,
}: {
  projectId: string;
  protocolVersionId: string;
  instrument: AppraisalInstrument;
  result: AppraisalResult;
  userId: string | undefined;
  canReview: boolean;
  canAdjudicate: boolean;
}): ReactElement {
  const names = new Map(instrument.domains.map((d) => [d.id, d.name]));
  const status = STATUS[result.status];
  const myTip = result.rows.find(
    (r) => r.assessor_id === userId && r.kind === 'independent' && !r.superseded
  );
  const reviewOpen =
    canReview &&
    (result.status === 'awaiting_independent' || instrument.mode === 'single');
  return (
    <li className="rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium text-foreground">
          {unitLabel(result.target_key)} · {result.outcome_key} ·{' '}
          {result.timepoint}
        </h3>
        <span
          aria-label={`Status: ${STATUS_TEXT[result.status]}`}
          className={`rounded-full px-2 py-0.5 text-xs font-medium ${status.tone}`}
        >
          {status.label}
        </span>
        {result.stale && (
          <span
            aria-label="Stale: cited evidence or protocol changed"
            className="rounded-full bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive"
          >
            Stale
          </span>
        )}
      </div>
      {result.unresolved_domains.length > 0 && (
        <p className="mt-2 text-sm text-muted-foreground">
          Unresolved:{' '}
          {result.unresolved_domains.map((d) => names.get(d) ?? d).join(', ')}
        </p>
      )}
      {result.rows.length > 0 && (
        <ul className="mt-3 space-y-3">
          {result.rows.map((row) => (
            <AssessmentRow
              key={row.id}
              row={row}
              names={names}
              result={result}
            />
          ))}
        </ul>
      )}
      {reviewOpen && (
        <AppraisalForm
          mode="review"
          projectId={projectId}
          protocolVersionId={protocolVersionId}
          instrument={instrument}
          result={result}
          supersedes={myTip?.id ?? null}
        />
      )}
      {canAdjudicate && result.status === 'conflict' && (
        <AppraisalForm
          mode="adjudicate"
          projectId={projectId}
          protocolVersionId={protocolVersionId}
          instrument={instrument}
          result={result}
          supersedes={
            result.rows.find((r) => r.kind === 'adjudicated' && !r.superseded)
              ?.id ?? null
          }
        />
      )}
    </li>
  );
}

function AssessmentRow({
  row,
  names,
  result,
}: {
  row: Appraisal;
  names: Map<string, string>;
  result: AppraisalResult;
}): ReactElement {
  const options = new Map(
    (result.evidence_options ?? []).map((o) => [o.id, o])
  );
  return (
    <li className="rounded-md bg-muted/40 p-3 text-sm">
      <p className="text-foreground">
        <span className="font-medium">
          {row.assessor_name ?? row.assessor_id.slice(0, 8)}
        </span>{' '}
        · {row.kind === 'adjudicated' ? 'Adjudication' : 'Independent'} ·{' '}
        {row.instrument_key}@{row.instrument_version}
        {row.superseded && ' · superseded'}
        {row.stale && ' · stale'}
      </p>
      <p className="text-muted-foreground">
        {DESIGN_LABEL[row.study_design as AppraisalDesign] ?? row.study_design}
        {row.applicability === 'not_applicable'
          ? ' · Not applicable (not scored)'
          : ` · Overall: ${row.overall ? JUDGMENT_LABEL[row.overall as Judgment] : 'Unknown'}`}
      </p>
      {row.rationale && <p className="mt-1 text-foreground">{row.rationale}</p>}
      <dl className="mt-2 space-y-1">
        {Object.entries(row.domains).map(([id, domain]) => (
          <div key={id}>
            <dt className="inline font-medium text-foreground">
              {names.get(id) ?? id}:
            </dt>{' '}
            <dd className="inline text-muted-foreground">
              {domain.judgment ? JUDGMENT_LABEL[domain.judgment] : 'Unknown'}
              {domain.rationale && ` — ${domain.rationale}`}
              {(domain.evidence ?? []).map((ref) => {
                const option = options.get(ref.id);
                return (
                  <span key={ref.id} className="block pl-3">
                    {option
                      ? `${String(option.value ?? option.missingness)}${option.quote ? ` “${option.quote}”` : ''}`
                      : `${ref.kind} ${ref.id.slice(0, 8)}`}
                  </span>
                );
              })}
            </dd>
          </div>
        ))}
      </dl>
    </li>
  );
}

interface DomainDraft {
  judgment: Judgment | '';
  signals: Record<string, SignalAnswer | ''>;
  rationale: string;
  evidence: string[];
}

function emptyDraft(
  instrument: AppraisalInstrument
): Record<string, DomainDraft> {
  return Object.fromEntries(
    instrument.domains.map((d) => [
      d.id,
      {
        judgment: '',
        signals: Object.fromEntries(d.signals.map((s) => [s, ''])),
        rationale: '',
        evidence: [],
      },
    ])
  );
}

function AppraisalForm({
  mode,
  projectId,
  protocolVersionId,
  instrument,
  result,
  supersedes,
}: {
  mode: 'review' | 'adjudicate';
  projectId: string;
  protocolVersionId: string;
  instrument: AppraisalInstrument;
  result: AppraisalResult;
  supersedes: string | null;
}): ReactElement {
  const queryClient = useQueryClient();
  const [design, setDesign] = useState<AppraisalDesign>(
    'randomized_parallel_group'
  );
  const [applicable, setApplicable] = useState(true);
  const [domains, setDomains] = useState(() => emptyDraft(instrument));
  const [overall, setOverall] = useState<Judgment | ''>('');
  const [rationale, setRationale] = useState('');
  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: queryKey(projectId) });
  };
  const missingRationale = applicable
    ? instrument.domains
        .filter(
          (d) => domains[d.id].judgment && !domains[d.id].rationale.trim()
        )
        .map((d) => d.name)
    : [];
  const mutation = useMutation({
    mutationFn: (): Promise<Appraisal> => {
      const body: AppraisalSubmit = {
        protocol_version_id: protocolVersionId,
        instrument_key: instrument.key,
        instrument_version: instrument.version,
        study_id: result.study_id ?? null,
        report_id: result.report_id ?? null,
        outcome_key: result.outcome_key,
        timepoint: result.timepoint,
        study_design: design,
        applicability: applicable ? 'applicable' : 'not_applicable',
        domains: applicable
          ? Object.fromEntries(
              Object.entries(domains).map(([id, d]) => [
                id,
                {
                  judgment: d.judgment || null,
                  signals: Object.fromEntries(
                    Object.entries(d.signals).map(([s, v]) => [s, v || null])
                  ),
                  rationale: d.rationale.trim() || null,
                  evidence: d.evidence.map((ref) => ({
                    kind: 'accepted_value' as const,
                    id: ref,
                  })),
                },
              ])
            )
          : {},
        overall: applicable ? overall || null : null,
        supersedes_assessment_id: supersedes,
        idempotency_key: crypto.randomUUID(),
      };
      if (mode === 'review') return submitAppraisal(projectId, body);
      return adjudicateAppraisal(projectId, {
        ...body,
        resolves_assessment_ids: result.rows
          .filter((r) => r.kind === 'independent' && !r.superseded)
          .map((r) => r.id),
        rationale: rationale.trim(),
      });
    },
    onSuccess: refresh,
    // A stale or revealed result refetches so the form shows the new state.
    onError: refresh,
  });
  const update = (id: string, patch: Partial<DomainDraft>): void =>
    setDomains((current) => ({
      ...current,
      [id]: { ...current[id], ...patch },
    }));
  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    mutation.mutate();
  };
  const prefix = `${mode}-${result.target_key}-${result.outcome_key}-${result.timepoint}`;
  const disabled =
    mutation.isPending ||
    missingRationale.length > 0 ||
    (mode === 'adjudicate' && !rationale.trim());
  return (
    <form
      onSubmit={onSubmit}
      aria-label={mode === 'review' ? 'Your appraisal' : 'Adjudication'}
      className="mt-4 space-y-3 border-t border-border pt-4 text-sm"
    >
      <h4 className="font-medium text-foreground">
        {mode === 'review' ? 'Your appraisal' : 'Adjudicate'}
      </h4>
      <label className="block">
        <span className="text-muted-foreground">Study design</span>
        <select
          value={design}
          onChange={(e) => setDesign(e.target.value as AppraisalDesign)}
          className="mt-1 block rounded-md border border-border bg-background px-2 py-1"
        >
          {instrument.designs.map((d) => (
            <option key={d} value={d}>
              {DESIGN_LABEL[d as AppraisalDesign] ?? d}
            </option>
          ))}
        </select>
      </label>
      <fieldset>
        <legend className="text-muted-foreground">Applicability</legend>
        {[true, false].map((value) => (
          <label
            key={String(value)}
            className="mr-4 inline-flex items-center gap-1"
          >
            <input
              type="radio"
              name={`${prefix}-applicability`}
              checked={applicable === value}
              onChange={() => setApplicable(value)}
            />
            {value ? 'Applicable' : 'Not applicable (not scored)'}
          </label>
        ))}
      </fieldset>
      {applicable &&
        instrument.domains.map((d) => (
          <fieldset key={d.id} className="rounded-md border border-border p-3">
            <legend className="px-1 font-medium text-foreground">
              {d.name}
            </legend>
            <label className="block">
              <span className="sr-only">{d.name} judgement</span>
              <select
                aria-label={`${d.name} judgement`}
                value={domains[d.id].judgment}
                onChange={(e) =>
                  update(d.id, { judgment: e.target.value as Judgment | '' })
                }
                className="rounded-md border border-border bg-background px-2 py-1"
              >
                <option value="">Unknown</option>
                {instrument.judgments.map((j) => (
                  <option key={j} value={j}>
                    {JUDGMENT_LABEL[j as Judgment] ?? j}
                  </option>
                ))}
              </select>
            </label>
            <details className="mt-2">
              <summary className="cursor-pointer text-muted-foreground">
                Signalling answers
              </summary>
              <div className="mt-2 flex flex-wrap gap-2">
                {d.signals.map((s) => (
                  <label key={s} className="inline-flex items-center gap-1">
                    {s}
                    <select
                      aria-label={`Signalling question ${s}`}
                      value={domains[d.id].signals[s]}
                      onChange={(e) =>
                        update(d.id, {
                          signals: {
                            ...domains[d.id].signals,
                            [s]: e.target.value as SignalAnswer | '',
                          },
                        })
                      }
                      className="rounded-md border border-border bg-background px-1 py-0.5"
                    >
                      <option value="">Unanswered</option>
                      {instrument.responses.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
            </details>
            <label className="mt-2 block">
              <span className="text-muted-foreground">Rationale</span>
              <textarea
                value={domains[d.id].rationale}
                onChange={(e) => update(d.id, { rationale: e.target.value })}
                maxLength={4000}
                rows={2}
                className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1"
              />
            </label>
            {(result.evidence_options ?? []).length > 0 && (
              <fieldset className="mt-2">
                <legend className="text-muted-foreground">Evidence</legend>
                {(result.evidence_options ?? []).map((o) => (
                  <label key={o.id} className="block">
                    <input
                      type="checkbox"
                      checked={domains[d.id].evidence.includes(o.id)}
                      onChange={(e) =>
                        update(d.id, {
                          evidence: e.target.checked
                            ? [...domains[d.id].evidence, o.id]
                            : domains[d.id].evidence.filter((x) => x !== o.id),
                        })
                      }
                    />{' '}
                    {String(o.value ?? o.missingness)}
                    {o.quote && ` “${o.quote}”`}
                  </label>
                ))}
              </fieldset>
            )}
          </fieldset>
        ))}
      {applicable && (
        <label className="block">
          <span className="text-muted-foreground">Overall</span>
          <select
            aria-label="Overall judgement"
            value={overall}
            onChange={(e) => setOverall(e.target.value as Judgment | '')}
            className="mt-1 block rounded-md border border-border bg-background px-2 py-1"
          >
            <option value="">Unknown</option>
            {instrument.judgments.map((j) => (
              <option key={j} value={j}>
                {JUDGMENT_LABEL[j as Judgment] ?? j}
              </option>
            ))}
          </select>
        </label>
      )}
      {mode === 'adjudicate' && (
        <label className="block">
          <span className="text-muted-foreground">Adjudication rationale</span>
          <textarea
            value={rationale}
            onChange={(e) => setRationale(e.target.value)}
            maxLength={4000}
            rows={2}
            className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1"
          />
        </label>
      )}
      {missingRationale.length > 0 && (
        <p className="text-muted-foreground">
          A judgement needs a rationale: {missingRationale.join(', ')}.
        </p>
      )}
      {mutation.error && (
        <p role="alert" className="text-destructive">
          {errorText(mutation.error)}
        </p>
      )}
      <button
        type="submit"
        disabled={disabled}
        className="rounded-md bg-primary px-3 py-1.5 font-medium text-primary-foreground disabled:opacity-50"
      >
        {mode === 'review' ? 'Submit appraisal' : 'Record adjudication'}
      </button>
    </form>
  );
}
