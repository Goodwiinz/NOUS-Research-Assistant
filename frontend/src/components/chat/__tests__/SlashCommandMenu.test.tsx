import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { SlashCommandMenu } from '../SlashCommandMenu';

const commands = Array.from({ length: 8 }, (_, index) => ({
  id: `command-${index}`,
  label: `/command-${index}`,
  title: `Command ${index}`,
}));

describe('SlashCommandMenu', () => {
  it('caps its height and scrolls a newly highlighted option into view', () => {
    const onHighlight = vi.fn();
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    });

    const { rerender } = render(
      <SlashCommandMenu
        open
        commands={commands}
        highlightedIndex={0}
        onHighlight={onHighlight}
        onRun={vi.fn()}
      />
    );

    const listbox = screen.getByRole('listbox');
    expect(listbox).toHaveStyle({ overflowY: 'auto' });
    vi.spyOn(listbox.parentElement!, 'getBoundingClientRect').mockReturnValue({
      top: 300,
      bottom: 520,
      left: 0,
      right: 390,
      width: 390,
      height: 220,
      x: 0,
      y: 300,
      toJSON: () => ({}),
    });
    fireEvent(window, new Event('resize'));
    expect(listbox.style.getPropertyValue('--slash-menu-max-height')).toBe(
      '292px'
    );

    scrollIntoView.mockClear();
    rerender(
      <SlashCommandMenu
        open
        commands={commands}
        highlightedIndex={5}
        onHighlight={onHighlight}
        onRun={vi.fn()}
      />
    );

    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' });
    expect(
      screen.getByRole('option', { name: /command-5 command 5/i })
    ).toHaveAttribute('aria-selected', 'true');
  });

  it('updates the highlight from pointer movement without moving focus out of the composer', () => {
    const onHighlight = vi.fn();

    render(
      <SlashCommandMenu
        open
        commands={commands.slice(0, 2)}
        highlightedIndex={0}
        onHighlight={onHighlight}
        onRun={vi.fn()}
      />
    );

    fireEvent.mouseEnter(
      screen.getByRole('option', { name: /command-1 command 1/i })
    );
    expect(onHighlight).toHaveBeenCalledWith(1);
  });
});
