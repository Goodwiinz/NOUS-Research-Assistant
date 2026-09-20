import { describe, expect, it } from 'vitest';

import { gradeAgentAnswer } from '../agentAnswerGrader';

describe('gradeAgentAnswer', () => {
  it('accepts synonyms from every required concept group', () => {
    const result = gradeAgentAnswer(
      'RAG retrieves relevant context before the model generates a grounded response.',
      {
        requiredConcepts: [
          ['retrieve', 'retrieves', 'retrieval'],
          ['generate', 'generates', 'generation'],
          ['grounded', 'source-backed'],
        ],
        forbiddenPhrases: ['i cannot answer'],
        minimumCharacters: 40,
      }
    );

    expect(result).toEqual({ passed: true, failures: [] });
  });

  it('reports missing concepts, forbidden phrases, and short answers together', () => {
    const result = gradeAgentAnswer('I cannot answer.', {
      requiredConcepts: [
        ['retrieve', 'retrieval'],
        ['generate', 'generation'],
      ],
      forbiddenPhrases: ['cannot answer'],
      minimumCharacters: 50,
    });

    expect(result).toEqual({
      passed: false,
      failures: [
        'missing concept 1 (retrieve | retrieval)',
        'missing concept 2 (generate | generation)',
        'contains forbidden phrase "cannot answer"',
        'answer has 16 characters; expected at least 50',
      ],
    });
  });

  it('matches concepts without case or punctuation sensitivity', () => {
    const result = gradeAgentAnswer(
      'A KNOWLEDGE-GRAPH stores entities and relations.',
      {
        requiredConcepts: [
          ['knowledge graph'],
          ['entity', 'entities'],
          ['relation', 'relations', 'relationship'],
        ],
      }
    );

    expect(result).toEqual({ passed: true, failures: [] });
  });

  it('does not accept a concept embedded inside another word', () => {
    const result = gradeAgentAnswer('The system regenerates its output.', {
      requiredConcepts: [['generate']],
    });

    expect(result).toEqual({
      passed: false,
      failures: ['missing concept 1 (generate)'],
    });
  });

  it('supports exact answers and maximum length', () => {
    expect(
      gradeAgentAnswer('blue sky', {
        requiredConcepts: [],
        exactAnswer: 'blue sky',
        maximumCharacters: 20,
      })
    ).toEqual({ passed: true, failures: [] });

    expect(
      gradeAgentAnswer('blue sky with extra words', {
        requiredConcepts: [],
        exactAnswer: 'blue sky',
        maximumCharacters: 20,
      })
    ).toEqual({
      passed: false,
      failures: [
        'answer does not exactly match "blue sky"',
        'answer has 25 characters; expected at most 20',
      ],
    });
  });

  it.each(['BLUE-SKY!', 'blue sky', 'BLUE SKY.', 'BLUE SKY plus extra words'])(
    'rejects an instruction-following violation: %s',
    (answer) => {
      expect(
        gradeAgentAnswer(answer, {
          requiredConcepts: [],
          exactAnswer: 'BLUE SKY',
        }).passed
      ).toBe(false);
    }
  );

  it('rejects empty answers and malformed concept groups', () => {
    expect(() =>
      gradeAgentAnswer('answer', { requiredConcepts: [[]] })
    ).toThrow('required concept 1 must contain at least one non-empty phrase');

    expect(
      gradeAgentAnswer('   ', {
        requiredConcepts: [],
      })
    ).toEqual({ passed: false, failures: ['answer is empty'] });
  });
});
