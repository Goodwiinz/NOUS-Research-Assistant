'use client';

/**
 * GOO-312: a run's reproducibility, read from its ``nous.run-manifest/2``
 * (or the legacy view). "complete" is shown only when the server derived it
 * with an empty missing list; everything else reads "incomplete" with the
 * missing identities as labels. A figure or table output can be registered
 * as a manuscript figure (EDIT; the server enforces the role).
 */

import { useState, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import {
  downloadManifestV2,
  getManifestV2,
  listFigures,
  registerFigure,
} from '@/services/researchEngineService';

export const manifestV2Key = (runId: string): readonly string[] =>
  ['run', runId, 'research-engine', 'manifest-v2'] as const;
export const figuresKey = (projectId: string): readonly string[] =>
  ['project', projectId, 'research-engine', 'figures'] as const;

const PATH_LABELS: Record<string, string> = {
  'code.sha256': 'code hash',
  'environment.template_id': 'sandbox template',
  'environment.lock_sha256': 'environment lock',
  seed: 'seed',
  protocol_version_id: 'protocol version',
  question_version_id: 'hypothesis (question version)',
  started_at: 'start time',
  completed_at: 'end time',
  status: 'run did not complete',
  inputs: 'inputs',
  outputs: 'outputs',
  'schema_version<2': 'recorded before run manifests (legacy run)',
};
const FILE_PATH = /^(inputs|outputs)\[(\d+)\]\.(sha256|artifact_id)$/;

/** A manifest JSON path from ``missing`` as a human label. */
export function missingLabel(path: string): string {
  const file = FILE_PATH.exec(path);
  if (file) {
    const [, group, index, key] = file;
    const noun = group === 'inputs' ? 'input' : 'output';
    const what = key === 'sha256' ? 'hash' : 'retained file';
    return `${noun} ${Number(index) + 1} ${what}`;
  }
  return PATH_LABELS[path] ?? path;
}

export function shortHash(value: unknown): string {
  return typeof value === 'string' && value ? value.slice(0, 12) : '—';
}

export function ReproducibilityBadge({
  completeness,
  missing,
}: {
  completeness: string;
  missing: string[];
}): ReactElement {
  // Never "complete" unless the server derived it with nothing missing.
  const complete = completeness === 'complete' && missing.length === 0;
  return (
    <p
      className={
        complete
          ? 'text-sm font-medium text-(--nous-terra)'
          : 'text-sm font-medium text-(--nous-mars)'
      }
    >
      {complete
        ? 'Reproducibility: complete'
        : `Reproducibility: incomplete (missing: ${
            missing.map(missingLabel).join(', ') || 'unknown'
          })`}
    </p>
  );
}

interface ManifestFile {
  name: string;
  role?: string;
  kind?: string;
  sha256?: string | null;
  byte_size?: number | null;
  artifact_id?: string | null;
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function manifestFiles(value: unknown): ManifestFile[] {
  return Array.isArray(value)
    ? value
        .map(record)
        .filter((item) => typeof item.name === 'string')
        .map((item) => item as unknown as ManifestFile)
    : [];
}

function FileTable({
  caption,
  files,
  action,
}: {
  caption: string;
  files: ManifestFile[];
  action?: (file: ManifestFile) => ReactElement | null;
}): ReactElement {
  return (
    <table className="w-full text-left text-xs">
      <caption className="py-1 text-left font-medium">{caption}</caption>
      <thead className="text-muted-foreground">
        <tr>
          <th className="py-1 pr-2 font-normal">Name</th>
          <th className="py-1 pr-2 font-normal">Role</th>
          <th className="py-1 pr-2 font-normal">SHA-256</th>
          {action && <th className="py-1 font-normal" />}
        </tr>
      </thead>
      <tbody>
        {files.map((file) => (
          <tr key={file.name} className="border-t border-border">
            <td className="py-1 pr-2">{file.name}</td>
            <td className="py-1 pr-2">{file.role ?? file.kind ?? '—'}</td>
            <td className="py-1 pr-2 font-mono">{shortHash(file.sha256)}</td>
            {action && <td className="py-1">{action(file)}</td>}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RegisterFigure({
  projectId,
  file,
}: {
  projectId: string;
  file: ManifestFile;
}): ReactElement {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState('');
  const [caption, setCaption] = useState('');
  const [idempotencyKey, setIdempotencyKey] = useState(() =>
    crypto.randomUUID()
  );
  const figures = useQuery({
    queryKey: figuresKey(projectId),
    queryFn: () => listFigures(projectId),
    enabled: open,
    retry: false,
  });
  const register = useMutation({
    mutationFn: () => {
      const all = figures.data?.figures ?? [];
      const tip = all.find((f) => f.figure_key === key && !f.superseded);
      return registerFigure(projectId, {
        figure_key: key,
        kind: file.role === 'table' ? 'table' : 'figure',
        caption,
        output_artifact_id: file.artifact_id as string,
        supersedes_figure_id: tip?.id ?? null,
        idempotency_key: idempotencyKey,
      });
    },
    onSuccess: () => {
      setIdempotencyKey(crypto.randomUUID());
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: figuresKey(projectId) });
    },
  });
  if (!file.artifact_id)
    return <span className="text-muted-foreground">—</span>;
  if (!open) {
    return (
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        Register as figure
      </Button>
    );
  }
  const id = `figure-${file.artifact_id}`;
  return (
    <div className="flex flex-wrap items-center gap-1">
      <label className="sr-only" htmlFor={`${id}-key`}>
        Figure key
      </label>
      <input
        id={`${id}-key`}
        value={key}
        onChange={(event) => setKey(event.target.value)}
        placeholder="fig-1"
        className="w-20 rounded-md border border-border bg-background px-1 py-0.5"
      />
      <label className="sr-only" htmlFor={`${id}-caption`}>
        Caption
      </label>
      <input
        id={`${id}-caption`}
        value={caption}
        onChange={(event) => setCaption(event.target.value)}
        placeholder="Caption"
        className="min-w-0 rounded-md border border-border bg-background px-1 py-0.5"
      />
      <Button
        size="sm"
        disabled={!key.trim() || !caption.trim() || register.isPending}
        onClick={() => register.mutate()}
      >
        Register
      </Button>
      {register.error && (
        <span role="alert" className="text-destructive">
          {register.error.message || 'Request failed'}
        </span>
      )}
    </div>
  );
}

export function RunReproducibility({
  runId,
  projectId,
}: {
  runId: string;
  projectId?: string | null;
}): ReactElement | null {
  const manifest = useQuery({
    queryKey: manifestV2Key(runId),
    queryFn: () => getManifestV2(runId),
    retry: false,
  });
  if (!manifest.data) return null;
  const { completeness, missing, manifest: body } = manifest.data;
  const doc = record(body);
  const environment = record(doc.environment);
  const inputs = manifestFiles(doc.inputs);
  const outputs = manifestFiles(doc.outputs);
  return (
    <section
      aria-label="Reproducibility"
      className="space-y-2 rounded-lg border border-border p-4"
    >
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">Reproducibility</h2>
        {body && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => void downloadManifestV2(runId)}
          >
            Download manifest
          </Button>
        )}
      </div>
      <ReproducibilityBadge completeness={completeness} missing={missing} />
      {body && (
        <>
          <FileTable caption="Inputs" files={inputs} />
          <FileTable
            caption="Outputs"
            files={outputs}
            action={(file) =>
              projectId && (file.role === 'figure' || file.role === 'table') ? (
                <RegisterFigure projectId={projectId} file={file} />
              ) : null
            }
          />
          <details className="text-xs">
            <summary className="cursor-pointer">Environment</summary>
            <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
              <dt className="text-muted-foreground">Template</dt>
              <dd>{String(environment.template_id ?? '—')}</dd>
              <dt className="text-muted-foreground">Python</dt>
              <dd>{String(environment.python ?? '—')}</dd>
              <dt className="text-muted-foreground">Lock</dt>
              <dd className="font-mono">
                {shortHash(environment.lock_sha256)}
              </dd>
              <dt className="text-muted-foreground">Code</dt>
              <dd className="font-mono">
                {shortHash(record(doc.code).sha256)}
              </dd>
            </dl>
          </details>
        </>
      )}
    </section>
  );
}
