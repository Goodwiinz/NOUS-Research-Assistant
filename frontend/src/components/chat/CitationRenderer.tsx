'use client';

import React, { useMemo } from 'react';
import type { Components } from 'react-markdown';
import { cn } from '@/lib/utils';
import { ChatMarkdown, MarkdownLink } from './ChatMarkdown';
import { CitationLink } from './CitationLink';
import {
  Citation,
  parseMessageWithCitations,
  getCitationByIndex,
} from '@/utils/citationParser';
import { citationKey } from './shared/CitationChips';

interface CitationRendererProps {
  content: string;
  citations?: Citation[];
  onCitationClick?: (citation: Citation) => void;
  activeCitationIndex?: number;
  className?: string;
  /** Tint the trailing text while a turn is still arriving. */
  freshTail?: boolean;
  /** Footnote numbering shared with the sources list (see `numberCitations`).
   * Inline markers still RESOLVE by the model's 1-based `[Doc N]` index; this
   * only decides the numeral shown, so two chunks of one document read as one
   * footnote instead of two. Absent means show the raw index. */
  citationNumbers?: Map<string, number>;
}

const CITATION_DESTINATION = '#nous-citation-';

type MarkdownNode = {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
};

/**
 * Replace citation markers after Markdown has been parsed. Transforming only
 * text nodes preserves the document tree: citations inside emphasis, lists,
 * headings, blockquotes and table cells remain children of those elements,
 * while code/inline-code nodes and existing links stay untouched.
 */
function createCitationPlugin(citationCount: number) {
  return function remarkCitations() {
    return (tree: MarkdownNode): void => {
      const transform = (node: MarkdownNode, insideLink = false): void => {
        if (!node.children) return;

        const children: MarkdownNode[] = [];
        for (const child of node.children) {
          const childIsLink =
            child.type === 'link' || child.type === 'linkReference';
          if (child.type !== 'text' || insideLink) {
            transform(child, insideLink || childIsLink);
            children.push(child);
            continue;
          }

          for (const segment of parseMessageWithCitations(child.value ?? '', {
            citationCount,
          })) {
            if (
              segment.type === 'citation' &&
              segment.citationIndex !== undefined
            ) {
              children.push({
                type: 'link',
                url: `${CITATION_DESTINATION}${segment.citationIndex}`,
                children: [
                  { type: 'text', value: String(segment.citationIndex) },
                ],
              });
            } else if (segment.content) {
              children.push({ type: 'text', value: segment.content });
            }
          }
        }
        node.children = children;
      };

      transform(tree);
    };
  };
}

export function CitationRenderer({
  content,
  citations = [],
  onCitationClick,
  activeCitationIndex,
  className,
  freshTail = false,
  citationNumbers,
}: CitationRendererProps): React.ReactElement {
  const citationCount = citations.length;
  const citationPlugin = useMemo(
    () => createCitationPlugin(citationCount),
    [citationCount]
  );
  const components = useMemo<Components>(
    () => ({
      a({ href, children }) {
        const match = href?.match(/^#nous-citation-(\d+)$/);
        if (!match) return <MarkdownLink href={href}>{children}</MarkdownLink>;

        const citationIndex = Number(match[1]);
        const citation = getCitationByIndex(citations, citationIndex);
        const displayNumber =
          (citation && citationNumbers?.get(citationKey(citation))) ??
          citationIndex;
        return (
          <CitationLink
            citationNumber={displayNumber}
            citation={citation}
            onClick={onCitationClick}
            isActive={activeCitationIndex === citationIndex}
          />
        );
      },
    }),
    [activeCitationIndex, citations, citationNumbers, onCitationClick]
  );

  return (
    <div className={cn('nous-prose w-full max-w-[72ch]', className)}>
      <ChatMarkdown
        content={content}
        freshTail={freshTail}
        remarkPlugins={[citationPlugin]}
        components={components}
      />
    </div>
  );
}

export default CitationRenderer;
