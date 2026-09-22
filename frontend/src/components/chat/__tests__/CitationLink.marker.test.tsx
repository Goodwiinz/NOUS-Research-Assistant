/**
 * The inline citation marker's four states.
 *
 * The marker is a footnote superscript now, so what is worth pinning is that
 * the numeral is set as a superscript, that it is still distinguishable when
 * it is the one being read, and that the non-navigable and unresolved states
 * do not present as clickable. A regression here is silent, and a paragraph
 * can carry ten of these.
 */

import { describe, expect, it, vi } from 'vitest';
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { CitationLink } from '../CitationLink';
import type { Citation } from '@/utils/citationParser';

function citation(overrides: Partial<Citation> = {}): Citation {
  return {
    number: 1,
    title: 'Attention Is All You Need',
    content: 'The Transformer follows this overall architecture.',
    score: 0.91,
    source: 'arxiv',
    documentId: 'doc-1',
    ...overrides,
  } as Citation;
}

const marker = (): HTMLElement =>
  screen.getByRole('button', { name: /Source 1/ });

describe('CitationLink marker', () => {
  it('sets the numeral as a superscript and marks the active one', () => {
    const { rerender, container } = render(
      <CitationLink citationNumber={1} citation={citation()} />
    );
    const sup = (): HTMLElement =>
      container.querySelector('sup') as HTMLElement;
    expect(sup()).toBeTruthy();
    expect(sup()).toHaveTextContent('1');
    // Sol-safe on light, Helios on dark: both tokenised for AA at 10px.
    expect(sup().className).toContain('text-(--nous-sol-safe)');
    expect(sup().className).not.toContain('underline');

    rerender(
      <CitationLink citationNumber={1} citation={citation()} isActive />
    );
    expect(sup().className).toContain('underline');
  });

  it('marks an external reference as non-navigable', () => {
    render(
      <CitationLink
        citationNumber={1}
        citation={citation({
          documentId: undefined,
          externalReferenceId: '2301.00001',
        })}
      />
    );
    // It does not present as clickable.
    expect(marker().className).toContain('cursor-default');
    expect(marker()).toHaveAccessibleName(/external reference/i);
  });

  it('disables the marker when no citation resolved', () => {
    render(<CitationLink citationNumber={1} />);
    expect(marker()).toBeDisabled();
    expect(marker().className).toContain('cursor-not-allowed');
  });

  it('routes a click to the citation, not the surrounding message', () => {
    const onClick = vi.fn();
    const c = citation();
    render(<CitationLink citationNumber={1} citation={c} onClick={onClick} />);

    fireEvent.click(marker());
    expect(onClick).toHaveBeenCalledWith(c);
  });
});
