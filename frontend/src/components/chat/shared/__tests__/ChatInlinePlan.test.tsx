import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ChatInlinePlan } from '../ChatInlinePlan';
import type { PlanStep } from '@/types/agent-chat';

const plan: PlanStep[] = [
  {
    step: 1,
    description: 'Search arXiv for the paper',
    tool: 'search_arxiv',
    args_hint: {},
    depends_on: [],
  },
];

describe('ChatInlinePlan', () => {
  it('defaults collapsed for a committed (non-streaming) instance', () => {
    render(<ChatInlinePlan plan={plan} />);

    expect(screen.getByText('Execution plan · 0/1')).toBeInTheDocument();
    expect(
      document.querySelector('[data-slot="reasoning-panel"]')
    ).toBeTruthy();
    expect(
      screen.queryByText('Search arXiv for the paper')
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Execution plan · 0/1' })
    ).toHaveAttribute('aria-expanded', 'false');
  });

  it('defaults open for a streaming instance so steps stream in visibly', () => {
    render(<ChatInlinePlan plan={plan} streaming />);

    expect(screen.getByText('Thinking')).toBeInTheDocument();
    expect(screen.getByText('Search arXiv for the paper')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Thinking' })).toHaveAttribute(
      'aria-expanded',
      'true'
    );
  });

  it('opens a committed planner rationale so it is visible after completion', () => {
    render(
      <ChatInlinePlan
        plan={plan}
        reasoning="Search arXiv first, then summarize the top hit."
      />
    );
    expect(
      screen.getByRole('button', { name: 'Reasoning · Execution plan · 0/1' })
    ).toHaveAttribute('aria-expanded', 'true');
    expect(
      screen.getByText('Search arXiv first, then summarize the top hit.')
    ).toBeInTheDocument();
  });

  it('omits the reasoning paragraph when no reasoning is provided', () => {
    render(<ChatInlinePlan plan={plan} streaming />);
    expect(screen.queryByText(/search arxiv first/i)).not.toBeInTheDocument();
  });

  it('renders planner rationale when a plan has no steps', () => {
    render(
      <ChatInlinePlan
        plan={[]}
        reasoning="The request needs a direct evidence check."
        streaming
      />
    );

    expect(screen.getByText('Planner rationale')).toBeInTheDocument();
    expect(
      screen.getByText('The request needs a direct evidence check.')
    ).toBeInTheDocument();
  });

  it('toggles aria-expanded and visible content on click', () => {
    render(<ChatInlinePlan plan={plan} />);
    const button = screen.getByRole('button', {
      name: 'Execution plan · 0/1',
    });

    expect(button).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(button);
    expect(button).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Search arXiv for the paper')).toBeInTheDocument();
    fireEvent.click(button);
    expect(button).toHaveAttribute('aria-expanded', 'false');
  });
});
