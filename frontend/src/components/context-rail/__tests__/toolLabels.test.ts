import { describe, expect, it } from 'vitest';
import { humanize, toolLabel, toolStatusLabel } from '../toolLabels';

describe('toolLabel', () => {
  it.each([
    ['arxiv_search', 'Search arXiv'],
    ['arxiv_ingest', 'Ingest papers'],
    ['document_search', 'Search documents'],
    ['ingest_document', 'Ingest document'],
    ['create_draft', 'Draft synthesis'],
    ['revise_draft', 'Revise draft'],
    ['create_note', 'Save note'],
    ['entity_search', 'Query entities'],
    ['kg_query', 'Query knowledge graph'],
    ['project_create', 'Create project'],
    ['compare_documents', 'Compare documents'],
    ['reflect', 'Review progress'],
    ['search_arxiv', 'Search arXiv'],
    ['ingest_arxiv_papers', 'Ingest arXiv papers'],
    ['do_kb_retrieve', 'Search knowledge base'],
    ['get_current_draft', 'Load current draft'],
  ])('maps %s → %s', (tool, expected) => {
    expect(toolLabel(tool)).toBe(expected);
  });

  it('falls back to Title Case for unknown tools', () => {
    expect(toolLabel('foo_bar_baz')).toBe('Foo Bar Baz');
  });

  it('leaves single-word tools as Title Case', () => {
    expect(toolLabel('ponder')).toBe('Ponder');
  });

  it('uses curated copy for generic generated server labels', () => {
    expect(toolLabel('do_kb_retrieve', 'Do Kb Retrieve')).toBe(
      'Search knowledge base'
    );
    expect(toolLabel('custom_tool', 'Run clinical lookup')).toBe(
      'Run clinical lookup'
    );
    expect(toolLabel('custom_tool', '   ')).toBe('Custom Tool');
  });

  it('falls back safely when the tool name is missing', () => {
    expect(humanize(undefined)).toBe('Unknown tool');
    expect(toolLabel(undefined)).toBe('Unknown tool');
  });

  it('gives current tools truthful fallback status copy', () => {
    expect(toolStatusLabel('search_arxiv', 'active')).toBe('Searching arXiv…');
    expect(toolStatusLabel('search_arxiv', 'done')).toBe('Searched arXiv');
    expect(toolStatusLabel('search_arxiv', 'error')).toBe(
      'Search arXiv failed'
    );
    expect(toolStatusLabel('arxiv_search', 'error')).toBe(
      'Search arXiv failed'
    );
    expect(toolStatusLabel('search_arxiv', 'cancelled')).toBe(
      'Cancelled Search arXiv'
    );
    expect(toolStatusLabel('search_arxiv', 'incomplete')).toBe(
      'Search arXiv status unavailable'
    );
    expect(toolStatusLabel('revise_draft', 'active')).toBe('Revising draft…');
    expect(toolStatusLabel('revise_draft', 'done')).toBe('Revised draft');
  });
});
