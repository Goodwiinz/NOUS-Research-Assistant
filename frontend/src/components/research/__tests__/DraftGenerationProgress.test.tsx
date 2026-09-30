import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DraftGenerationProgress } from '../DraftGenerationProgress';
import type { GenerationStatus } from '@/services/projectService';

describe('DraftGenerationProgress', () => {
  it('renders an interrupted task as terminal, without spinner or Cancel', () => {
    const status = {
      task_id: 't1',
      status: 'interrupted',
      progress: 40,
      current_step: 'Draft generation interrupted',
      started_at: new Date().toISOString(),
    } as GenerationStatus;

    render(<DraftGenerationProgress status={status} onCancel={vi.fn()} />);

    expect(screen.getByText('Generation interrupted')).toBeInTheDocument();
    expect(screen.queryByText('Generating draft')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /cancel/i })
    ).not.toBeInTheDocument();
  });
});
