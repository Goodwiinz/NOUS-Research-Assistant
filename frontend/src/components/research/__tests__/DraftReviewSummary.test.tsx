import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DraftReviewSummary } from '../DraftReviewSummary';

describe('DraftReviewSummary', () => {
  it('renders blocked, minor, unknown, and uncited findings', () => {
    render(
      <DraftReviewSummary
        review={{
          id: 'r',
          project_id: 'p',
          outcome: 'blocked',
          candidate_content: 'candidate',
          created_at: '',
          review: {
            summary: { minor: 1, major: 1, unverified: 1 },
            verdicts: [{ verdict: 'minor', evidence: 'Effect was smaller.' }],
            uncited_assertions: [{ text: 'Mortality doubled.' }],
          },
        }}
      />
    );
    expect(screen.getByRole('status')).toHaveTextContent('blocked');
    expect(screen.getByRole('status')).toHaveTextContent('1 unknown');
    expect(screen.getByText(/uncited: Mortality doubled/)).toBeInTheDocument();
  });
});
