'use client';

/**
 * GOO-312: one figure's chain, output -> run -> code/environment/data ->
 * hypothesis/protocol, read from the manifest the figure names.
 */

import type { ReactElement } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getFigureLineage } from '@/services/researchEngineService';
import {
  manifestFiles,
  ReproducibilityBadge,
  shortHash,
} from './RunReproducibility';

export const figureLineageKey = (
  projectId: string,
  figureId: string
): readonly string[] =>
  [
    'project',
    projectId,
    'research-engine',
    'figures',
    figureId,
    'lineage',
  ] as const;

function Row({ label, value }: { label: string; value: string }): ReactElement {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-mono">{value}</dd>
    </>
  );
}

export function FigureLineage({
  projectId,
  figureId,
}: {
  projectId: string;
  figureId: string;
}): ReactElement {
  const lineage = useQuery({
    queryKey: figureLineageKey(projectId, figureId),
    queryFn: () => getFigureLineage(projectId, figureId),
    retry: false,
  });
  if (lineage.error) {
    return (
      <p role="alert" className="text-xs text-destructive">
        {lineage.error.message || 'Lineage unavailable'}
      </p>
    );
  }
  if (!lineage.data) {
    return <p className="text-xs text-muted-foreground">Loading lineage…</p>;
  }
  const data = lineage.data;
  const environment = data.environment ?? {};
  return (
    <div aria-label="Figure lineage" className="space-y-1 text-xs">
      <ReproducibilityBadge
        completeness={data.completeness}
        missing={data.missing}
      />
      {data.figure.stale && (
        <p className="text-(--nous-mars)">Stale: a run input changed</p>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        <Row
          label="Output"
          value={`${data.output.name} ${shortHash(data.output.sha256)}`}
        />
        <Row
          label="Run"
          value={`${data.run_id.slice(0, 8)} (${data.run_status})`}
        />
        <Row label="Code" value={shortHash(data.code.sha256)} />
        <Row
          label="Environment"
          value={`${String(environment.template_id ?? '—')} lock ${shortHash(
            environment.lock_sha256
          )}`}
        />
        {manifestFiles(data.inputs).map((input) => (
          <Row
            key={input.name}
            label="Data"
            value={`${input.name} ${shortHash(input.sha256)}`}
          />
        ))}
        <Row label="Hypothesis" value={shortHash(data.hypothesis_sha256)} />
        <Row
          label="Question version"
          value={shortHash(data.question_version_id)}
        />
        <Row label="Protocol" value={shortHash(data.protocol_version_id)} />
      </dl>
    </div>
  );
}
