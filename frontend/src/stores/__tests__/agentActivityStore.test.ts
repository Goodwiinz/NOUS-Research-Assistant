import { beforeEach, describe, expect, it } from 'vitest';
import { useAgentActivityStore } from '../agentActivityStore';

describe('agentActivityStore', () => {
  beforeEach(() => {
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
  });

  const getRun = (
    threadId: string
  ): ReturnType<typeof useAgentActivityStore.getState>['runs'][string] =>
    useAgentActivityStore.getState().runs[threadId];

  it('startRun initializes a running run with empty steps and plan', () => {
    useAgentActivityStore
      .getState()
      .startRun('t1', 'Literature synth', 'Reviewing Mamba-2');

    const run = getRun('t1');
    expect(run.state).toBe('running');
    expect(run.name).toBe('Literature synth');
    expect(run.task).toBe('Reviewing Mamba-2');
    expect(run.steps).toEqual([]);
    expect(run.plan).toEqual([]);
  });

  it('setPlan stores plan items with sequential ids', () => {
    const { startRun, setPlan } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', ['Read the paper', 'Summarize key findings']);
    const plan = getRun('t1').plan;
    expect(plan).toHaveLength(2);
    expect(plan[0].text).toBe('Read the paper');
    expect(plan[0].done).toBe(false);
    expect(plan[1].text).toBe('Summarize key findings');
  });

  it('setPlan is idempotent once a plan exists (avoids clobber on re-emit)', () => {
    const { startRun, setPlan } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', ['first']);
    setPlan('t1', ['second', 'third']);
    const plan = getRun('t1').plan;
    expect(plan).toHaveLength(1);
    expect(plan[0].text).toBe('first');
  });

  it('finishRun(done) marks tool-less plan items as complete', () => {
    const { startRun, setPlan, finishRun } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', ['one', 'two', 'three']);
    finishRun('t1', 'done');
    const plan = getRun('t1').plan;
    expect(plan.every((p) => p.done)).toBe(true);
  });

  it('finishRun(error) leaves plan items as pending', () => {
    const { startRun, setPlan, finishRun } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', ['one', 'two']);
    finishRun('t1', 'error');
    const plan = getRun('t1').plan;
    expect(plan.every((p) => !p.done)).toBe(true);
  });

  it('finishRun(stopped) leaves plan items as pending and sets state', () => {
    const { startRun, setPlan, finishRun } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', ['one', 'two']);
    finishRun('t1', 'stopped');
    const run = getRun('t1');
    expect(run.state).toBe('stopped');
    expect(run.plan.every((p) => !p.done)).toBe(true);
  });

  it('setPlan keeps planner tool hints and strips "N/A"', () => {
    const { startRun, setPlan } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', [
      { text: 'Identify the request', tool: 'N/A' },
      { text: 'Search arXiv', tool: 'search_arxiv' },
      'Respond to the user',
    ]);
    const plan = getRun('t1').plan;
    expect(plan[0].tool).toBeUndefined();
    expect(plan[1].tool).toBe('search_arxiv');
    expect(plan[2].tool).toBeUndefined();
  });

  it('a successful tool completion marks the matching plan item done', () => {
    const { startRun, setPlan, pushToolStart, pushToolEnd } =
      useAgentActivityStore.getState();
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', [
      { text: 'Search arXiv', tool: 'search_arxiv' },
      { text: 'Summarize', tool: 'summarize_document' },
    ]);
    pushToolStart('t1', 'search_arxiv');
    pushToolEnd('t1', 'search_arxiv', true);
    const plan = getRun('t1').plan;
    expect(plan[0].done).toBe(true);
    expect(plan[1].done).toBe(false);
  });

  it('a failed tool does not mark the matching plan item done', () => {
    const { startRun, setPlan, pushToolStart, pushToolEnd } =
      useAgentActivityStore.getState();
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', [{ text: 'Search arXiv', tool: 'search_arxiv' }]);
    pushToolStart('t1', 'search_arxiv');
    pushToolEnd('t1', 'search_arxiv', false);
    expect(getRun('t1').plan[0].done).toBe(false);
  });

  it('repeated tool completions mark successive matching plan items', () => {
    const { startRun, setPlan, pushToolStart, pushToolEnd } =
      useAgentActivityStore.getState();
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', [
      { text: 'Summarize methods', tool: 'summarize_document' },
      { text: 'Summarize results', tool: 'summarize_document' },
    ]);
    pushToolStart('t1', 'summarize_document');
    pushToolEnd('t1', 'summarize_document', true);
    expect(getRun('t1').plan.map((p) => p.done)).toEqual([true, false]);
    pushToolStart('t1', 'summarize_document');
    pushToolEnd('t1', 'summarize_document', true);
    expect(getRun('t1').plan.map((p) => p.done)).toEqual([true, true]);
  });

  it('finishRun(done) does NOT bulk-complete unexecuted tool-bearing items', () => {
    const { startRun, setPlan, pushToolStart, pushToolEnd, finishRun } =
      useAgentActivityStore.getState();
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', [
      { text: 'Identify the request', tool: 'N/A' },
      { text: 'Search arXiv', tool: 'search_arxiv' },
      { text: 'Ingest papers', tool: 'ingest_arxiv' },
    ]);
    // Only the search ran; ingest never executed.
    pushToolStart('t1', 'search_arxiv');
    pushToolEnd('t1', 'search_arxiv', true);
    finishRun('t1', 'done');
    const plan = getRun('t1').plan;
    expect(plan[0].done).toBe(true); // tool-less → closed out at stream end
    expect(plan[1].done).toBe(true); // completed by its tool
    expect(plan[2].done).toBe(false); // never ran → stays pending
  });

  it('a re-send carries the previous run’s completed plan items forward', () => {
    const { startRun, setPlan, pushToolStart, pushToolEnd } =
      useAgentActivityStore.getState();
    const items = [
      { text: 'Search arXiv', tool: 'search_arxiv' },
      { text: 'Ingest papers', tool: 'ingest_arxiv' },
    ];
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', items);
    pushToolStart('t1', 'search_arxiv');
    pushToolEnd('t1', 'search_arxiv', true);

    // Re-send in the same thread: steps reset, but the rail must not report
    // "0 of 2" for work the transcript still shows as completed.
    startRun('t1', 'NOUS', 'task again');
    expect(getRun('t1').steps).toEqual([]);
    setPlan('t1', items);

    expect(getRun('t1').plan.map((p) => p.done)).toEqual([true, false]);
  });

  it('carry-forward survives a re-send whose run never received a plan', () => {
    const { startRun, setPlan, pushToolStart, pushToolEnd } =
      useAgentActivityStore.getState();
    const items = [{ text: 'Search arXiv', tool: 'search_arxiv' }];
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', items);
    pushToolStart('t1', 'search_arxiv');
    pushToolEnd('t1', 'search_arxiv', true);

    startRun('t1', 'NOUS', 'no plan emitted');
    startRun('t1', 'NOUS', 'third try');
    setPlan('t1', items);

    expect(getRun('t1').plan[0].done).toBe(true);
  });

  it('carry-forward never marks an item that was not done before', () => {
    const { startRun, setPlan } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS', 'task');
    setPlan('t1', [{ text: 'Search arXiv', tool: 'search_arxiv' }]);

    startRun('t1', 'NOUS', 'task again');
    setPlan('t1', [
      { text: 'Search arXiv', tool: 'search_arxiv' },
      { text: 'A brand new step', tool: 'ingest_arxiv' },
    ]);

    expect(getRun('t1').plan.map((p) => p.done)).toEqual([false, false]);
  });

  it('pushToolStart appends an active step with a mapped label', () => {
    const { startRun, pushToolStart } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS Agent', 'task');
    pushToolStart('t1', 'arxiv_search');

    const steps = getRun('t1').steps;
    expect(steps).toHaveLength(1);
    expect(steps[0].tool).toBe('arxiv_search');
    expect(steps[0].status).toBe('active');
    expect(steps[0].label).toBe('Search arXiv');
  });

  it('duplicate pushToolStart for same tool is de-duped while active', () => {
    const { startRun, pushToolStart } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS Agent', 'task');
    pushToolStart('t1', 'arxiv_search');
    pushToolStart('t1', 'arxiv_search');
    expect(getRun('t1').steps).toHaveLength(1);
  });

  it('tracks concurrent calls to the same tool by invocation id', () => {
    const { startRun, pushToolStart, pushToolEnd } =
      useAgentActivityStore.getState();
    startRun('t1', 'NOUS Agent', 'task');
    pushToolStart('t1', 'search_documents', 'call-a');
    pushToolStart('t1', 'search_documents', 'call-b');
    pushToolEnd('t1', 'search_documents', true, 'call-a');

    expect(getRun('t1').steps).toMatchObject([
      { callId: 'call-a', status: 'done' },
      { callId: 'call-b', status: 'active' },
    ]);
  });

  it('pushToolEnd flips matching active step to done', () => {
    const { startRun, pushToolStart, pushToolEnd } =
      useAgentActivityStore.getState();
    startRun('t1', 'NOUS Agent', 'task');
    pushToolStart('t1', 'arxiv_search');
    pushToolEnd('t1', 'arxiv_search', true);
    expect(getRun('t1').steps[0].status).toBe('done');
  });

  it('pushToolEnd with ok=false flips to error', () => {
    const { startRun, pushToolStart, pushToolEnd } =
      useAgentActivityStore.getState();
    startRun('t1', 'NOUS Agent', 'task');
    pushToolStart('t1', 'arxiv_search');
    pushToolEnd('t1', 'arxiv_search', false);
    expect(getRun('t1').steps[0].status).toBe('error');
  });

  it('pushToolEnd for unknown tool is ignored', () => {
    const { startRun, pushToolEnd } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS Agent', 'task');
    pushToolEnd('t1', 'nothing_here', true);
    expect(getRun('t1').steps).toEqual([]);
  });

  it('finishRun transitions running → done', () => {
    const { startRun, finishRun } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS Agent', 'task');
    finishRun('t1', 'done');
    expect(getRun('t1').state).toBe('done');
  });

  it('finishRun transitions running → error', () => {
    const { startRun, finishRun } = useAgentActivityStore.getState();
    startRun('t1', 'NOUS Agent', 'task');
    finishRun('t1', 'error');
    expect(getRun('t1').state).toBe('error');
  });

  it.each([
    ['stopped', 'cancelled'],
    ['error', 'error'],
    ['done', 'incomplete'],
  ] as const)(
    'finishRun(%s) settles an unclosed tool as %s',
    (state, status) => {
      const { startRun, pushToolStart, finishRun } =
        useAgentActivityStore.getState();
      startRun('t1', 'NOUS Agent', 'task');
      pushToolStart('t1', 'search_documents');
      finishRun('t1', state);
      expect(getRun('t1').steps[0].status).toBe(status);
    }
  );

  it('runs are isolated per thread', () => {
    const { startRun, pushToolStart } = useAgentActivityStore.getState();
    startRun('t1', 'A', 'taskA');
    startRun('t2', 'B', 'taskB');
    pushToolStart('t1', 'arxiv_search');
    expect(getRun('t1').steps).toHaveLength(1);
    expect(getRun('t2').steps).toHaveLength(0);
    expect(getRun('t1').name).toBe('A');
    expect(getRun('t2').name).toBe('B');
  });

  it('startRun on existing thread replaces prior run', () => {
    const { startRun, pushToolStart } = useAgentActivityStore.getState();
    startRun('t1', 'A', 'first');
    pushToolStart('t1', 'arxiv_search');
    startRun('t1', 'A', 'second');
    expect(getRun('t1').task).toBe('second');
    expect(getRun('t1').steps).toEqual([]);
  });
});

