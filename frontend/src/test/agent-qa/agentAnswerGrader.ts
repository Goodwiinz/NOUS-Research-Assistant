export interface AgentAnswerCriteria {
  requiredConcepts: string[][];
  /** Optional regex alternatives, indexed by required concept, against normalized answer text. */
  requiredConceptPatterns?: string[][];
  forbiddenPhrases?: string[];
  /** Case-specific patterns for claims that depend on punctuation or position. */
  forbiddenPatterns?: { pattern: string; reason: string }[];
  minimumCharacters?: number;
  maximumCharacters?: number;
  exactAnswer?: string;
}

export interface AgentAnswerGrade {
  passed: boolean;
  failures: string[];
}

function normalize(value: string): string {
  return value
    .normalize('NFKD')
    .toLocaleLowerCase('en-US')
    .replace(/%/g, ' percent ')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

export function gradeAgentAnswer(
  answer: string,
  criteria: AgentAnswerCriteria
): AgentAnswerGrade {
  const normalizedAnswer = normalize(answer);
  const searchableAnswer = ` ${normalizedAnswer} `;
  const failures: string[] = [];

  criteria.requiredConcepts.forEach((alternatives, index) => {
    if (
      alternatives.length === 0 ||
      alternatives.some((phrase) => !phrase.trim())
    ) {
      throw new Error(
        `required concept ${index + 1} must contain at least one non-empty phrase`
      );
    }
    const patterns = criteria.requiredConceptPatterns?.[index] ?? [];
    const matched =
      alternatives.some((phrase) =>
        searchableAnswer.includes(` ${normalize(phrase)} `)
      ) ||
      patterns.some((pattern) =>
        new RegExp(pattern, 'iu').test(normalizedAnswer)
      );
    if (!matched) {
      failures.push(
        `missing concept ${index + 1} (${[
          ...alternatives,
          ...patterns.map((pattern) => `/${pattern}/`),
        ].join(' | ')})`
      );
    }
  });

  for (const phrase of criteria.forbiddenPhrases ?? []) {
    if (searchableAnswer.includes(` ${normalize(phrase)} `)) {
      failures.push(`contains forbidden phrase "${phrase}"`);
    }
  }

  for (const { pattern, reason } of criteria.forbiddenPatterns ?? []) {
    if (new RegExp(pattern, 'imu').test(answer.normalize('NFKC'))) {
      failures.push(reason);
    }
  }

  if (!normalizedAnswer) {
    failures.push('answer is empty');
  }

  if (criteria.exactAnswer !== undefined && answer !== criteria.exactAnswer) {
    failures.push(`answer does not exactly match "${criteria.exactAnswer}"`);
  }

  if (
    criteria.minimumCharacters !== undefined &&
    answer.length < criteria.minimumCharacters
  ) {
    failures.push(
      `answer has ${answer.length} characters; expected at least ${criteria.minimumCharacters}`
    );
  }

  if (
    criteria.maximumCharacters !== undefined &&
    answer.length > criteria.maximumCharacters
  ) {
    failures.push(
      `answer has ${answer.length} characters; expected at most ${criteria.maximumCharacters}`
    );
  }

  return { passed: failures.length === 0, failures };
}
