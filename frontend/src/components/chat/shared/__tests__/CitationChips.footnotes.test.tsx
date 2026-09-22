/**
 * The sources list under a committed assistant reply.
 *
 * The reading design makes provenance the visible structure of the answer:
 * one numbered row per distinct cited document, in first-cited order, with the
 * quoted passage. Numbering has to survive deduplication because the inline
 * superscript markers are numbered from the same map.
 */

import { describe, expect, it, vi } from 'vitest';
import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';

import { CitationChips, numberCitations } from '../CitationChips';
import type { Citation } from '@/utils/citationParser';

const citations: Citation[] = [
  {
    documentId: 'doc-1',
    title: 'Attention Is All You Need',
    score: 0.91,
    content: 'The Transformer follows this overall architecture.',
    pageNumber: 7,
  },
  {
    documentId: 'doc-2',
    title: 'Retrieval-Augmented Generation',
    score: 0.62,
    content: 'We combine a parametric and a non-parametric memory.',
  },
  {
    // Same document as the first citation, a different chunk: one row.
    documentId: 'doc-1',
    title: 'Attention Is All You Need',
    score: 0.55,
    content: 'Self-attention layers connect all positions.',
  },
];

describe('CitationChips sources list', () => {
  it('renders one row per distinct document in first-cited order', () => {
    render(<CitationChips citations={citations} />);

    const list = screen.getByRole('list');
    const rows = within(list).getAllByRole('listitem');
    expect(rows).toHaveLength(2);

    expect(within(rows[0]).getByText('1')).toBeInTheDocument();
    expect(
      within(rows[0]).getByText(/Attention Is All You Need/)
    ).toBeInTheDocument();
    expect(within(rows[1]).getByText('2')).toBeInTheDocument();
    expect(
      within(rows[1]).getByText(/Retrieval-Augmented Generation/)
    ).toBeInTheDocument();
  });

  it('shows the page locator and the quoted snippet, and no relevance percentage', () => {
    const { container } = render(<CitationChips citations={citations} />);

    expect(
      screen.getByText(/Attention Is All You Need · p\. 7/)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/The Transformer follows this overall architecture\./)
    ).toBeInTheDocument();
    // The quoted passage is quoted.
    expect(container.textContent).toContain('“');
    // Relevance lives in the diagnostics view, not in the reading view.
    expect(container.textContent).not.toContain('%');
  });

  it('routes a row click to the clicked citation', () => {
    const onCitationClick = vi.fn();
    render(
      <CitationChips
        citations={citations}
        diagnosticsTraceId="trace-1"
        onCitationClick={onCitationClick}
      />
    );

    fireEvent.click(
      screen.getByRole('button', { name: /Retrieval-Augmented Generation/ })
    );

    expect(onCitationClick).toHaveBeenCalledWith(
      citations,
      citations[1],
      'trace-1'
    );
  });

  it('numbers citations by deduplicated first-cited order', () => {
    const { ordered, indexByKey } = numberCitations(citations);

    expect(ordered).toHaveLength(2);
    expect(indexByKey.get('doc-1')).toBe(1);
    expect(indexByKey.get('doc-2')).toBe(2);
  });
});
