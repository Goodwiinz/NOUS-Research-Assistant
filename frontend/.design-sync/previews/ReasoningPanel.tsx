import { ReasoningPanel } from '@nous/design-system';

const steps = [
  {
    title: 'Scoping the question',
    body: 'The user wants a comparison of sparse and linear attention variants on long-document benchmarks, limited to the "Efficient Transformers" project.',
  },
  {
    title: 'Searching project sources',
    body: 'Querying 14 indexed papers for Long Range Arena results and memory complexity claims.',
  },
  {
    title: 'Reconciling reported numbers',
    body: 'Longformer and BigBird report different LRA splits; normalizing to the shared ListOps and Text tasks before comparing.',
  },
];

export const Completed = () => (
  <ReasoningPanel
    steps={steps}
    visibleSteps={steps.length}
    streaming={false}
    defaultOpen
    restingLabel="How this answer was prepared"
    className="max-w-[560px]"
  />
);

export const Streaming = () => (
  <ReasoningPanel
    steps={steps}
    visibleSteps={2}
    streaming
    defaultOpen
    elapsed="4.2s"
    restingLabel="Reasoning summary"
    className="max-w-[560px]"
  />
);

export const Collapsed = () => (
  <ReasoningPanel
    steps={steps}
    visibleSteps={steps.length}
    streaming={false}
    restingLabel="Execution plan · 3/3"
    className="max-w-[560px]"
  />
);
