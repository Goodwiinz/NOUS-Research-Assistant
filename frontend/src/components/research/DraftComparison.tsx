'use client';

/**
 * DraftComparison Component
 * Side-by-side comparison of two draft versions
 */

import React, { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ArrowRight, TrendingUp, TrendingDown, Minus } from 'lucide-react';
import {
  projectService,
  type DraftComparison as DraftComparisonType,
} from '@/services/projectService';

export interface DraftComparisonProps {
  comparison: DraftComparisonType;
  draftA?: { content: string };
  draftB?: { content: string };
  /** GOO-314: with both saved-version ids, an "Anchored diff" tab appears. */
  projectId?: string;
  fromDraftId?: string;
  toDraftId?: string;
}

export const DraftComparison: React.FC<DraftComparisonProps> = ({
  comparison,
  draftA,
  draftB,
  projectId,
  fromDraftId,
  toDraftId,
}) => {
  const anchorable = Boolean(projectId && fromDraftId && toDraftId);
  const [tab, setTab] = useState<'counts' | 'anchored'>('counts');
  const anchored = useQuery({
    enabled: anchorable && tab === 'anchored',
    queryKey: ['project', projectId, 'draft-diff', fromDraftId, toDraftId],
    queryFn: () =>
      projectService.diffDrafts(
        projectId as string,
        fromDraftId as string,
        toDraftId as string
      ),
  });
  const diffView = useMemo(() => {
    if (!draftA || !draftB) {
      return null;
    }

    const linesA = draftA.content.split('\n');
    const linesB = draftB.content.split('\n');
    const normalizedA = new Set(
      linesA.map((line) => line.trim()).filter(Boolean)
    );
    const normalizedB = new Set(
      linesB.map((line) => line.trim()).filter(Boolean)
    );

    return {
      versionA: linesA.map((line) => {
        const trimmed = line.trim();
        if (!trimmed) return { text: line, type: 'unchanged' as const };
        return normalizedB.has(trimmed)
          ? { text: line, type: 'unchanged' as const }
          : { text: line, type: 'removed' as const };
      }),
      versionB: linesB.map((line) => {
        const trimmed = line.trim();
        if (!trimmed) return { text: line, type: 'unchanged' as const };
        return normalizedA.has(trimmed)
          ? { text: line, type: 'unchanged' as const }
          : { text: line, type: 'added' as const };
      }),
    };
  }, [draftA, draftB]);

  const getTrendIcon = (diff: number) => {
    if (diff > 0) return <TrendingUp className="h-4 w-4 text-primary" />;
    if (diff < 0) return <TrendingDown className="h-4 w-4 text-destructive" />;
    return <Minus className="h-4 w-4 text-muted-foreground" />;
  };

  const formatDiff = (diff: number) => {
    if (diff > 0) return `+${diff}`;
    return diff.toString();
  };

  const similarityColor =
    comparison.similarity_score > 0.8
      ? 'text-primary'
      : comparison.similarity_score > 0.5
        ? 'text-(--nous-helios)'
        : 'text-destructive';

  const lineClass = (type: 'added' | 'removed' | 'unchanged') => {
    if (type === 'added') return 'bg-primary/10 text-foreground';
    if (type === 'removed') return 'bg-destructive/10 text-destructive';
    return 'text-muted-foreground';
  };

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      <div className="p-4 border-b border-border flex items-center gap-3">
        <h3 className="font-semibold text-foreground">Version comparison</h3>
        {anchorable && (
          <div role="tablist" className="flex gap-1 text-xs">
            {(['counts', 'anchored'] as const).map((value) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={tab === value}
                onClick={() => setTab(value)}
                className={`px-2 py-1 rounded border border-border ${
                  tab === value
                    ? 'bg-primary/10 text-primary'
                    : 'text-muted-foreground'
                }`}
              >
                {value === 'counts' ? 'Counts' : 'Anchored diff'}
              </button>
            ))}
          </div>
        )}
      </div>

      {tab === 'anchored' && (
        <ul aria-label="Anchored diff" className="p-4 space-y-2 text-sm">
          {anchored.data?.hunks.length === 0 && (
            <li className="text-muted-foreground">No sentence changed.</li>
          )}
          {anchored.data?.hunks.map((h) => (
            <li key={`${h.old[0]}-${h.new[0]}`} className="space-y-0.5">
              <span className="text-xs text-muted-foreground">
                {h.op} · v{anchored.data.from.version} [{h.old[0]}, {h.old[1]}]
                → v{anchored.data.to.version} [{h.new[0]}, {h.new[1]}]
              </span>
              {h.old_text && (
                <div className="bg-destructive/10 text-destructive px-1.5 rounded">
                  {h.old_text}
                </div>
              )}
              {h.new_text && (
                <div className="bg-primary/10 text-foreground px-1.5 rounded">
                  {h.new_text}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {tab === 'counts' && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 p-4 border-b border-border">
            <div className="text-center">
              <span className="text-xs text-muted-foreground mb-2 block">
                Version {comparison.version_a.version}
              </span>
              <div className="space-y-2">
                <div className="p-2 bg-muted rounded">
                  <span className="text-lg block tabular-nums text-foreground">
                    {comparison.version_a.word_count}
                  </span>
                  <span className="text-xs text-muted-foreground">words</span>
                </div>
                <div className="p-2 bg-muted rounded">
                  <span className="text-lg block tabular-nums text-foreground">
                    {comparison.version_a.citation_count}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    citations
                  </span>
                </div>
              </div>
              <span className="text-xs text-muted-foreground mt-2 block">
                {new Date(comparison.version_a.created_at).toLocaleDateString()}
              </span>
            </div>

            <div className="flex flex-col items-center justify-center">
              <ArrowRight className="h-6 w-6 text-muted-foreground mb-4" />

              <div className="space-y-3 w-full">
                <div className="flex items-center justify-center gap-2">
                  {getTrendIcon(comparison.word_count_diff)}
                  <span className="text-sm tabular-nums">
                    {formatDiff(comparison.word_count_diff)} words
                  </span>
                </div>
                <div className="flex items-center justify-center gap-2">
                  {getTrendIcon(comparison.citation_count_diff)}
                  <span className="text-sm tabular-nums">
                    {formatDiff(comparison.citation_count_diff)} citations
                  </span>
                </div>
                <div className="p-2 bg-muted rounded text-center">
                  <span
                    className={`text-lg tabular-nums block ${similarityColor}`}
                  >
                    {(comparison.similarity_score * 100).toFixed(1)}%
                  </span>
                  <span className="text-xs text-muted-foreground">
                    similarity
                  </span>
                </div>
              </div>
            </div>

            <div className="text-center">
              <span className="text-xs text-muted-foreground mb-2 block">
                Version {comparison.version_b.version}
              </span>
              <div className="space-y-2">
                <div className="p-2 bg-muted rounded">
                  <span className="text-lg block tabular-nums text-foreground">
                    {comparison.version_b.word_count}
                  </span>
                  <span className="text-xs text-muted-foreground">words</span>
                </div>
                <div className="p-2 bg-muted rounded">
                  <span className="text-lg block tabular-nums text-foreground">
                    {comparison.version_b.citation_count}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    citations
                  </span>
                </div>
              </div>
              <span className="text-xs text-muted-foreground mt-2 block">
                {new Date(comparison.version_b.created_at).toLocaleDateString()}
              </span>
            </div>
          </div>

          {diffView && (
            <div className="grid grid-cols-2 divide-x divide-border">
              <div className="p-4 max-h-[400px] overflow-y-auto">
                <span className="text-xs text-muted-foreground mb-2 block">
                  Version {comparison.version_a.version}
                </span>
                <div className="text-sm font-(--nous-font-mono) whitespace-pre-wrap space-y-1">
                  {diffView.versionA.map((line, idx) => (
                    <div
                      key={`a-${idx}`}
                      className={`px-1.5 py-0.5 rounded ${lineClass(line.type)}`}
                    >
                      {line.text || ' '}
                    </div>
                  ))}
                </div>
              </div>
              <div className="p-4 max-h-[400px] overflow-y-auto">
                <span className="text-xs text-muted-foreground mb-2 block">
                  Version {comparison.version_b.version}
                </span>
                <div className="text-sm font-(--nous-font-mono) whitespace-pre-wrap space-y-1">
                  {diffView.versionB.map((line, idx) => (
                    <div
                      key={`b-${idx}`}
                      className={`px-1.5 py-0.5 rounded ${lineClass(line.type)}`}
                    >
                      {line.text || ' '}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default DraftComparison;
