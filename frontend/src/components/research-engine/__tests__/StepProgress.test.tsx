import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { StepProgress, type StepData } from '../StepProgress';

describe('StepProgress', () => {
  it('shows partial search coverage before expanding the result', () => {
    render(
      <StepProgress
        step={{
          stepIndex: 0,
          stepName: 'Find papers',
          stepType: 'search',
          status: 'complete',
          tokenCount: 0,
          qualityMarks: [],
          output: {
            coverage: {
              partial: true,
              providers: { pubmed: { status: 'failed' } },
            },
          },
        }}
      />
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      /some selected databases could not be searched/i
    );
  });

  it('renders structured output objects without crashing', () => {
    const step: StepData = {
      stepIndex: 0,
      stepName: 'Extract Evidence',
      stepType: 'extract',
      status: 'complete',
      tokenCount: 50,
      qualityMarks: [],
      output: {
        content: 'Structured summary',
        sources: ['paper-a', 'paper-b'],
      },
    };

    render(<StepProgress step={step} />);

    fireEvent.click(screen.getByRole('button', { name: /extract evidence/i }));
    expect(screen.getByText(/structured summary/i)).toBeInTheDocument();
  });
});
