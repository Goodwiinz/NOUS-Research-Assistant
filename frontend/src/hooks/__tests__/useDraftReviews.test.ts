import { describe, expect, it } from 'vitest';
import { draftReviewQueryKey } from '../useDraftReviews';

describe('draftReviewQueryKey', () => {
  it('shares the project mutation invalidation prefix', () => {
    expect(draftReviewQueryKey('project-1')).toEqual([
      'project',
      'project-1',
      'draft-reviews',
    ]);
  });
});
