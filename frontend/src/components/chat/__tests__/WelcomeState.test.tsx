import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('framer-motion', () => ({
  motion: {
    div: ({
      children,
      initial: _initial,
      animate: _animate,
      transition: _transition,
      ...props
    }: React.HTMLAttributes<HTMLDivElement> & {
      initial?: unknown;
      animate?: unknown;
      transition?: unknown;
    }) => <div {...props}>{children}</div>,
  },
  useReducedMotion: () => true,
}));

import { WelcomeState } from '../WelcomeState';

describe('WelcomeState starters', () => {
  it('uses prompts that name the required input instead of assuming one', () => {
    const onPromptSelect = vi.fn();
    render(<WelcomeState onPromptSelect={onPromptSelect} />);

    expect(
      screen.getByText(/document or passage I provide/i)
    ).toBeInTheDocument();
    expect(screen.getByText(/claim:/i)).toBeInTheDocument();
    expect(screen.getByText(/context:/i)).toBeInTheDocument();
    expect(screen.getByText(/findings:/i)).toBeInTheDocument();
    expect(screen.getByText(/templates are editable/i)).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole('button', { name: /compare these findings/i })
    );
    expect(onPromptSelect).toHaveBeenCalledWith('Compare these findings: ');
  });
});
