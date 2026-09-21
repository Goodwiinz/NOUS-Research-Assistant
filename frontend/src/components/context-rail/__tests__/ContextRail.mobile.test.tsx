import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/hooks', () => ({
  useCitationsForThread: () => ({ allCitations: [], relatedResults: [] }),
}));

vi.mock('../AgentActivityPanel', () => ({ AgentActivityPanel: () => null }));
vi.mock('../AllCitationsPanel', () => ({ AllCitationsPanel: () => null }));
vi.mock('../ContextPanel', () => ({ ContextPanel: () => null }));
vi.mock('../ProgressPanel', () => ({ ProgressPanel: () => null }));
vi.mock('../ProjectBindingCard', () => ({ ProjectBindingCard: () => null }));
vi.mock('../RelatedResultsPanel', () => ({ RelatedResultsPanel: () => null }));
vi.mock('../WorkingFoldersPanel', () => ({
  WorkingFoldersPanel: () => null,
}));

import { ContextRailDrawer } from '../ContextRail';

describe('ContextRailDrawer', () => {
  it('uses modal semantics and restores focus to its trigger on Escape', async () => {
    render(<ContextRailDrawer threadId="thread-1" ragEnabled />);

    const trigger = screen.getByRole('button', { name: 'Open chat context' });
    fireEvent.click(trigger);

    const dialog = await screen.findByRole('dialog', {
      name: 'Chat context',
    });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(
      screen.getByRole('button', { name: 'Close chat context' })
    ).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Chat context' })).toBeNull();
      expect(document.activeElement).toBe(trigger);
    });
  });
});
