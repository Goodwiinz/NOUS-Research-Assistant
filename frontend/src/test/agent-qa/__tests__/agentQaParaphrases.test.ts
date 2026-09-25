import { describe, expect, it } from 'vitest';

import dataset from '../../../../e2e/fixtures/agent-qa.v1.json';
import { gradeAgentAnswer } from '../agentAnswerGrader';
import type { AgentAnswerCriteria } from '../agentAnswerGrader';

// Authored offline examples, not observations from a live agent run.
const examples = [
  {
    id: 'paper-summary',
    answer: 'The 20 students found papers faster using the search tool.',
    incorrect: 'The 30 students found papers faster using the search tool.',
  },
  {
    id: 'main-finding',
    answer: 'Semantic search returned more relevant papers.',
    incorrect: 'Keyword search returned more relevant papers.',
  },
  {
    id: 'study-method',
    answer: 'They conducted interviews with twelve librarians.',
    incorrect: 'They surveyed twelve librarians.',
  },
  {
    id: 'study-limitation',
    answer: 'Only one university was involved.',
    incorrect: 'The study covered multiple universities and a large sample.',
  },
  {
    id: 'supporting-source',
    answer: 'Note A.',
    incorrect: 'Note B says search found more papers.',
  },
  {
    id: 'compare-paper-notes',
    answer: 'Paper A saved reading time; Paper B did not.',
    incorrect: 'Paper A and Paper B both saved reading time.',
  },
  {
    id: 'missing-results',
    answer: 'Cannot tell: no results are included.',
    incorrect: 'The first method worked better.',
  },
  {
    id: 'incomplete-citation',
    answer:
      'Mark the citation incomplete and find the original source; do not invent authors or a DOI.',
    incorrect: 'Smith et al., Nature 2021, is the complete citation.',
  },
  {
    id: 'draft-research-note',
    answer:
      'The tool found more relevant papers, but the small sample limits the finding.',
    incorrect: 'The tool found more relevant papers with ten students.',
  },
  {
    id: 'next-research-step',
    answer: 'Test the search tool with a larger group.',
    incorrect: 'The tool was already tested with a larger group.',
  },
  {
    id: 'extract-paper-author',
    answer: 'The author is Maya Chen.',
    incorrect: 'The author is Alex Rivera.',
  },
  {
    id: 'extract-paper-year',
    answer: 'The listed year is 2022.',
    incorrect: 'The listed year is 2024.',
  },
  {
    id: 'extract-study-sample',
    answer: 'Eighteen students participated.',
    incorrect: 'Eight students participated.',
  },
  {
    id: 'extract-study-duration',
    answer: 'The study lasted 6 weeks.',
    incorrect: 'The study lasted six months.',
  },
  {
    id: 'select-search-source',
    answer: 'Read Paper Search for research on finding papers.',
    incorrect: 'Read Reading Notes for research on finding papers.',
  },
  {
    id: 'select-citation-source',
    answer: 'Citation Checks is the relevant paper.',
    incorrect: 'Fast Reading is the relevant paper.',
  },
  {
    id: 'missing-study-location',
    answer: 'Their university is not stated.',
    incorrect: 'They were from Northfield University.',
  },
  {
    id: 'untested-user-group',
    answer: 'We need a study involving students.',
    incorrect: 'We need another study involving only librarians.',
  },
  {
    id: 'missing-comparison-group',
    answer: 'Results from people using the previous tool are missing.',
    incorrect: 'A comparison between morning and evening use is missing.',
  },
  {
    id: 'extract-search-keywords',
    answer: 'Try librarian and summary.',
    incorrect: 'Try students and textbooks.',
  },
  {
    id: 'narrow-search-topic',
    answer: 'Add librarian to your search.',
    incorrect: 'Add students to your search.',
  },
  {
    id: 'citation-title-year',
    answer: '2020 — Useful Summaries.',
    incorrect: 'Useful Summaries (2023).',
  },
  {
    id: 'citation-author-year',
    answer: 'The record lists Alex Rivera (2024).',
    incorrect: 'The record lists Maya Chen (2024).',
  },
  {
    id: 'count-supporting-studies',
    answer: '2 papers report faster reading.',
    incorrect: 'All three papers report faster reading.',
  },
  {
    id: 'total-study-participants',
    answer: 'Twenty-five students participated across the studies.',
    incorrect: 'Twenty students participated across the studies.',
  },
  {
    id: 'compare-reading-time',
    answer: 'They saved ten minutes.',
    incorrect: 'They saved twenty minutes.',
  },
  {
    id: 'choose-note-section',
    answer: 'Put it in the Limitations section.',
    incorrect: 'Put it in the Finding section.',
  },
  {
    id: 'label-reading-status',
    answer: 'Label Paper A as To read.',
    incorrect: 'Label Paper A as Read.',
  },
  {
    id: 'draft-project-description',
    answer: 'A comparison of search tools for librarians.',
    incorrect: 'A comparison of reading tools for students.',
  },
  {
    id: 'draft-follow-up-task',
    answer: 'Review the full text.',
    incorrect: 'Write the final conclusion using only the abstract.',
  },
];

