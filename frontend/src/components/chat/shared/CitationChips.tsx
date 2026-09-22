'use client';

import React from 'react';
import { Activity } from 'lucide-react';

import type { Citation } from '@/utils/citationParser';

export interface CitationChipsProps {
  citations: Citation[];
  diagnosticsTraceId?: string;
  onCitationClick?: (
    citations: Citation[],
    clickedCitation: Citation,
    traceId?: string
  ) => void;
}

/**
 * Stable identity for a cited source. Two chunks of the same document are the
 * same source to a reader, so they collapse onto one row and share one numeral.
 */
export function citationKey(citation: Citation): string {
  return citation.documentId ?? citation.externalReferenceId ?? citation.title;
}

/**
 * Deduplicate citations by source, keeping first-cited order, and hand back the
 * 1-based numbering. The inline superscript markers and the sources list are
 * numbered from this same map so the two can never drift.
 */
export function numberCitations(citations: Citation[]): {
  ordered: Citation[];
  indexByKey: Map<string, number>;
} {
  const ordered: Citation[] = [];
  const indexByKey = new Map<string, number>();

  for (const citation of citations) {
    const key = citationKey(citation);
    if (indexByKey.has(key)) continue;
    ordered.push(citation);
    indexByKey.set(key, ordered.length);
  }

  return { ordered, indexByKey };
}

function locatorFor(citation: Citation): string | null {
  // Only a real page number renders. Nothing is invented from the score or the
  // chunk index — a fabricated locator is worse than none.
  return typeof citation.pageNumber === 'number'
    ? `p. ${citation.pageNumber}`
    : null;
}

/**
 * The numbered sources list under a committed assistant reply: one row per
 * distinct cited document, in citation order, carrying the numeral that the
 * inline marker uses, the title, the page locator when known, and the quoted
 * passage. Relevance percentages live in the retrieval diagnostics view.
 *
 * Shared by the legacy ChatBubble and the assistant-ui message renderer.
 */
export function CitationChips({
  citations,
  diagnosticsTraceId,
  onCitationClick,
}: CitationChipsProps): React.JSX.Element | null {
  if (citations.length === 0) return null;

  const { ordered } = numberCitations(citations);

  return (
    <section aria-label="Sources" className="mt-5 max-w-[720px]">
      <div className="mb-1 text-[11px] font-semibold text-(--nous-fg-3)">
        Sources
      </div>
      <ol className="m-0 list-none p-0">
        {ordered.map((citation, index) => {
          const locator = locatorFor(citation);
          const title = citation.title || 'Untitled source';
          return (
            <li key={citationKey(citation)}>
              <button
                type="button"
                onClick={() =>
                  onCitationClick?.(citations, citation, diagnosticsTraceId)
                }
                className="grid w-full grid-cols-[22px_1fr] items-baseline gap-3 border-t border-(--nous-border-1) py-2.5 text-left transition-colors hover:bg-(--nous-bg-2) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
              >
                <span className="text-[11px] font-semibold text-(--nous-sol-safe) dark:text-(--nous-helios)">
                  {index + 1}
                </span>
                <span>
                  <span className="block text-[13px] font-medium text-(--nous-fg-1)">
                    {locator ? `${title} · ${locator}` : title}
                  </span>
                  {citation.content && (
                    <span
                      className="mt-0.5 line-clamp-2 block text-[14px] leading-[1.5] text-(--nous-fg-2)"
                      style={{ fontFamily: 'var(--nous-font-body)' }}
                    >
                      {`“…${citation.content}…”`}
                    </span>
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      <div className="border-t border-(--nous-border-1)" />
      {diagnosticsTraceId && (
        <div className="flex justify-end">
          <a
            href={`/diagnostics?trace=${encodeURIComponent(diagnosticsTraceId)}`}
            className="mt-1.5 flex items-center gap-1 text-[11px] text-(--nous-fg-3) transition-colors hover:text-(--nous-sol-safe) dark:hover:text-(--nous-helios)"
            title="View retrieval diagnostics"
          >
            <Activity className="h-3 w-3" />
            <span>Retrieval diagnostics</span>
          </a>
        </div>
      )}
    </section>
  );
}
