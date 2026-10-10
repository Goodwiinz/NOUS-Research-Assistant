import { describe, expect, it } from 'vitest';

import dataset from '../../../../e2e/fixtures/agent-qa.v1.json';
import { gradeAgentAnswer } from '../agentAnswerGrader';

const citationCase = dataset.cases.find(
  (entry) => entry.id === 'incomplete-citation'
);
if (!citationCase) throw new Error('Missing incomplete-citation case');
const criteria = citationCase.criteria;
const advice =
  'Do not assign an author, journal, DOI, URL, or full title unless you can verify it. Mark this as an incomplete reference and recover missing metadata from the original source.';

describe('incomplete citation integrity', () => {
  it.each([
    advice,
    `${advice} Publication status is unknown; do not label it as an unpublished note.`,
    `${advice} Do not use “Deep learning survey. (2021). [Unpublished note]” without evidence.`,
  ])(
    'accepts correct caution without requiring the verb fabricate: %s',
    (answer) => {
      expect(gradeAgentAnswer(answer, criteria)).toEqual({
        passed: true,
        failures: [],
      });
    }
  );

  it.each([
    'Reference list: Deep learning survey. (2021). [Unpublished note; author and source details unavailable].',
    'Deep learning survey. (2021). Unpublished manuscript/note. Author and publication details unavailable.',
    'Citation: Deep learning survey (2021). Unpublished manuscript.',
    '[Unpublished note; author unknown]',
    '- Reference list: Deep learning survey. (2021). [Unpublished note; author unknown].',
    '1. Deep learning survey. (2021). Unpublished manuscript.',
    '> [Unpublished note; author unknown]',
  ])(
    'rejects unsupported publication status in a proposed entry: %s',
    (entry) => {
      const result = gradeAgentAnswer(`${entry}\n\n${advice}`, criteria);
      expect(result.passed).toBe(false);
      expect(result.failures).toEqual([
        'unverified publication status in the proposed citation',
      ]);
    }
  );
});