describe('authored offline research-note examples', () => {
  it('covers every dataset case exactly once', () => {
    const exampleIds = examples.map(({ id }) => id);
    const datasetIds = dataset.cases.map(({ id }) => id);
    expect(new Set(exampleIds).size).toBe(exampleIds.length);
    expect(exampleIds.sort()).toEqual(datasetIds.sort());
  });
  it.each([
    'The note does not specify which university the students attended.',
    'The university is not mentioned.',
    'The note doesn’t specify the university.',
  ])('accepts an unspecified university: %s', (answer) => {
    const testCase = dataset.cases.find(
      (entry) => entry.id === 'missing-study-location'
    );
    if (!testCase) throw new Error('Missing missing-study-location case');
    expect(gradeAgentAnswer(answer, testCase.criteria).passed).toBe(true);
  });
  it('accepts either stated study limitation independently', () => {
    const testCase = dataset.cases.find(
      (entry) => entry.id === 'study-limitation'
    );
    if (!testCase) throw new Error('Missing study-limitation case');
    expect(
      gradeAgentAnswer('The sample was small.', testCase.criteria).passed
    ).toBe(true);
    expect(
      gradeAgentAnswer('Only one university was involved.', testCase.criteria)
        .passed
    ).toBe(true);
  });

  it.each(examples)('accepts the correct $id answer', ({ id, answer }) => {
    const testCase = dataset.cases.find((entry) => entry.id === id);
    if (!testCase) throw new Error(`Missing dataset case: ${id}`);
    expect(gradeAgentAnswer(answer, testCase.criteria)).toEqual({
      passed: true,
      failures: [],
    });
  });

  it.each(examples)('rejects the incorrect $id answer', ({ id, incorrect }) => {
    const testCase = dataset.cases.find((entry) => entry.id === id);
    if (!testCase) throw new Error(`Missing dataset case: ${id}`);
    expect(gradeAgentAnswer(incorrect, testCase.criteria).passed).toBe(false);
  });
});

// Captured complete answers from the 2026-09-20 live run of dataset 1.2.0.
// These are observed regressions, distinct from the authored offline examples above.
const capturedAnswers = [
  {
    id: 'missing-results',
    answer:
      'The note does not include results, so it is impossible to determine which search method worked better.',
  },
  {
    id: 'incomplete-citation',
    answer:
      'Do not invent the authors, title, venue, DOI, or URL. Treat the citation as incomplete and handle it transparently:\n\nKeep a temporary in-text marker such as [citation details incomplete] or (Deep learning survey, 2021, metadata to be verified).\nIn the references, record only what is known, e.g.:\n\nDeep learning survey. (2021). Incomplete bibliographic record; source to be verified.\n\nSearch your library, project documents, browser history, or the paper file for identifying metadata.\nVerify the record against a reliable source such as the publisher page, Crossref, Google Scholar, or arXiv.\nReplace the placeholder only after confirming the title, author(s), year, and publication details.\n\nIf no source can be identified, either omit the citation from the final bibliography or explicitly label it as an unverified personal note, rather than presenting a guessed reference.',
  },
  {
    id: 'next-research-step',
    answer:
      'Researchers should test the search tool with a larger and more diverse group to determine whether its effectiveness generalizes beyond the small initial study.',
  },
];

describe('captured live answer regressions from 1.2.0', () => {
  it.each(capturedAnswers)(
    'accepts the complete $id answer',
    ({ id, answer }) => {
      const testCase = dataset.cases.find((entry) => entry.id === id);
      if (!testCase) throw new Error(`Missing dataset case: ${id}`);
      expect(gradeAgentAnswer(answer, testCase.criteria).passed).toBe(true);
    }
  );
});

