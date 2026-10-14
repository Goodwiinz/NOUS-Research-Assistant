import { describe, expect, it, vi } from 'vitest';
import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { CitationPanelBody } from '../CitationPanelBody';
import type { Citation } from '@/utils/citationParser';

function citation(overrides: Partial<Citation>): Citation {
  return {
    number: 1,
    title: 'Retrieval-Augmented Generation',
    content: 'A passage of retrieved text.',
    score: 0.5,
    source: 'arxiv',
    ...overrides,
  } as Citation;
}

/** The bar fills, in render order, as (background, opacity) pairs. */
function fillsFor(container: HTMLElement): { bg: string; opacity: number }[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>('span.origin-left')
  ).map((el) => ({
    bg: el.style.background,
    opacity: Number(el.style.opacity),
  }));
}

describe('CitationPanelBody relevance meter', () => {
  it('opens the clicked citation page even when another passage has a higher score', () => {
    const first = citation({ documentId: 'doc-1', pageNumber: 1, score: 0.5 });
    const third = citation({ documentId: 'doc-1', pageNumber: 3, score: 0.9 });
    const onOpen = vi.fn();
    render(
      <CitationPanelBody
        citations={[first, third]}
        activeCitationId="doc-1"
        activeCitationPage={1}
        onCitationClick={onOpen}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: 'Open document' }));
    expect(onOpen).toHaveBeenLastCalledWith(first);
    fireEvent.click(screen.getByRole('button', { name: 'Open page 3' }));
    expect(onOpen).toHaveBeenLastCalledWith(third);
  });
  it('tiers the bar fill so a stronger score is never fainter', () => {
    // Rendered one at a time: a group can auto-expand and repeat its top
    // score on a chunk bar, so a single render's bars are not one-per-score.
    const opacityAt = (score: number): number => {
      const { container, unmount } = render(
        <CitationPanelBody citations={[citation({ score })]} />
      );
      const fills = fillsFor(container);
      // One opaque accent throughout — the tier rides on opacity, because the
      // *-intense / *-muted tokens are low-alpha glows despite their names.
      expect(fills.every((f) => f.bg === 'var(--nous-sol)')).toBe(true);
      const { opacity } = fills[0];
      unmount();
      return opacity;
    };

    const strong = opacityAt(0.92);
    const middling = opacityAt(0.61);
    const weak = opacityAt(0.22);

    expect(strong).toBeGreaterThan(middling);
    expect(middling).toBeGreaterThan(weak);
    expect(weak).toBeGreaterThan(0);
  });

  it('renders no meter for synthetic zero-score citations', () => {
    const { container } = render(
      <CitationPanelBody
        citations={[citation({ number: 1, title: 'Preview only', score: 0 })]}
      />
    );

    expect(fillsFor(container)).toHaveLength(0);
    expect(screen.getByText('Preview only')).toBeInTheDocument();
  });

  it('bounds long source text and expands the untouched content', () => {
    const source = `${'A supplied source passage. '.repeat(20)}END OF SOURCE`;
    render(
      <CitationPanelBody
        citations={[citation({ content: source, score: 0.8 })]}
      />
    );

    const text = screen.getByTestId('citation-source-text');
    expect(text.textContent).not.toContain('END OF SOURCE');
    fireEvent.click(
      screen.getByRole('button', { name: 'Show full source text' })
    );

    expect(text).toHaveTextContent(source);
    expect(screen.getByRole('button', { name: 'Show less' })).toHaveAttribute(
      'aria-expanded',
      'true'
    );
  });

  it('renders short and empty source text honestly as inert text', () => {
    const source = '**literal** <script>alert(1)</script> [not a link]';
    const { rerender } = render(
      <CitationPanelBody citations={[citation({ content: source })]} />
    );

    const text = screen.getByTestId('citation-source-text');
    expect(text).toHaveTextContent(source);
    expect(text.querySelector('script')).toBeNull();

    rerender(<CitationPanelBody citations={[citation({ content: '' })]} />);
    expect(screen.getByRole('status')).toHaveTextContent(
      'Source text unavailable.'
    );
  });
});
