import { NousAgentStatusCard } from '@nous/design-system';

export const Running = () => (
  <div className="max-w-[420px]">
    <NousAgentStatusCard
      name="Literature Scout"
      task="Surveying efficient attention papers since 2020"
      steps={[
        { label: 'Searched arXiv for "linear attention"', status: 'done' },
        { label: 'Ranked 38 candidates by citation count', status: 'done' },
        { label: 'Reading FlashAttention-2 methods section', status: 'active' },
        { label: 'Draft comparison table', status: 'pending' },
      ]}
    />
  </div>
);

export const Completed = () => (
  <div className="max-w-[420px]">
    <NousAgentStatusCard
      name="Citation Checker"
      task="Verified references in Chapter 3 draft"
      active={false}
      steps={[
        { label: 'Extracted 24 citations from the draft', status: 'done' },
        { label: 'Matched 24 citations to project sources', status: 'done' },
        { label: 'Flagged 2 page-number mismatches', status: 'done' },
      ]}
    />
  </div>
);

export const Failed = () => (
  <div className="max-w-[420px]">
    <NousAgentStatusCard
      name="Ingestion Agent"
      task="Importing 6 PDFs into Efficient Transformers"
      active={false}
      steps={[
        { label: 'Uploaded 6 files to the project', status: 'done' },
        { label: 'Parsing scanned_appendix.pdf failed', status: 'error' },
        { label: 'Index remaining documents', status: 'incomplete' },
        { label: 'Build knowledge-graph entities', status: 'cancelled' },
      ]}
    />
  </div>
);