// Authored offline boundary examples; these were not observed in the live run.
describe('1.2.1 rubric concept boundaries', () => {
  const criteriaFor = (id: string): AgentAnswerCriteria => {
    const testCase = dataset.cases.find((entry) => entry.id === id);
    if (!testCase) throw new Error(`Missing dataset case: ${id}`);
    return testCase.criteria;
  };

  it.each([
    'The results are absent, so I cannot identify the better method.',
    "I can't determine which method worked better because results are not provided.",
  ])('accepts both missing-results concepts: %s', (answer) => {
    expect(
      gradeAgentAnswer(answer, criteriaFor('missing-results')).passed
    ).toBe(true);
  });

  it.each([
    'It is impossible to determine which method worked better.',
    'The note does not include results.',
  ])('rejects a missing-results answer lacking one concept: %s', (answer) => {
    expect(
      gradeAgentAnswer(answer, criteriaFor('missing-results')).passed
    ).toBe(false);
  });

  it('still rejects an unsupported better-method claim', () => {
    expect(
      gradeAgentAnswer(
        'The results are absent, so it is impossible to determine; the first method worked better.',
        criteriaFor('missing-results')
      ).passed
    ).toBe(false);
  });

  it.each([
    'Do not invent authors or a DOI; the citation is incomplete. Verify the citation against a reliable source.',
    'Do not invent authors or a DOI; the citation is incomplete. Verify the bibliographic details against a reliable source.',
  ])('accepts explicit citation verification: %s', (answer) => {
    expect(
      gradeAgentAnswer(answer, criteriaFor('incomplete-citation')).passed
    ).toBe(true);
  });

  it('rejects citation caution without a source-verification action', () => {
    expect(
      gradeAgentAnswer(
        'Do not invent authors or a DOI; the citation is incomplete.',
        criteriaFor('incomplete-citation')
      ).passed
    ).toBe(false);
  });

  it.each([
    'Researchers should test the tool with a larger and more diverse sample.',
    'Researchers should evaluate the tool with a larger participant group.',
  ])('accepts a follow-up test with more participants: %s', (answer) => {
    expect(
      gradeAgentAnswer(answer, criteriaFor('next-research-step')).passed
    ).toBe(true);
  });

  it.each([
    'Researchers should test the tool with a more diverse group.',
    'A larger group would help.',
  ])('rejects a follow-up lacking one required concept: %s', (answer) => {
    expect(
      gradeAgentAnswer(answer, criteriaFor('next-research-step')).passed
    ).toBe(false);
  });

  it('still rejects a claim that the larger study already happened', () => {
    expect(
      gradeAgentAnswer(
        'Researchers should test the tool with a larger group; it was already tested with a larger group.',
        criteriaFor('next-research-step')
      ).passed
    ).toBe(false);
  });
});

// Complete answer captured in the 2026-09-20 live rerun of dataset 1.2.1.
describe('captured missing-results regression from the 1.2.1 live run', () => {
  const testCase = dataset.cases.find(
    (entry) => entry.id === 'missing-results'
  );
  if (!testCase) throw new Error('Missing missing-results case');

  it('accepts the complete observed answer', () => {
    const answer =
      'It cannot be determined. The note says the methods were compared but provides no results indicating which performed better.';
    expect(gradeAgentAnswer(answer, testCase.criteria).passed).toBe(true);
  });

  // Authored offline variants of the same uncertainty concept.
  it.each([
    'It cannot be identified which method worked better because there are no results.',
    "It can't be determined which method worked better because results are absent.",
    'We are unable to determine which method worked better without results.',
    'It is not possible to determine which method worked better because results are not provided.',
  ])('accepts a passive uncertainty expression: %s', (answer) => {
    expect(gradeAgentAnswer(answer, testCase.criteria).passed).toBe(true);
  });
});

// Complete citation answer from the same 1.2.1 live rerun.
describe('captured incomplete-citation regression from the 1.2.1 live run', () => {
  const testCase = dataset.cases.find(
    (entry) => entry.id === 'incomplete-citation'
  );
  if (!testCase) throw new Error('Missing incomplete-citation case');

  it('accepts the complete observed answer', () => {
    const answer =
      'Treat “Deep learning survey, 2021” as an incomplete citation placeholder—not as a bibliography entry.\n\nVerification workflow\n\nPreserve the note exactly as found.\nLabel it something like: Unverified source—insufficient metadata.\n\nSearch for identifying context in your files.\nCheck surrounding notes, PDFs, filenames, footnotes, reference lists, quotations, page numbers, and any author names or distinctive phrases. “Deep learning survey” may describe many different papers.\n\nSearch authoritative indexes using the recovered details.\nUse the exact title or a distinctive phrase in sources such as:\n\nCrossref\nGoogle Scholar\nPubMed, if relevant\narXiv\nThe journal or publisher’s website\nYour institution’s library catalogue\n\nConfirm the complete metadata from the original record.\nVerify, rather than infer:\n\nAll authors and author order\nExact title and subtitle\nPublication year and version\nJournal, conference, or repository\nVolume, issue, and pages or article number\nDOI or stable URL\narXiv identifier, if applicable\n\nCross-check at least two reliable sources.\nFor example, compare the publisher’s record with Crossref or the paper itself. Resolve discrepancies in favor of the publisher’s final version or the document’s title page.\n\nDo not cite it until it is resolved.\nIn a draft, use a transparent marker such as:\n\n[UNVERIFIED: “Deep learning survey,” 2021—identify author/title before publication]\n\nDo not invent an author, expand the title, assign a DOI, or select the most likely survey merely because it matches the year.\n\nRecord provenance once identified.\nKeep the original note, the verified source record, the identifier used, and—if relevant—the page or passage that established the match.\n\nIf no unique source can be identified, omit it from the bibliography and mark the associated claim for review. A citation should be included only when another reader could reliably locate the exact source.';
    expect(gradeAgentAnswer(answer, testCase.criteria).passed).toBe(true);
  });

  // Authored offline verification variants.
  it.each([
    'Do not invent authors or a DOI. The citation is incomplete; confirm the original record.',
    'Do not invent authors or a DOI. The citation is incomplete; verify the source record.',
    "Do not invent authors or a DOI. The citation is incomplete; check the publisher's record.",
    'Do not invent authors or a DOI. The citation is incomplete; identify the exact source.',
  ])('accepts source-record verification wording: %s', (answer) => {
    expect(gradeAgentAnswer(answer, testCase.criteria).passed).toBe(true);
  });
});