describe('agentActivityStore — currentThreadId lifecycle', () => {
  beforeEach(() => {
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
  });

  it('startRun sets currentThreadId to the new thread', () => {
    useAgentActivityStore.getState().startRun('t1', 'A', 'task');
    expect(useAgentActivityStore.getState().currentThreadId).toBe('t1');
  });

  it('finishRun clears currentThreadId when the finishing thread is current', () => {
    const { startRun, finishRun } = useAgentActivityStore.getState();
    startRun('t1', 'A', 'task');
    finishRun('t1', 'done');
    expect(useAgentActivityStore.getState().currentThreadId).toBeNull();
  });

  it('finishRun does not change currentThreadId when finishing a different thread', () => {
    const { startRun, finishRun } = useAgentActivityStore.getState();
    startRun('t1', 'A', 'task');
    startRun('t2', 'B', 'task'); // t2 is now current
    finishRun('t1', 'done'); // t1 finishes, t2 still current
    expect(useAgentActivityStore.getState().currentThreadId).toBe('t2');
  });
});

describe('agentActivityStore — no-op updates do not notify subscribers', () => {
  beforeEach(() => {
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
  });

  it('pushToolStart for unknown thread does not notify subscribers', () => {
    let calls = 0;
    const unsub = useAgentActivityStore.subscribe(() => {
      calls++;
    });
    useAgentActivityStore.getState().pushToolStart('t-missing', 'arxiv_search');
    unsub();
    expect(calls).toBe(0);
  });

  it('pushToolEnd for unknown thread does not notify subscribers', () => {
    let calls = 0;
    const unsub = useAgentActivityStore.subscribe(() => {
      calls++;
    });
    useAgentActivityStore
      .getState()
      .pushToolEnd('t-missing', 'arxiv_search', true);
    unsub();
    expect(calls).toBe(0);
  });

  it('finishRun for unknown thread does not notify subscribers', () => {
    let calls = 0;
    const unsub = useAgentActivityStore.subscribe(() => {
      calls++;
    });
    useAgentActivityStore.getState().finishRun('t-missing', 'done');
    unsub();
    expect(calls).toBe(0);
  });

  it('duplicate pushToolStart does not notify subscribers', () => {
    const { startRun, pushToolStart } = useAgentActivityStore.getState();
    startRun('t1', 'A', 'task');
    pushToolStart('t1', 'arxiv_search');
    let calls = 0;
    const unsub = useAgentActivityStore.subscribe(() => {
      calls++;
    });
    pushToolStart('t1', 'arxiv_search'); // de-duped, no-op
    unsub();
    expect(calls).toBe(0);
  });

  it('clearRun forgets only that thread and releases the current pointer', () => {
    const { startRun, clearRun } = useAgentActivityStore.getState();
    startRun('t1', 'Agent', 'a');
    startRun('t2', 'Agent', 'b');
    clearRun('t2');
    const { runs } = useAgentActivityStore.getState();
    expect(runs['t2']).toBeUndefined();
    expect(runs['t1']?.state).toBe('running');
    expect(useAgentActivityStore.getState().currentThreadId).toBeNull();
  });

  it('clearRun on an unknown thread is a no-op', () => {
    const calls: number[] = [];
    const unsub = useAgentActivityStore.subscribe(() => calls.push(1));
    useAgentActivityStore.getState().clearRun('missing');
    unsub();
    expect(calls).toHaveLength(0);
  });
});
