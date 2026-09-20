import { describe, expect, it } from 'vitest';

import dataset from '../../../../e2e/fixtures/agent-qa.v1.json';
import { gradeAgentAnswer } from '../agentAnswerGrader';

// Reviewed responses from the first live run on 2026-09-20. These are
// regression examples for rubric false negatives, not new live evaluations.
const paraphrases = [
  {
    id: 'doi-purpose',
    answer:
      'A DOI (Digital Object Identifier) is a unique, persistent alphanumeric code assigned to a scholarly work, such as a journal article, book chapter, or dataset. Researchers can use it to create accurate citations and reliably locate the work online, even if the publisher’s web address changes.',
    incorrect:
      'A DOI is a temporary web address used to locate scholarly papers and create citations. It becomes unusable when the publisher changes the address.',
  },
  {
    id: 'knowledge-graph-basics',
    answer:
      'In a research knowledge graph:\n\nEntities are identifiable items, such as people, papers, organizations, methods, datasets, or concepts.\nRelationships are labeled connections between those entities, representing a documented association in the graph.\n\nExample:\n\nBERT --AUTHORED_BY--> Jacob Devlin\n\nHere, BERT is an entity, Jacob Devlin is another entity, and AUTHORED_BY is the relationship connecting them.',
    incorrect:
      'Entities include papers and authors. Papers describe experiments, and authors are people who write those papers. That is all a knowledge graph contains.',
  },
  {
    id: 'missing-results',
    answer:
      'The passage does not state which process worked better. It only describes the randomization and temperature recording.',
    incorrect:
      'The method randomized 40 samples and recorded temperature every minute. The first process worked better because its temperatures were lower.',
  },
  {
    id: 'sample-percentage',
    answer: '20 ÷ 80 × 100 = 25%.',
    incorrect: '20 ÷ 80 × 100 = 20%.',
  },
];

describe('reviewed live Q&A paraphrases', () => {
  it.each(paraphrases)('accepts the correct $id answer', ({ id, answer }) => {
    const testCase = dataset.cases.find((entry) => entry.id === id);
    if (!testCase) throw new Error(`Missing dataset case: ${id}`);
    expect(gradeAgentAnswer(answer, testCase.criteria)).toEqual({
      passed: true,
      failures: [],
    });
  });

  it.each(paraphrases)(
    'still rejects an incomplete or incorrect $id answer',
    ({ id, incorrect }) => {
      const testCase = dataset.cases.find((entry) => entry.id === id);
      if (!testCase) throw new Error(`Missing dataset case: ${id}`);
      expect(gradeAgentAnswer(incorrect, testCase.criteria).passed).toBe(false);
    }
  );
});