// Complete answer captured in the 2026-09-20 live rerun of dataset 1.2.1.
describe('captured next-research-step regression from the 1.2.1 live run', () => {
  const testCase = dataset.cases.find(
    (entry) => entry.id === 'next-research-step'
  );
  if (!testCase) throw new Error('Missing next-research-step case');

  it('accepts the complete observed answer', () => {
    const answer =
      'Conduct a larger-scale study with a more diverse group of participants to test whether the search tool’s effectiveness generalizes beyond the initial small study.';
    expect(gradeAgentAnswer(answer, testCase.criteria).passed).toBe(true);
  });

  // Authored offline population-size variants.
  it.each([
    'Researchers should test the tool in a larger study.',
    'Researchers should test the tool with a larger cohort.',
    'Researchers should test the tool with a bigger group.',
    'Researchers should test the tool with a bigger sample.',
    'Researchers should test the tool with more people.',
    'Researchers should test the tool in a larger trial.',
  ])('accepts a larger follow-up population: %s', (answer) => {
    expect(gradeAgentAnswer(answer, testCase.criteria).passed).toBe(true);
  });
});

// Complete answers captured in the 2026-09-25 live run of dataset 1.2.1.
describe('captured live paraphrases from the 2026-09-25 run', () => {
  it.each([
    {
      id: 'incomplete-citation',
      answer:
        'Treat it as an incomplete, unverified reference. Check the original note or source for the authors, exact title, publication venue, date, and DOI/URL; verify these against a reliable bibliographic database. Until then, use a provisional label such as [incomplete reference; metadata unverified] and do not infer its publication type or missing details.',
    },
    {
      id: 'next-research-step',
      answer:
        'Conduct a larger-scale follow-up study with a substantially bigger and diverse participant group to test whether the search tool’s effectiveness generalizes beyond the initial small study.',
    },
  ])('accepts the complete $id answer', ({ id, answer }) => {
    const testCase = dataset.cases.find((entry) => entry.id === id);
    if (!testCase) throw new Error(`Missing dataset case: ${id}`);
    expect(gradeAgentAnswer(answer, testCase.criteria).passed).toBe(true);
  });

  it('accepts compares in a description limited to the brief', () => {
    const testCase = dataset.cases.find(
      (entry) => entry.id === 'draft-project-description'
    );
    if (!testCase) throw new Error('Missing project-description case');
    expect(
      gradeAgentAnswer(
        'This project compares search tools used by librarians.',
        testCase.criteria
      ).passed
    ).toBe(true);
  });

  it('flags the captured project description for topics absent from the brief', () => {
    const testCase = dataset.cases.find(
      (entry) => entry.id === 'draft-project-description'
    );
    if (!testCase) throw new Error('Missing project-description case');
    const answer =
      'This project compares search tools used by librarians, examining their features, usability, search effectiveness, and suitability for different research needs.';
    expect(gradeAgentAnswer(answer, testCase.criteria).passed).toBe(false);
  });

  it('exposes the scope phrase check as a manual-review signal for negated caveats', () => {
    const testCase = dataset.cases.find(
      (entry) => entry.id === 'draft-project-description'
    );
    if (!testCase) throw new Error('Missing project-description case');
    const answer =
      'This project compares search tools used by librarians. The brief does not specify features or usability.';
    const grade = gradeAgentAnswer(answer, testCase.criteria);
    expect(grade.failures).toContain('contains forbidden phrase "features"');
    expect(grade.failures).toContain('contains forbidden phrase "usability"');
  });
});
