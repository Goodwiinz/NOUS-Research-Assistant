import { describe, expect, it } from 'vitest';

import dataset from '../../../../e2e/fixtures/agent-qa.v1.json';
import { gradeAgentAnswer } from '../agentAnswerGrader';

describe('agent Q&A dataset', () => {
  it('has a stable version and unique, complete cases', () => {
    expect(dataset.version).toMatch(/^\d+\.\d+\.\d+$/);
    expect(dataset.cases).toHaveLength(30);

    const ids = dataset.cases.map((testCase) => testCase.id);
    expect(new Set(ids).size).toBe(ids.length);

    for (const testCase of dataset.cases) {
      expect(testCase.id).toMatch(/^[a-z0-9-]+$/);
      expect(testCase.category.trim()).not.toBe('');
      expect(testCase.question.trim()).not.toBe('');
      expect(testCase.referenceAnswer.trim()).not.toBe('');
    }
  });

  it('grades every reference answer as passing its rubric', () => {
    for (const testCase of dataset.cases) {
      expect(
        gradeAgentAnswer(testCase.referenceAnswer, testCase.criteria),
        `reference answer failed for ${testCase.id}`
      ).toEqual({ passed: true, failures: [] });
    }
  });
});
