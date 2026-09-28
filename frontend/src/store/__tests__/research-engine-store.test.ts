import { beforeEach, describe, expect, it } from 'vitest';
import type {
  PendingReviewResponse,
  RunResponse,
  StepResponse,
} from '@/services/researchEngineService';
import { useResearchEngineStore } from '../research-engine-store';

const RUN_ID = '11111111-1111-4111-8111-111111111111';
const OTHER_RUN_ID = '22222222-2222-4222-8222-222222222222';

function run(id = RUN_ID, overrides: Partial<RunResponse> = {}): RunResponse {
  return {
    id,
    blueprint_id: '33333333-3333-4333-8333-333333333333',
    blueprint_version: 1,
    status: 'running',
    total_tokens: 0,
    created_at: '2026-09-27T10:00:00Z',
    updated_at: '2026-09-27T10:00:01Z',
    ...overrides,
  };
}

function step(
  id: string,
  stepIndex: number,
  overrides: Partial<StepResponse> = {}
): StepResponse {
  return {
    id,
    run_id: RUN_ID,
    step_index: stepIndex,
    step_type: stepIndex === 0 ? 'search' : 'screen',
    mode: 'deterministic',
    temperature: 0,
    token_count: stepIndex + 1,
    started_at: '2026-09-27T10:00:01Z',
    completed_at: '2026-09-27T10:00:02Z',
    output: { version: 'persisted' },
    quality_marks: [],
    ...overrides,
  };
}

describe('research engine run store', () => {
  beforeEach(() => {
    useResearchEngineStore.setState(
      useResearchEngineStore.getInitialState(),
      true
    );
  });

  it('hydrates persisted steps in index order and keeps their database identities', () => {
    const store = useResearchEngineStore.getState();
    store.resetRun(RUN_ID);
    store.hydrateRun(run(), [
      step('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 1),
      step('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 0),
    ]);

    const state = useResearchEngineStore.getState();
    expect(state.activeRun?.id).toBe(RUN_ID);
    expect(
      state.runSteps.map(({ id, step_index }) => [id, step_index])
    ).toEqual([
      ['aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 0],
      ['bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 1],
    ]);
    expect(state.runSteps.every(({ status }) => status === 'complete')).toBe(
      true
    );
  });

  it('reconciles duplicate and out-of-order events by persisted id, then run/index fallback', () => {
    const store = useResearchEngineStore.getState();
    store.resetRun(RUN_ID);
    store.hydrateRun(run(), [step('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 0)]);

    store.mergeRunEvent({
      event: 'step_start',
      run_id: RUN_ID,
      step_index: 0,
      step_type: 'search',
      timestamp: '2026-09-27T10:00:03Z',
    });
    store.mergeRunEvent({
      event: 'step_complete',
      run_id: RUN_ID,
      step_index: 0,
      step_type: 'search',
      output: { version: 'streamed' },
      token_count: 8,
      timestamp: '2026-09-27T10:00:04Z',
    });
    store.mergeRunEvent({
      event: 'step_complete',
      run_id: RUN_ID,
      step_index: 0,
      step_type: 'search',
      output: { version: 'streamed' },
      token_count: 8,
      timestamp: '2026-09-27T10:00:04Z',
    });
    store.mergeRunEvent({
      event: 'step_start',
      run_id: RUN_ID,
      step_index: 0,
      timestamp: '2026-09-27T10:00:02Z',
    });

    expect(useResearchEngineStore.getState().runSteps).toEqual([
      expect.objectContaining({
        id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        run_id: RUN_ID,
        step_index: 0,
        status: 'complete',
        output: { version: 'streamed' },
        token_count: 8,
      }),
    ]);
  });

  it('uses a supplied persisted id before the run/index fallback', () => {
    const store = useResearchEngineStore.getState();
    store.resetRun(RUN_ID);
    store.hydrateRun(run(), [
      step('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 0),
      step('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 1),
    ]);

    store.mergeRunEvent({
      event: 'step_complete',
      id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
      run_id: RUN_ID,
      step_index: 0,
      output: { matched: 'persisted-id' },
    });

    const steps = useResearchEngineStore.getState().runSteps;
    expect(steps).toHaveLength(2);
    expect(steps[0].output).toEqual({ version: 'persisted' });
    expect(steps[1].output).toEqual({ matched: 'persisted-id' });
  });

  it('clears run-owned state on a run switch and ignores late hydration for the old run', () => {
    const store = useResearchEngineStore.getState();
    const review: PendingReviewResponse = {
      pending: true,
      descriptor: {
        run_id: RUN_ID,
        step_index: 1,
        stage_type: 'screen',
        review_kind: 'screening',
        contract_version: 1,
        output_hash: 'a'.repeat(64),
        status: 'pending',
      },
      stage_output: { screening: [] },
    };

    store.resetRun(RUN_ID);
    store.hydrateRun(run(), [step('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 0)]);
    store.setPendingReview(review);
    store.resetRun(OTHER_RUN_ID);
    store.hydrateRun(run(RUN_ID), [
      step('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 0),
    ]);

    const switched = useResearchEngineStore.getState();
    expect(switched.activeRunId).toBe(OTHER_RUN_ID);
    expect(switched.activeRun).toBeNull();
    expect(switched.runSteps).toEqual([]);
    expect(switched.pendingReview).toBeNull();

    switched.hydrateRun(run(OTHER_RUN_ID), [
      step('cccccccc-cccc-4ccc-8ccc-cccccccccccc', 0, {
        run_id: OTHER_RUN_ID,
      }),
    ]);
    expect(useResearchEngineStore.getState().activeRun?.id).toBe(OTHER_RUN_ID);
  });

  it('keeps pending review state server-owned', () => {
    const store = useResearchEngineStore.getState();
    store.resetRun(RUN_ID);
    const review: PendingReviewResponse = {
      pending: true,
      descriptor: {
        run_id: RUN_ID,
        step_index: 2,
        stage_type: 'extract',
        review_kind: 'extraction',
        contract_version: 1,
        output_hash: 'b'.repeat(64),
        status: 'pending',
      },
      stage_output: { extractions: [] },
    };

    store.setPendingReview(review);
    expect(useResearchEngineStore.getState().pendingReview).toBe(review);
  });
});
