import { describe, expect, it } from 'vitest';

import baseline from '../../../../e2e/fixtures/agent-qa.v1.json';
import dataset from '../../../../e2e/fixtures/agent-qa.multi-turn.v1.json';
import { interleaveAgentQaCases } from '../../../../e2e/agent-qa/multiTurnSequence';
import { gradeAgentAnswer } from '../agentAnswerGrader';

describe('multi-turn agent Q&A follow-ups', () => {
  it('grades the expected answers and rejects plausible context mix-ups', () => {
    const wrongAnswers: Record<string, string> = {
      'recall-search-paper-author': 'Alex Rivera.',
      'recall-summary-paper-year': 'Year: 2024.',
      'recall-project-brief': 'Compare citation styles used by students.',
      'correct-search-paper-author': 'Maya Chen.',
      'recall-corrected-record': 'Maya Chen, 2023.',
    };

    expect(dataset.version).toMatch(/^\d+\.\d+\.\d+$/);
    expect(dataset.cases).toHaveLength(5);
    expect(new Set(dataset.cases.map((testCase) => testCase.id)).size).toBe(5);
    for (const testCase of dataset.cases) {
      expect(testCase.question.trim()).not.toBe('');
      expect(testCase.referenceAnswer.trim()).not.toBe('');
      expect(
        gradeAgentAnswer(testCase.referenceAnswer, testCase.criteria),
        `reference answer failed for ${testCase.id}`
      ).toEqual({ passed: true, failures: [] });
      expect(
        gradeAgentAnswer(wrongAnswers[testCase.id], testCase.criteria).passed,
        `context mix-up passed for ${testCase.id}`
      ).toBe(false);
    }
  });

  it('requires the explicit Year label in the summary-paper recall answer', () => {
    const testCase = dataset.cases.find(
      (entry) => entry.id === 'recall-summary-paper-year'
    );
    if (!testCase) throw new Error('Missing summary-paper recall case');
    expect(gradeAgentAnswer('2022.', testCase.criteria).passed).toBe(false);
  });

  it('accepts the observed concise project-brief recall while rejecting its negation', () => {
    const testCase = dataset.cases.find(
      (entry) => entry.id === 'recall-project-brief'
    );
    if (!testCase) throw new Error('Missing project-brief recall case');
    expect(
      gradeAgentAnswer('Search tools used by librarians.', testCase.criteria)
        .passed
    ).toBe(true);
    expect(
      gradeAgentAnswer(
        'Do not compare search tools used by librarians.',
        testCase.criteria
      ).passed
    ).toBe(false);
    expect(
      gradeAgentAnswer(
        'The brief asked you to evaluate search tools used by librarians.',
        testCase.criteria
      ).passed
    ).toBe(false);
    expect(
      gradeAgentAnswer(
        'Compare search tools used by librarians and recommend one.',
        testCase.criteria
      ).passed
    ).toBe(false);
  });

  it.each([
    "Don't compare search tools used by librarians.",
    'The brief did not ask you to compare search tools used by librarians.',
    'The brief asked you not to compare search tools used by librarians.',
  ])('rejects a negated comparison in the brief recall: %s', (answer) => {
    const testCase = dataset.cases.find(
      (entry) => entry.id === 'recall-project-brief'
    );
    if (!testCase) throw new Error('Missing project-brief recall case');
    expect(gradeAgentAnswer(answer, testCase.criteria).passed).toBe(false);
  });

  it('asks each contextual follow-up while its source is still in recent history', () => {
    const sequence = interleaveAgentQaCases(baseline.cases, dataset.cases);
    const ids = sequence.map(({ testCase }) => testCase.id);

    expect(sequence).toHaveLength(35);
    expect(new Set(ids).size).toBe(35);
    expect(sequence.filter(({ source }) => source === 'baseline')).toHaveLength(
      30
    );
    expect(
      sequence.filter(({ source }) => source === 'follow-up')
    ).toHaveLength(5);
    for (const followUp of dataset.cases) {
      const sourceTurn = ids.indexOf(followUp.afterCaseId);
      const followUpTurn = ids.indexOf(followUp.id);
      expect(sourceTurn, `missing source for ${followUp.id}`).toBeGreaterThan(
        -1
      );
      expect(followUpTurn, `missing ${followUp.id}`).toBeGreaterThan(
        sourceTurn
      );
      expect(followUpTurn - sourceTurn).toBeLessThan(20);
    }
    expect(ids.indexOf('correct-search-paper-author')).toBeLessThan(
      ids.indexOf('recall-corrected-record')
    );
    expect(
      ids.indexOf('recall-corrected-record') -
        ids.indexOf('extract-paper-author')
    ).toBeLessThan(20);
  });

  it('rejects a follow-up whose source does not occur in the baseline', () => {
    expect(() =>
      interleaveAgentQaCases(
        [{ id: 'first' }],
        [{ id: 'follow-up', afterCaseId: 'missing' }]
      )
    ).toThrow(/missing/);
  });
});
