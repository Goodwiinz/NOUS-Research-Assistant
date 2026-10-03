import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { Document } from '@/types';
import { DocumentMetadataEditor } from '../DocumentMetadataEditor';

const document = {
  id: 'doc-1',
  title: 'Original title',
  description: '',
  tags: [],
} as Document;

describe('DocumentMetadataEditor', () => {
  it('shows an error and stays open when saving fails', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const onSave = vi.fn().mockRejectedValue(new Error('500 internal'));
    const onClose = vi.fn();

    render(
      <DocumentMetadataEditor
        document={document}
        isOpen
        onClose={onClose}
        onSave={onSave}
      />
    );

    fireEvent.change(screen.getByPlaceholderText('Document title'), {
      target: { value: 'New title' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Failed to save metadata. Please try again.'
    );
    expect(onSave).toHaveBeenCalledWith(
      'doc-1',
      expect.objectContaining({ title: 'New title' })
    );
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes without an error when saving succeeds', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const onClose = vi.fn();

    render(
      <DocumentMetadataEditor
        document={document}
        isOpen
        onClose={onClose}
        onSave={onSave}
      />
    );

    fireEvent.change(screen.getByPlaceholderText('Document title'), {
      target: { value: 'New title' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('names an invalid date field instead of calling onSave', async () => {
    const onSave = vi.fn();

    render(
      <DocumentMetadataEditor
        document={document}
        isOpen
        onClose={vi.fn()}
        onSave={onSave}
      />
    );

    fireEvent.change(screen.getByPlaceholderText('Field name'), {
      target: { value: 'published' },
    });
    fireEvent.change(screen.getByPlaceholderText('Value'), {
      target: { value: 'not a date' },
    });
    fireEvent.change(screen.getByRole('combobox'), {
      target: { value: 'date' },
    });
    fireEvent.click(screen.getByRole('button', { name: /add custom field/i }));
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '"published" is not a valid date.'
    );
    expect(onSave).not.toHaveBeenCalled();
  });

  it('does not reopen with the previous save error', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const props = {
      document,
      onClose: vi.fn(),
      onSave: vi.fn().mockRejectedValue(new Error('500 internal')),
    };
    const { rerender } = render(<DocumentMetadataEditor {...props} isOpen />);

    fireEvent.change(screen.getByPlaceholderText('Document title'), {
      target: { value: 'New title' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }));
    await screen.findByRole('alert');

    rerender(<DocumentMetadataEditor {...props} isOpen={false} />);
    rerender(<DocumentMetadataEditor {...props} isOpen />);

    expect(screen.queryByRole('alert')).toBeNull();
  });
});
