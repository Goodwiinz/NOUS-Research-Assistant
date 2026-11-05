import { Plan } from '@nous/design-system';
import { ListChecks } from 'lucide-react';

// Shape matches mapPlanToTasks(): one top-level task per planner step, no
// subtasks, the planner's args_hint as the description, the tool as a chip.
const agentRun = [
  { id: '1', title: 'Search project documents for sparse attention methods', description: 'query: sparse attention long context · top_k: 8', status: 'completed', tools: ['search_documents'] },
  { id: '2', title: 'Find recent arXiv papers on block-sparse attention', description: 'query: block-sparse attention 2024 · max_results: 10', status: 'completed', tools: ['search_arxiv'] },
  { id: '3', title: 'Add the three most-cited papers to the project', description: 'paper_ids: ["2004.05150","2007.14062","2309.17453"]', status: 'in-progress', tools: ['add_document_to_project'] },
  { id: '4', title: 'Compare recall results across methods', description: 'metric: needle-in-a-haystack recall', status: 'pending', tools: ['analyze_documents'] },
  { id: '5', title: 'Draft the related-work section with citations', description: 'style: APA · length: 600 words', status: 'pending', tools: ['create_draft'] },
].map((t) => ({ ...t, priority: '', level: 0, dependencies: [] as string[], subtasks: [] }));

// Wrapper ported from AgentMessageItem's collapsible "Execution plan" block.
export const ExecutionPlan = () => (
  <div style={{ width: 560 }} className="my-2 overflow-hidden rounded-lg border border-border/50 bg-muted/20">
    <div className="flex w-full items-center gap-2 px-3 py-2">
      <ListChecks className="h-3.5 w-3.5 shrink-0 text-sol" />
      <span className="text-xs font-medium text-foreground">Execution plan</span>
      <span className="ml-auto text-[10px] tabular-nums text-muted-foreground/70">2/5</span>
    </div>
    <div className="border-t border-border/30">
      <Plan tasks={agentRun} readOnly />
    </div>
  </div>
);

// Research plan with nested subtasks, including a failed and a blocked step.
const reviewPlan = [
  {
    id: '1',
    title: 'Collect sources on retrieval-augmented generation',
    description: 'Gather primary papers and benchmark reports from 2020 onward',
    status: 'in-progress',
    priority: 'high',
    level: 0,
    dependencies: [],
    subtasks: [
      { id: '1.1', title: 'Search arXiv for RAG surveys', description: 'Query cs.CL for survey papers since 2023', status: 'completed', priority: 'high', tools: ['search_arxiv'] },
      { id: '1.2', title: 'Import PDFs into the project', description: 'Ingest the 12 selected papers', status: 'failed', priority: 'medium', tools: ['ingest_document'] },
      { id: '1.3', title: 'Confirm which venue versions to cite', description: 'Two papers have conflicting preprint and camera-ready numbers', status: 'need-help', priority: 'medium', tools: ['search_documents'] },
    ],
  },
  {
    id: '2',
    title: 'Synthesize findings into a literature review',
    description: 'Group methods by retriever design and summarize results',
    status: 'pending',
    priority: 'medium',
    level: 0,
    dependencies: ['1'],
    subtasks: [],
  },
];

export const ResearchReview = () => (
  <div style={{ width: 560 }}>
    <Plan tasks={reviewPlan} readOnly />
  </div>
);
