import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { PageFilterDetails } from '../PageFilterDetails';
import { parsePageFilter } from '@/types/pdf-chat';

describe('PDF filter diagnostics', () => {
  it('shows accepted physical pages without treating null optional timing as data', () => {
    const filter = parsePageFilter({
      document_id: 'doc-1',
      accepted_pages: [1, 2],
      total_pages: 8,
      short_circuited: true,
      selection_page_range: [0, 1],
      timing_ms: null,
      model: null,
    });
    expect(filter).toBeDefined();
    render(<PageFilterDetails filter={filter!} />);
    expect(
      screen.getByText('Selected PDF pages: 1, 2 of 8')
    ).toBeInTheDocument();
    expect(
      screen.getByText('Requested pages 1–2 (inclusive).')
    ).toBeInTheDocument();
    expect(screen.queryByText(/ranking time/)).not.toBeInTheDocument();
  });

  it.each([
    null,
    {},
    {
      document_id: 'doc-1',
      accepted_pages: [0],
      total_pages: 2,
      short_circuited: false,
    },
  ])('does not claim malformed metadata is a page selection', (input) => {
    expect(parsePageFilter(input)).toBeUndefined();
  });
});
