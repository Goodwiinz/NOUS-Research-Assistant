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

  it('keeps incomplete coverage messaging accurate when providers returned results', () => {
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
              providers: {
                pubmed: {
                  status: 'partial',
                  returned_count: 2,
                  completion: 'partial_failure',
                },
              },
            },
          },
        }}
      />
    );

    expect(screen.getByRole('alert')).toHaveTextContent(
      /some selected databases could not be searched/i
    );
    expect(screen.getByRole('group', { name: 'Search provider coverage' })).toHaveTextContent(
      '2 / — returned'
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

  it('shows the full escaped Markdown export as text', () => {
    const markdown = `# Report\n\n${'Evidence line. '.repeat(45)}\n\n<script>window.alert('bad')</script>`;
    render(
      <StepProgress
        step={{
          stepIndex: 3,
          stepName: 'Export report',
          stepType: 'export',
          status: 'complete',
          tokenCount: 0,
          qualityMarks: [],
          output: { markdown, media_type: 'text/markdown' },
        }}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /export report/i }));
    expect(screen.getByText(/Evidence line\./).textContent).toBe(markdown);
    expect(document.querySelector('script')).toBeNull();
  });
});
