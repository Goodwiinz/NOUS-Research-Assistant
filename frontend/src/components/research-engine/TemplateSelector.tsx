'use client';

import { useState, type ReactElement } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { FileText, Loader2, Sparkles } from 'lucide-react';
import {
  getTemplateDetail,
  listTemplates,
} from '@/services/researchEngineService';
import type {
  BlueprintTemplateDetail,
  BlueprintTemplateSummary,
} from '@/services/researchEngineService';

export interface TemplateSelectorProps {
  onSelect: (template: BlueprintTemplateDetail | null) => void;
}

export function TemplateSelector({
  onSelect,
}: TemplateSelectorProps): ReactElement {
  const queryClient = useQueryClient();
  const templatesQuery = useQuery({
    queryKey: ['research-engine', 'templates'],
    queryFn: listTemplates,
  });
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [failedSlug, setFailedSlug] = useState<string | null>(null);

  const handleTemplateSelect = async (
    template: BlueprintTemplateSummary
  ): Promise<void> => {
    setSelectedSlug(template.slug);
    setFailedSlug(null);
    try {
      const detail = await queryClient.fetchQuery({
        queryKey: ['research-engine', 'templates', template.slug],
        queryFn: () => getTemplateDetail(template.slug),
      });
      onSelect(detail);
    } catch {
      setFailedSlug(template.slug);
    } finally {
      setSelectedSlug(null);
    }
  };

  if (templatesQuery.isLoading) {
    return (
      <div>
        <div className="h-6 w-64 rounded bg-muted animate-pulse mb-2" />
        <div className="h-4 w-80 rounded bg-muted animate-pulse mb-6" />
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div
              key={i}
              className="h-32 rounded-xl border border-border bg-card animate-pulse"
            />
          ))}
        </div>
        <span className="sr-only" role="status">
          Loading templates
        </span>
      </div>
    );
  }

  if (templatesQuery.isError) {
    return (
      <div
        role="alert"
        className="flex items-start gap-3 p-4 rounded-xl border border-destructive/30 bg-destructive/5"
      >
        <div className="flex-1">
          <p className="text-sm text-foreground">Failed to load templates.</p>
          <button
            type="button"
            onClick={() => void templatesQuery.refetch()}
            className="mt-1 text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 rounded"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h2 className="text-lg font-semibold text-foreground mb-2">
        Choose a blueprint template
      </h2>
      <p className="text-sm text-muted-foreground mb-6">
        Select a template to get started, or create a blank blueprint.
      </p>

      {failedSlug && (
        <div
          role="alert"
          className="mb-4 rounded-lg border border-destructive/30 bg-destructive/5 p-3"
        >
          <p className="text-sm text-foreground">
            Failed to load the selected template.
          </p>
          <button
            type="button"
            onClick={() => {
              const template = templatesQuery.data?.find(
                (item) => item.slug === failedSlug
              );
              if (template) void handleTemplateSelect(template);
            }}
            className="mt-1 rounded text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            Retry
          </button>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {/* Blank blueprint option */}
        <button
          type="button"
          onClick={() => onSelect(null)}
          disabled={selectedSlug !== null}
          className="group rounded-xl border border-dashed border-border bg-card p-5 text-left hover:border-primary/40 hover:bg-muted/30 transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <div className="flex items-center gap-2 mb-3">
            <FileText
              aria-hidden="true"
              className="h-5 w-5 text-muted-foreground group-hover:text-primary transition-colors"
            />
            <span className="font-medium text-foreground">Blank blueprint</span>
          </div>
          <p className="text-sm text-muted-foreground">
            Start from scratch with an empty blueprint.
          </p>
          <div className="mt-3 text-xs text-muted-foreground tabular-nums">
            0 steps
          </div>
        </button>

        {/* Template cards */}
        {(templatesQuery.data ?? []).map((tpl) => (
          <button
            type="button"
            key={tpl.slug}
            onClick={() => void handleTemplateSelect(tpl)}
            disabled={selectedSlug !== null}
            className="group rounded-xl border border-border bg-card p-5 text-left hover:border-primary/40 hover:shadow-md transition-all focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-wait disabled:opacity-60"
          >
            <div className="flex items-center gap-2 mb-3">
              <Sparkles
                aria-hidden="true"
                className="h-5 w-5 text-primary shrink-0"
              />
              <span className="font-medium text-foreground truncate">
                {tpl.name}
              </span>
              {selectedSlug === tpl.slug && (
                <Loader2
                  aria-hidden="true"
                  className="ml-auto h-4 w-4 animate-spin"
                />
              )}
            </div>
            {tpl.description && (
              <p className="text-sm text-muted-foreground mb-3 line-clamp-2">
                {tpl.description}
              </p>
            )}
            <div className="text-xs text-muted-foreground tabular-nums">
              {tpl.step_count} step{tpl.step_count !== 1 ? 's' : ''}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

export default TemplateSelector;
