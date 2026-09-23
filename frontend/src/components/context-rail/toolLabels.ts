interface ToolCopy {
  label: string;
  active: string;
  done: string;
}

const copy = (label: string, active: string, done: string): ToolCopy => ({
  label,
  active,
  done,
});

/** One vocabulary for every /chat tool surface: live, committed, and reload. */
const TOOL_COPY: Record<string, ToolCopy> = {
  arxiv_search: copy('Search arXiv', 'Searching arXiv', 'Searched arXiv'),
  arxiv_ingest: copy('Ingest papers', 'Ingesting papers', 'Ingested papers'),
  document_search: copy(
    'Search documents',
    'Searching documents',
    'Searched documents'
  ),
  ingest_document: copy(
    'Ingest document',
    'Ingesting document',
    'Ingested document'
  ),
  project_create: copy('Create project', 'Creating project', 'Created project'),
  create_note: copy('Save note', 'Saving note', 'Saved note'),
  entity_search: copy(
    'Query entities',
    'Querying entities',
    'Queried entities'
  ),
  kg_query: copy(
    'Query knowledge graph',
    'Querying the knowledge graph',
    'Queried the knowledge graph'
  ),
  reflect: copy('Review progress', 'Reviewing progress', 'Reviewed progress'),
  search_arxiv: copy('Search arXiv', 'Searching arXiv', 'Searched arXiv'),
  ingest_arxiv_papers: copy(
    'Ingest arXiv papers',
    'Ingesting arXiv papers',
    'Ingested arXiv papers'
  ),
  search_documents: copy(
    'Search documents',
    'Searching documents',
    'Searched documents'
  ),
  do_kb_retrieve: copy(
    'Search knowledge base',
    'Searching the knowledge base',
    'Searched the knowledge base'
  ),
  create_project: copy('Create project', 'Creating project', 'Created project'),
  create_project_note: copy(
    'Save project note',
    'Saving project note',
    'Saved project note'
  ),
  search_knowledge_graph: copy(
    'Search knowledge graph',
    'Searching the knowledge graph',
    'Searched the knowledge graph'
  ),
  create_draft: copy(
    'Draft synthesis',
    'Drafting synthesis',
    'Drafted synthesis'
  ),
  revise_draft: copy('Revise draft', 'Revising draft', 'Revised draft'),
  add_document_to_project: copy(
    'Add document to project',
    'Adding document to project',
    'Added document to project'
  ),
  compare_documents: copy(
    'Compare documents',
    'Comparing documents',
    'Compared documents'
  ),
  explore_entity_neighborhood: copy(
    'Explore related entities',
    'Exploring related entities',
    'Explored related entities'
  ),
  export_bibliography: copy(
    'Export bibliography',
    'Exporting bibliography',
    'Exported bibliography'
  ),
  extract_entities: copy(
    'Extract entities',
    'Extracting entities',
    'Extracted entities'
  ),
  find_entity_paths: copy(
    'Find entity paths',
    'Finding entity paths',
    'Found entity paths'
  ),
  get_graph_stats: copy(
    'Get graph statistics',
    'Getting graph statistics',
    'Got graph statistics'
  ),
  get_current_draft: copy(
    'Load current draft',
    'Loading current draft',
    'Loaded current draft'
  ),
  list_external_databases: copy(
    'List external databases',
    'Listing external databases',
    'Listed external databases'
  ),
  list_project_documents: copy(
    'List project documents',
    'Listing project documents',
    'Listed project documents'
  ),
  list_projects: copy('List projects', 'Listing projects', 'Listed projects'),
  search_external_database: copy(
    'Search external database',
    'Searching an external database',
    'Searched an external database'
  ),
  summarize_document: copy(
    'Summarize document',
    'Summarizing document',
    'Summarized document'
  ),
};

export function humanize(tool?: string): string {
  if (typeof tool !== 'string' || !tool) return 'Unknown tool';
  return tool.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

export function toolLabel(tool?: string, serverLabel?: string): string {
  const fallback = humanize(tool);
  const explicitServerLabel = serverLabel?.trim() || undefined;
  if (explicitServerLabel && explicitServerLabel !== fallback) {
    return explicitServerLabel;
  }
  return (
    (tool ? TOOL_COPY[tool]?.label : undefined) ??
    explicitServerLabel ??
    fallback
  );
}

/** Status-aware microcopy shared by the transcript and context rail. */
export function toolStatusLabel(
  tool: string,
  status: 'active' | 'done' | 'error' | 'cancelled' | 'incomplete'
): string {
  const labels = TOOL_COPY[tool];
  if (status === 'active') {
    return `${labels?.active ?? `Running ${toolLabel(tool)}`}…`;
  }
  if (status === 'done') {
    return labels?.done ?? `${toolLabel(tool)} complete`;
  }
  if (status === 'cancelled') return `Cancelled ${toolLabel(tool)}`;
  if (status === 'incomplete') return `${toolLabel(tool)} status unavailable`;
  return `${toolLabel(tool)} failed`;
}
