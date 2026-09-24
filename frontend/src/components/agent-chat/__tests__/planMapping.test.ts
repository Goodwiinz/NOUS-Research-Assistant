import { describe, expect, it } from 'vitest';
import {
  deriveStepStatus,
  formatArgsHint,
  mapPlanToTasks,
} from '../planMapping';
import type { PlanStep, ToolExecution } from '@/types/agent-chat';

function makeStep(overrides: Partial<PlanStep> = {}): PlanStep {
  return {
    step: 1,
    description: 'Search arXiv',
    tool: 'search_arxiv',
    args_hint: {},
    depends_on: [],
    ...overrides,
  };
}

function makeExec(overrides: Partial<ToolExecution> = {}): ToolExecution {
  return {
    id: 'exec-1',
    toolName: 'search_arxiv',
    toolDisplayName: 'Search arXiv',
    args: {},
    status: 'completed',
    ...overrides,
  };
}

describe('deriveStepStatus', () => {
  it('returns pending when no matching executions', () => {
    expect(deriveStepStatus(makeStep(), [])).toBe('pending');
  });

  it('returns failed when any matching execution failed', () => {
    const execs = [
      makeExec({ status: 'completed' }),
      makeExec({ status: 'failed' }),
    ];
    expect(deriveStepStatus(makeStep(), execs)).toBe('failed');
  });

  it('returns failed when the newest matching execution was cancelled (R4-L20)', () => {
    const execs = [makeExec({ status: 'cancelled' })];
    expect(deriveStepStatus(makeStep(), execs)).toBe('failed');
  });

  it('returns in-progress when any matching execution is running (and none failed)', () => {
    const execs = [
      makeExec({ status: 'completed' }),
      makeExec({ status: 'running' }),
    ];
    expect(deriveStepStatus(makeStep(), execs)).toBe('in-progress');
  });

  it('returns pending when the matching execution is still pending', () => {
    expect(
      deriveStepStatus(makeStep(), [makeExec({ status: 'pending' })])
    ).toBe('pending');
  });

  it('returns completed when all matching executions completed', () => {
    const execs = [makeExec({ status: 'completed' })];
    expect(deriveStepStatus(makeStep(), execs)).toBe('completed');
  });

  it('prioritizes failed over running and completed', () => {
    const execs = [
      makeExec({ status: 'completed' }),
      makeExec({ status: 'running' }),
      makeExec({ status: 'failed' }),
    ];
    expect(deriveStepStatus(makeStep(), execs)).toBe('failed');
  });

  it('ignores executions for other tools', () => {
    const execs = [makeExec({ toolName: 'other_tool', status: 'failed' })];
    expect(deriveStepStatus(makeStep(), execs)).toBe('pending');
  });
});

describe('formatArgsHint', () => {
  it('returns empty string for empty object', () => {
    expect(formatArgsHint({})).toBe('');
  });

  it('formats string values without JSON quoting', () => {
    expect(formatArgsHint({ query: 'transformers' })).toBe(
      'query: transformers'
    );
  });

  it('JSON-stringifies non-string values', () => {
    expect(formatArgsHint({ limit: 5 })).toBe('limit: 5');
  });

  it('joins multiple entries with a middot separator', () => {
    expect(formatArgsHint({ query: 'x', limit: 5 })).toBe(
      'query: x · limit: 5'
    );
  });
});

describe('mapPlanToTasks', () => {
  it('maps step fields onto a flat task list', () => {
    const steps = [
      makeStep({
        step: 1,
        description: 'Search arXiv',
        tool: 'search_arxiv',
        args_hint: { query: 'x' },
        depends_on: [],
      }),
      makeStep({
        step: 2,
        description: 'Ingest paper',
        tool: 'ingest_document',
        args_hint: {},
        depends_on: [1],
      }),
    ];
    const execs = [makeExec({ status: 'completed' })];

    const tasks = mapPlanToTasks(steps, execs);

    expect(tasks).toHaveLength(2);
    expect(tasks[0]).toMatchObject({
      id: '1',
      title: 'Search arXiv',
      description: 'query: x',
      status: 'completed',
      dependencies: [],
      subtasks: [],
      tools: ['search_arxiv'],
    });
    expect(tasks[1]).toMatchObject({
      id: '2',
      title: 'Ingest paper',
      status: 'pending',
      dependencies: ['1'],
      tools: ['ingest_document'],
    });
  });

  it('does not complete every same-tool step from one execution', () => {
    const steps = [
      makeStep({ step: 1, description: 'Search A', tool: 'search_documents' }),
      makeStep({ step: 2, description: 'Search B', tool: 'search_documents' }),
    ];
    const execs = [
      makeExec({ toolName: 'search_documents', status: 'completed' }),
    ];

    const tasks = mapPlanToTasks(steps, execs);

    expect(tasks[0].status).toBe('completed');
    expect(tasks[1].status).toBe('pending');
  });

  it('advances the second same-tool step once its own execution starts', () => {
    const steps = [
      makeStep({ step: 1, description: 'Search A', tool: 'search_documents' }),
      makeStep({ step: 2, description: 'Search B', tool: 'search_documents' }),
    ];
    const execs = [
      makeExec({ toolName: 'search_documents', status: 'completed' }),
      makeExec({ toolName: 'search_documents', status: 'running' }),
    ];

    const tasks = mapPlanToTasks(steps, execs);

    expect(tasks[0].status).toBe('completed');
    expect(tasks[1].status).toBe('in-progress');
  });

  it('keeps a retry with the step it retries', () => {
    // Step 1's tool failed and was retried successfully; step 2 never ran.
    const steps = [
      makeStep({ step: 1, description: 'Search A', tool: 'search_documents' }),
      makeStep({ step: 2, description: 'Search B', tool: 'search_documents' }),
    ];
    const execs = [
      makeExec({ toolName: 'search_documents', status: 'failed' }),
      makeExec({ toolName: 'search_documents', status: 'completed' }),
    ];

    const tasks = mapPlanToTasks(steps, execs);

    expect(tasks[0].status).toBe('completed');
    expect(tasks[1].status).toBe('pending');
  });

  it('omits tools array entry when step has no tool', () => {
    const tasks = mapPlanToTasks([makeStep({ tool: '' })], []);
    expect(tasks[0].tools).toEqual([]);
  });
});
