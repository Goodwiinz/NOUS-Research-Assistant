/**
 * Unit tests for the rename dialog's IME composition safety (audit F3)
 *
 * Japanese/Chinese/Korean input: the Enter that confirms an IME candidate
 * inside the rename field must not commit a rename built from unconfirmed
 * composition text — and the committing Enter must not leak its default
 * behavior past the dialog.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';

import { ChatDialogs } from '../ChatDialogs';

const baseProps = {
  renameDialog: {
    open: true,
    threadId: 't1',
    currentTitle: 'old',
    value: 'にほんご',
  },
  setRenameDialog: vi.fn(),
  commitRename: vi.fn(),
  deleteDialog: { open: false, threadId: null },
  setDeleteDialog: vi.fn(),
  commitDeleteThread: vi.fn(),
  bulkDeleteDialog: { open: false, ids: [] },
  setBulkDeleteDialog: vi.fn(),
  commitBulkDelete: vi.fn(),
};

describe('ChatDialogs rename IME safety', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    render(<ChatDialogs {...baseProps} />);
  });

  it('gives the rename textbox an accessible name', () => {
    expect(
      screen.getByRole('textbox', { name: 'Thread name' })
    ).toBeInTheDocument();
  });

  it('does not rename on Enter while an IME composition is active', () => {
    const input = screen.getByRole('textbox') as HTMLInputElement;

    // The confirm-Enter of the composition arrives flagged isComposing.
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    expect(baseProps.commitRename).not.toHaveBeenCalled();

    // Control: once composition has ended, Enter commits the rename once.
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(baseProps.commitRename).toHaveBeenCalledTimes(1);
  });

  it('calls preventDefault on the renaming Enter', () => {
    const input = screen.getByRole('textbox');

    const event = new KeyboardEvent('keydown', {
      key: 'Enter',
      bubbles: true,
      cancelable: true,
    });
    const preventDefault = vi.spyOn(event, 'preventDefault');
    fireEvent(input, event);

    expect(preventDefault).toHaveBeenCalledTimes(1);
  });
});
