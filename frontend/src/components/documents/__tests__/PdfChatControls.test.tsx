import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PdfChatControls } from '../PdfChatControls';

describe('PDF page controls', () => {
  // Mutation-verified: passing textSelection.range as onAsk's first argument
  // fails this test; quote origin must never become an implicit restriction.
  // pnpm --dir frontend test src/components/documents/__tests__/PdfChatControls.test.tsx -t 'keeps quote provenance'
  it('keeps quote provenance separate from an optional explicit page restriction', () => {
    const onAsk = vi.fn();
    const props = {
      onAsk,
      textSelection: {
        range: [1, 2] as [number, number],
        quote: '> chosen text',
      },
    };
    const { rerender } = render(<PdfChatControls {...props} />);
    expect(screen.getByLabelText('Selected PDF passage')).toHaveTextContent(
      'chosen text'
    );
    fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    expect(onAsk).toHaveBeenLastCalledWith(undefined, '> chosen text', [1, 2]);
    expect(screen.getByLabelText('First page')).toHaveValue(null);
    expect(screen.getByLabelText('Last page')).toHaveValue(null);
    fireEvent.change(screen.getByLabelText('First page'), {
      target: { value: '1' },
    });
    fireEvent.change(screen.getByLabelText('Last page'), {
      target: { value: '4' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    expect(onAsk).toHaveBeenLastCalledWith([0, 3], '> chosen text', [1, 2]);
    rerender(
      <PdfChatControls {...props} textSelection={{ ...props.textSelection }} />
    );
    fireEvent.click(screen.getByRole('button', { name: 'Clear page range' }));
    fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    expect(onAsk).toHaveBeenLastCalledWith(undefined, '> chosen text', [1, 2]);
    fireEvent.click(screen.getByRole('button', { name: 'Clear quote' }));
    fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    expect(onAsk).toHaveBeenLastCalledWith(undefined);
    expect(screen.queryByLabelText('Selected PDF passage')).toBeNull();
  });
  it('sends inclusive first-page selection as zero-based indices, and supports the whole PDF', () => {
    const onAsk = vi.fn();
    render(<PdfChatControls onAsk={onAsk} />);
    fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    expect(onAsk).toHaveBeenLastCalledWith(undefined);
    fireEvent.change(screen.getByLabelText('First page'), {
      target: { value: '1' },
    });
    fireEvent.change(screen.getByLabelText('Last page'), {
      target: { value: '1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    expect(onAsk).toHaveBeenLastCalledWith([0, 0]);
    expect(screen.getByText(/without restricting pages/)).toBeInTheDocument();
  });

  it.each([
    ['0', '1'],
    ['2', '1'],
    ['1', ''],
    ['1', '101'],
    ['1.5', '2'],
  ])(
    'blocks an invalid range %s–%s with an accessible explanation',
    (first, last) => {
      render(<PdfChatControls onAsk={vi.fn()} />);
      fireEvent.change(screen.getByLabelText('First page'), {
        target: { value: first },
      });
      fireEvent.change(screen.getByLabelText('Last page'), {
        target: { value: last },
      });
      expect(
        screen.getByRole('button', { name: 'Ask about PDF' })
      ).toBeDisabled();
      expect(screen.getByRole('status')).toHaveTextContent(/page/i);
    }
  );
});
