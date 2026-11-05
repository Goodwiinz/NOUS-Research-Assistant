import { ToolExecutionCard } from '@nous/design-system';

// One tool call inside an agent turn (AgentMessageItem).
export const Completed = () => (
  <div style={{ maxWidth: 480 }}>
    <ToolExecutionCard
      execution={{ id: 't1', toolName: 'search_arxiv', toolDisplayName: 'Search arXiv', args: { query: 'sparse attention long context' }, status: 'completed', result: { status: 'found', count: 12 }, durationMs: 1840 }}
    />
  </div>
);

export const States = () => (
  <div style={{ maxWidth: 480 }}>
    <ToolExecutionCard execution={{ id: 't2', toolName: 'search_documents', toolDisplayName: 'Search project documents', args: {}, status: 'running' }} />
    <ToolExecutionCard execution={{ id: 't3', toolName: 'ingest_paper', toolDisplayName: 'Ingest paper', args: {}, status: 'failed', error: 'PDF could not be parsed (encrypted).', durationMs: 920 }} />
    <ToolExecutionCard execution={{ id: 't4', toolName: 'create_note', toolDisplayName: 'Create note', args: {}, status: 'cancelled' }} />
  </div>
);
