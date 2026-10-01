import { describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { render, screen } from '@/test/test-utils';
import { DraftExportModal } from '../DraftExportModal';

function renderModal(): ReturnType<typeof vi.fn> {
  const onExport = vi.fn().mockResolvedValue(undefined);
  render(
    <DraftExportModal
      isOpen
      onClose={vi.fn()}
      onExport={onExport}
      draftTitle="Results"
    />
  );
  return onExport;
}

describe('DraftExportModal (GOO-317)', () => {
  it('default export format still markdown', async () => {
    const onExport = renderModal();
    await userEvent.click(screen.getByRole('button', { name: 'Export' }));
    expect(onExport).toHaveBeenCalledWith('markdown', true, 'bibtex');
  });

  it('csl and ris options call the right format', async () => {
    const onExport = renderModal();
    await userEvent.click(screen.getByRole('button', { name: /CSL JSON/ }));
    expect(
      screen.queryByRole('switch', { name: 'Include bibliography' })
    ).not.toBeInTheDocument();
    expect(screen.getByText(/never filled in/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Export' }));
    expect(onExport).toHaveBeenLastCalledWith('csl-json', true, 'bibtex');
    await userEvent.click(screen.getByRole('button', { name: /RIS/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Export' }));
    expect(onExport).toHaveBeenLastCalledWith('ris', true, 'bibtex');
  });
});

describe('DraftExportModal reopen', () => {
  it('resets to the initial format each time it opens', async () => {
    const props = {
      onClose: vi.fn(),
      onExport: vi.fn().mockResolvedValue(undefined),
      draftTitle: 'Results',
      initialFormat: 'latex' as const,
    };
    const { rerender } = render(<DraftExportModal isOpen {...props} />);
    await userEvent.click(screen.getByRole('button', { name: /RIS/ }));
    rerender(<DraftExportModal isOpen={false} {...props} />);
    rerender(<DraftExportModal isOpen {...props} />);
    await userEvent.click(screen.getByRole('button', { name: 'Export' }));
    expect(props.onExport).toHaveBeenLastCalledWith('latex', true, 'bibtex');
  });
});
