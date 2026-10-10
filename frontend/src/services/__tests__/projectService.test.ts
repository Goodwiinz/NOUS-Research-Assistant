import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { listWorkflowLinkOptions, projectService } from '../projectService';
import { api } from '../api-client';

describe('listWorkflowLinkOptions', () => {
  it('paginates at the API maximum and returns only manageable live projects', async () => {
    const eligible = {
      id: 'eligible',
      name: 'Eligible',
      workspace_id: 'workspace',
      can_manage: true,
      workspace_archived: false,
      research_status: 'active' as const,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    };
    const first = Array.from({ length: 100 }, (_, index) => ({
      ...eligible,
      id: `project-${index}`,
      can_manage: index !== 0,
      research_engine_project_id: index === 1 ? 'already-mapped' : null,
    }));
    const list = vi
      .spyOn(projectService, 'listProjects')
      .mockResolvedValueOnce({ projects: first, total: 101, has_next: true })
      .mockResolvedValueOnce({
        projects: [{ ...eligible, id: 'project-100' }],
        total: 101,
        has_next: false,
      });

    const result = await listWorkflowLinkOptions();

    expect(list).toHaveBeenNthCalledWith(1, { skip: 0, limit: 100 });
    expect(list).toHaveBeenNthCalledWith(2, { skip: 100, limit: 100 });
    expect(result).toHaveLength(99);
    expect(result.some((project) => project.id === 'project-1')).toBe(false);
    expect(result.at(-1)?.id).toBe('project-100');
  });
});

describe('projectService.downloadBibliography', () => {
  const createObjectURL = vi.fn(() => 'blob:download-url');
  const revokeObjectURL = vi.fn();
  const appendChild = vi.spyOn(document.body, 'appendChild');
  const removeChild = vi.spyOn(document.body, 'removeChild');

  beforeEach(() => {
    vi.restoreAllMocks();
    appendChild.mockImplementation(() => document.createElement('div'));
    removeChild.mockImplementation(() => document.createElement('div'));
    Object.defineProperty(window, 'URL', {
      writable: true,
      value: {
        createObjectURL,
        revokeObjectURL,
      },
    });
  });

  it('uses the .bib extension for bibtex downloads', async () => {
    vi.spyOn(projectService, 'getProjectBibliography')
      .mockResolvedValue({
        project_id: 'proj-1',
        project_name: 'alpha-project',
        format: 'bibtex',
        content: '@article{test}',
        citation_count: 1,
        generated_at: new Date().toISOString(),
      });

    const click = vi.fn();
    const originalCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tagName: string) => {
      if (tagName === 'a') {
        const anchor = originalCreateElement('a');
        vi.spyOn(anchor, 'click').mockImplementation(click);
        return anchor;
      }
      return originalCreateElement(tagName);
    });

    await projectService.downloadBibliography('proj-1', 'bibtex');

    const createdAnchor = (document.createElement as Mock).mock.results[0]
      .value as HTMLAnchorElement;
    expect(createdAnchor.download).toBe('alpha-project-bibliography.bib');
    expect(click).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:download-url');
  });
});

describe('reference exports (GOO-317)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('default export format still markdown', async () => {
    const download = vi.spyOn(api, 'download').mockResolvedValue(undefined);
    await projectService.downloadDraftExport('p1', 'd1');
    expect(download).toHaveBeenCalledWith(
      '/projects/p1/drafts/d1/export?format=markdown&include_bibliography=true&bib_format=bibtex',
      'draft.md'
    );
    await projectService.downloadDraftExport('p1', 'd1', 'latex');
    expect(download).toHaveBeenLastCalledWith(
      expect.stringContaining('format=latex'),
      'draft.zip'
    );
  });

  it('csl and ris options call the right format', async () => {
    const download = vi.spyOn(api, 'download').mockResolvedValue(undefined);
    await projectService.downloadDraftExport('p1', 'd1', 'csl-json');
    expect(download).toHaveBeenLastCalledWith(
      expect.stringContaining('/projects/p1/drafts/d1/export?format=csl-json'),
      'references.json'
    );
    await projectService.exportDraft('p1', 'd1', 'ris');
    expect(download).toHaveBeenLastCalledWith(
      expect.stringContaining('/projects/p1/drafts/d1/export?format=ris'),
      'references.ris'
    );
  });

  it('release reference download uses release endpoint', async () => {
    const download = vi.spyOn(api, 'download').mockResolvedValue(undefined);
    await projectService.downloadReleaseReferences('p1', 'r1', 'ris');
    expect(download).toHaveBeenCalledWith(
      '/projects/p1/manuscript-releases/r1/references?format=ris',
      'references.ris'
    );
    const get = vi
      .spyOn(api, 'get')
      .mockResolvedValue({ format: 'csl-json', records: 0, omissions: [] });
    await projectService.getReleaseReferenceReport('p1', 'r1');
    expect(get).toHaveBeenCalledWith(
      '/projects/p1/manuscript-releases/r1/references?format=csl-json&report=true'
    );
  });
});
