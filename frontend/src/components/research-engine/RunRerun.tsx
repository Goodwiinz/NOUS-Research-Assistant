'use client';

/**
 * GOO-313: rerun a completed run from a fresh environment. Eligibility
 * reasons come from the server as structured codes and render as labels.
 * The comparison rule is declared here, one row per manifest output, and
 * the server hashes it before anything runs; a retry reuses it. Each
 * attempt shows "executed" and "reproduced" as separate results. Admission,
 * cancel and retry need the reviewer role; the server enforces it.
 */

import { useState, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import {
  admitRerun,
  cancelRerun,
  downloadRerunComparison,
  getRerunEligibility,
  listReruns,
  retryRerun,
  type Rerun,
  type RerunAttempt,
} from '@/services/researchEngineService';
import { missingLabel, shortHash } from './RunReproducibility';

export const rerunEligibilityKey = (runId: string): readonly string[] =>
  ['run', runId, 'research-engine', 'rerun-eligibility'] as const;
export const rerunsKey = (runId: string): readonly string[] =>
  ['run', runId, 'research-engine', 'reruns'] as const;

const REASONS: Record<string, string> = {
  no_manifest: 'no run manifest (legacy run)',
  run_not_completed: 'run did not complete',
  run_not_conformant: 'run did not conform to its approved plan',
  unsupported_workflow_shape: 'needs exactly one analyze step',
  template_mismatch: 'sandbox template differs',
  environment_lock_mismatch: 'environment lock differs after install',
  environment_install_failed: 'environment lock failed to install',
  code_mismatch: 'restored code hash differs',
  sandbox_unavailable: 'sandbox unavailable',
  project_lifecycle: 'project archived or deleted',
  cancelled_by_reviewer: 'cancelled by a reviewer',
  lease_expired: 'worker stopped (lease expired)',
  timeout: 'timed out',
  output_storage_failed: 'outputs could not be stored',
};
const PREFIXED: Record<string, (rest: string) => string> = {
  manifest_incomplete: (path) => `manifest missing: ${missingLabel(path)}`,
  artifact_missing: (name) => `archived file missing: ${name}`,
  artifact_corrupt: (name) => `archived file corrupt: ${name}`,
  input_mismatch: (name) => `restored input hash differs: ${name}`,
  missing_output: (name) => `output not written: ${name}`,
  exit_code: (code) => `command exited with ${code}`,
};

/** A server reason code as a human label. */
export function rerunReasonLabel(reason: string): string {
  const cut = reason.indexOf(':');
  if (cut > 0) {
    const label = PREFIXED[reason.slice(0, cut)];
    if (label) return label(reason.slice(cut + 1));
  }
  return REASONS[reason] ?? reason;
}

interface RuleRow {
  name: string;
  mode: 'bytes' | 'json_numeric';
  pointers: string;
  abs: string;
  rel: string;
}

function rowsFrom(rule: unknown): RuleRow[] {
  const outputs = (rule as { outputs?: unknown } | null)?.outputs;
  return Array.isArray(outputs)
    ? outputs
        .map((o) => (o as { name?: unknown }).name)
        .filter((name): name is string => typeof name === 'string')
        .map((name) => ({
          name,
          mode: 'bytes',
          pointers: '',
          abs: '0',
          rel: '0',
        }))
    : [];
}

const tolerance = (value: string): number | null => {
  const parsed = Number(value);
  return value.trim() !== '' && Number.isFinite(parsed) && parsed >= 0
    ? parsed
    : null;
};
const pointerList = (value: string): string[] =>
  value
    .split(',')
    .map((p) => p.trim())
    .filter(Boolean);

/** The rule to submit, or null while a numeric row is incomplete. */
export function ruleFrom(rows: RuleRow[]): Record<string, unknown> | null {
  if (rows.length === 0) return null;
  const outputs: Record<string, unknown>[] = [];
  for (const row of rows) {
    if (row.mode === 'bytes') {
      outputs.push({ name: row.name, mode: 'bytes' });
      continue;
    }
    const pointers = pointerList(row.pointers);
    const abs = tolerance(row.abs);
    const rel = tolerance(row.rel);
    if (!pointers.length || !pointers.every((p) => p.startsWith('/'))) {
      return null;
    }
    if (abs === null || rel === null) return null;
    outputs.push({ name: row.name, mode: 'json_numeric', pointers, abs, rel });
  }
  return { schema: 'nous.rerun-rule/1', outputs };
}

function RuleTable({
  rows,
  onChange,
}: {
  rows: RuleRow[];
  onChange: (rows: RuleRow[]) => void;
}): ReactElement {
  const set = (index: number, patch: Partial<RuleRow>): void =>
    onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  return (
    <table className="w-full text-left text-xs">
      <caption className="py-1 text-left font-medium">
        Comparison rule (declared before running; every output)
      </caption>
      <thead className="text-muted-foreground">
        <tr>
          <th className="py-1 pr-2 font-normal">Output</th>
          <th className="py-1 pr-2 font-normal">Mode</th>
          <th className="py-1 pr-2 font-normal">JSON pointers</th>
          <th className="py-1 pr-2 font-normal">Abs</th>
          <th className="py-1 font-normal">Rel</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => {
          const id = `rerun-rule-${index}`;
          const numeric = row.mode === 'json_numeric';
          return (
            <tr key={row.name} className="border-t border-border">
              <td className="py-1 pr-2">{row.name}</td>
              <td className="py-1 pr-2">
                <label className="sr-only" htmlFor={`${id}-mode`}>
                  {`Mode for ${row.name}`}
                </label>
                <select
                  id={`${id}-mode`}
                  value={row.mode}
                  onChange={(event) =>
                    set(index, { mode: event.target.value as RuleRow['mode'] })
                  }
                  className="rounded-md border border-border bg-background px-1 py-0.5"
                >
                  <option value="bytes">bytes</option>
                  <option value="json_numeric">numeric tolerance</option>
                </select>
              </td>
              {(['pointers', 'abs', 'rel'] as const).map((field) => (
                <td key={field} className="py-1 pr-2">
                  <label className="sr-only" htmlFor={`${id}-${field}`}>
                    {`${field} for ${row.name}`}
                  </label>
                  <input
                    id={`${id}-${field}`}
                    value={row[field]}
                    disabled={!numeric}
                    placeholder={field === 'pointers' ? '/estimate' : '0'}
                    onChange={(event) =>
                      set(index, { [field]: event.target.value })
                    }
                    className="w-full min-w-0 rounded-md border border-border bg-background px-1 py-0.5 disabled:opacity-50"
                  />
                </td>
              ))}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Badge({
  on,
  label,
}: {
  on: boolean | null;
  label: string;
}): ReactElement {
  const text =
    on === null ? `${label}: n/a` : on ? label : `Not ${label.toLowerCase()}`;
  const tone =
    on === null
      ? 'text-muted-foreground'
      : on
        ? 'text-(--nous-terra)'
        : 'text-(--nous-mars)';
  return (
    <span
      className={`rounded border border-border px-1.5 py-0.5 text-xs font-medium ${tone}`}
    >
      {text}
    </span>
  );
}

function AttemptView({ attempt }: { attempt: RerunAttempt }): ReactElement {
  const executed = attempt.status === 'executed';
  return (
    <li className="space-y-1 border-t border-border pt-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-medium">Attempt {attempt.attempt}</span>
        <span className="text-muted-foreground">{attempt.status}</span>
        <Badge on={executed} label="Executed" />
        <Badge
          on={executed ? attempt.reproduction === 'reproduced' : null}
          label="Reproduced"
        />
      </div>
      {attempt.reasons && attempt.reasons.length > 0 && (
        <ul className="flex flex-wrap gap-1 text-xs">
          {attempt.reasons.map((reason) => (
            <li key={reason} className="rounded bg-muted px-1.5 py-0.5">
              {rerunReasonLabel(reason)}
            </li>
          ))}
        </ul>
      )}
      {attempt.comparison && (
        <table className="w-full text-left text-xs">
          <caption className="py-1 text-left font-medium">Comparison</caption>
          <thead className="text-muted-foreground">
            <tr>
              <th className="py-1 pr-2 font-normal">Output</th>
              <th className="py-1 pr-2 font-normal">Mode</th>
              <th className="py-1 pr-2 font-normal">Expected</th>
              <th className="py-1 pr-2 font-normal">Actual</th>
              <th className="py-1 font-normal">Result</th>
            </tr>
          </thead>
          <tbody>
            {attempt.comparison.map((row) => (
              <tr key={row.name} className="border-t border-border">
                <td className="py-1 pr-2">{row.name}</td>
                <td className="py-1 pr-2">{row.mode}</td>
                <td className="py-1 pr-2 font-mono">
                  {shortHash(row.expected_sha256)}
                </td>
                <td className="py-1 pr-2 font-mono">
                  {shortHash(row.actual_sha256)}
                </td>
                <td className="py-1">
                  {row.equal
                    ? 'identical bytes'
                    : (row.numeric ?? [])
                        .map(
                          (n) =>
                            `${String(n.pointer)}: ${
                              n.within ? 'within' : 'outside'
                            } (Δ ${n.abs_diff === null ? '—' : String(n.abs_diff)})`
                        )
                        .join('; ') ||
                      (row.reason ? rerunReasonLabel(row.reason) : 'differs')}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </li>
  );
}

function RerunView({
  rerun,
  runId,
}: {
  rerun: Rerun;
  runId: string;
}): ReactElement {
  const queryClient = useQueryClient();
  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: rerunsKey(runId) });
  };
  const cancel = useMutation({
    mutationFn: () => cancelRerun(rerun.id),
    onSuccess: refresh,
  });
  const retry = useMutation({
    mutationFn: () => retryRerun(rerun.id),
    onSuccess: refresh,
  });
  const latest = rerun.attempts[rerun.attempts.length - 1];
  const inFlight = latest?.status === 'queued' || latest?.status === 'running';
  const retryable =
    !!latest &&
    !inFlight &&
    latest.status !== 'executed' &&
    !!latest.finished_at;
  const error = cancel.error ?? retry.error;
  return (
    <div className="space-y-1 rounded-md border border-border p-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-mono">rule {shortHash(rerun.rule_hash)}</span>
        {inFlight && (
          <Button
            size="sm"
            variant="outline"
            disabled={cancel.isPending}
            onClick={() => cancel.mutate()}
          >
            Cancel
          </Button>
        )}
        {retryable && (
          <Button
            size="sm"
            variant="outline"
            disabled={retry.isPending}
            onClick={() => retry.mutate()}
          >
            Retry
          </Button>
        )}
        <Button
          size="sm"
          variant="ghost"
          onClick={() => void downloadRerunComparison(rerun.id)}
        >
          Download comparison
        </Button>
        {error && (
          <span role="alert" className="text-destructive">
            {error.message || 'Request failed'}
          </span>
        )}
      </div>
      <ul className="space-y-2">
        {rerun.attempts.map((attempt) => (
          <AttemptView key={attempt.attempt} attempt={attempt} />
        ))}
      </ul>
    </div>
  );
}

export function RunRerun({ runId }: { runId: string }): ReactElement | null {
  const queryClient = useQueryClient();
  const eligibility = useQuery({
    queryKey: rerunEligibilityKey(runId),
    queryFn: () => getRerunEligibility(runId),
    retry: false,
  });
  const reruns = useQuery({
    queryKey: rerunsKey(runId),
    queryFn: () => listReruns(runId),
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.reruns.some((r) =>
        r.attempts.some((a) => a.status === 'queued' || a.status === 'running')
      )
        ? 3000
        : false,
  });
  const [rows, setRows] = useState<RuleRow[] | null>(null);
  const [idempotencyKey, setIdempotencyKey] = useState(() =>
    crypto.randomUUID()
  );
  const ruleRows = rows ?? rowsFrom(eligibility.data?.default_rule);
  const rule = ruleFrom(ruleRows);
  const admit = useMutation({
    mutationFn: () =>
      admitRerun(runId, { rule, idempotency_key: idempotencyKey }),
    onSuccess: () => {
      setIdempotencyKey(crypto.randomUUID());
      void queryClient.invalidateQueries({ queryKey: rerunsKey(runId) });
    },
  });
  if (!eligibility.data) return null;
  const { eligible, reasons } = eligibility.data;
  return (
    <section
      aria-label="Rerun from fresh environment"
      className="space-y-2 rounded-lg border border-border p-4"
    >
      <h2 className="text-sm font-semibold">Rerun from fresh environment</h2>
      {!eligible && (
        <div className="space-y-1">
          <p className="text-sm font-medium text-(--nous-mars)">
            Not eligible for rerun
          </p>
          <ul className="flex flex-wrap gap-1 text-xs">
            {reasons.map((reason) => (
              <li key={reason} className="rounded bg-muted px-1.5 py-0.5">
                {rerunReasonLabel(reason)}
              </li>
            ))}
          </ul>
        </div>
      )}
      {eligible && <RuleTable rows={ruleRows} onChange={setRows} />}
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          disabled={!eligible || rule === null || admit.isPending}
          onClick={() => admit.mutate()}
        >
          Rerun
        </Button>
        {admit.error && (
          <span role="alert" className="text-xs text-destructive">
            {admit.error.message || 'Request failed'}
          </span>
        )}
      </div>
      {(reruns.data?.reruns ?? []).map((rerun) => (
        <RerunView key={rerun.id} rerun={rerun} runId={runId} />
      ))}
    </section>
  );
}
