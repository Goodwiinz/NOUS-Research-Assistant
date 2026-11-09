import { ChatInlinePlan } from '@nous/design-system';

// ChatInlinePlan shows the agent planner's steps above an assistant answer.
// Committed plans start collapsed ("Execution plan · done/total"); a plan with
// a planner rationale, or a live (streaming) plan, starts expanded. Step status
// is derived from the turn's tool executions.

const plan = [
  {
    step: 1,
    description: 'Search the project library for attention-efficiency papers',
    tool: 'search_documents',
    args_hint: { query: 'efficient attention long context' },
    depends_on: [],
  },
  {
    step: 2,
    description: 'Find related sparse-attention work on arXiv',
    tool: 'search_arxiv',
    args_hint: { query: 'sparse attention transformer' },
    depends_on: [1],
  },
  {
    step: 3,
    description: 'Compare complexity and exactness across methods',
    tool: 'synthesize',
    args_hint: {},
    depends_on: [1, 2],
  },
];

const executions = [
  { id: 't1', tool: 'search_documents', label: 'Search documents', status: 'done' as const, durationMs: 820 },
  { id: 't2', tool: 'search_arxiv', label: 'Search arXiv', status: 'done' as const, durationMs: 1640 },
];

export const WithRationale = () => (
  <div className="max-w-[720px]">
    <ChatInlinePlan
      plan={plan}
      reasoning="Check the user's library first, then fill gaps from arXiv before synthesizing a comparison."
      toolExecutions={executions}
    />
  </div>
);

export const Collapsed = () => (
  <div className="max-w-[720px]">
    <ChatInlinePlan plan={plan} toolExecutions={executions} />
  </div>
);

export const Streaming = () => (
  <div className="max-w-[720px]">
    <ChatInlinePlan
      plan={plan}
      streaming
      toolExecutions={[
        executions[0],
        { id: 't2', tool: 'search_arxiv', label: 'Search arXiv', status: 'running' as const },
      ]}
    />
  </div>
);
