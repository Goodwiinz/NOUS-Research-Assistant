import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/test-utils';
import { CellCitation } from '../CellCitation';

describe('GOO-305 CellCitation', () => {
  it('shows no percentage and no confidence, and names the anchor status', async () => {
    const { user, container } = render(
      <CellCitation
        citation_snippet="n = 120"
        anchorStatus="legacy_unanchored"
      />
    );
    await user.click(
      screen.getByLabelText('Evidence: legacy, no source location')
    );
    expect(await screen.findByText('n = 120')).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/%|Confidence/);
    expect(container.textContent).not.toMatch(/%/);
  });

  it('opens the drawer instead of a popover when given onOpen', async () => {
    const onOpen = vi.fn();
    const { user } = render(
      <CellCitation
        citation_snippet={null}
        anchorStatus="verified"
        onOpen={onOpen}
      />
    );
    await user.click(screen.getByLabelText('Evidence: verified'));
    expect(onOpen).toHaveBeenCalledOnce();
  });
});
