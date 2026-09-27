import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mocked } from 'vitest';
import {
  listProjects,
  createProject,
  getProject,
  linkProject,
  listTemplates,
  createBlueprint,
  getBlueprint,
  startRun,
  getRun,
  pauseRun,
  resumeRun,
  getRunManifest,
  listSteps,
  getStep,
  getLegacyProject,
  listProjectRoles,
  assignProjectRole,
  removeProjectRole,
} from '../researchEngineService';
import { api } from '../api-client';

vi.mock('../api-client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

const mockApi = api as Mocked<typeof api>;

const BASE = '/api/v1/research-engine';

describe('researchEngineService', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('listProjects', () => {
    it('calls GET /projects', async () => {
      mockApi.get.mockResolvedValue([]);
      await listProjects();
      expect(mockApi.get).toHaveBeenCalledWith(`${BASE}/projects`);
    });
  });

  describe('createProject', () => {
    it('calls POST /projects with data', async () => {
      const data = { collection_id: 'collection-1' };
      mockApi.post.mockResolvedValue({ id: '1', ...data });
      await createProject(data);
      expect(mockApi.post).toHaveBeenCalledWith(`${BASE}/projects`, data);
    });
  });

  describe('canonical project compatibility', () => {
    it('resolves historical engine ids separately', async () => {
      mockApi.get.mockResolvedValue({
        research_engine_project_id: 'engine-1',
        collection_id: 'collection-1',
      });

      await getLegacyProject('engine-1');

      expect(mockApi.get).toHaveBeenCalledWith(
        `${BASE}/legacy-projects/engine-1`
      );
    });

    it('uses the canonical collection id for independent roles', async () => {
      mockApi.get.mockResolvedValue([]);
      mockApi.put.mockResolvedValue({
        id: 'assignment-1',
        project_id: 'collection-1',
        user_id: 'user-1',
        role: 'reviewer',
      });

      await listProjectRoles('collection-1');
      await assignProjectRole('collection-1', 'user-1', 'reviewer');
      await removeProjectRole('collection-1', 'user-1', 'reviewer');

      expect(mockApi.get).toHaveBeenCalledWith(
        `${BASE}/projects/collection-1/roles`
      );
      expect(mockApi.put).toHaveBeenCalledWith(
        `${BASE}/projects/collection-1/roles`,
        { user_id: 'user-1', role: 'reviewer' }
      );
      expect(mockApi.delete).toHaveBeenCalledWith(
        `${BASE}/projects/collection-1/roles/user-1/reviewer`
      );
    });
  });

  describe('getProject', () => {
    it('calls GET /projects/:id', async () => {
      mockApi.get.mockResolvedValue({ id: 'p1' });
      await getProject('p1');
      expect(mockApi.get).toHaveBeenCalledWith(`${BASE}/projects/p1`);
    });
  });

  describe('linkProject', () => {
    it('links an engine project to the canonical collection id', async () => {
      mockApi.patch.mockResolvedValue({ id: 'p1', collection_id: 'c1' });
      await linkProject('p1', 'c1');
      expect(mockApi.patch).toHaveBeenCalledWith(
        `${BASE}/projects/p1/collection`,
        { collection_id: 'c1' }
      );
    });
  });

  describe('listTemplates', () => {
    it('calls GET /blueprints/templates', async () => {
      mockApi.get.mockResolvedValue([]);
      await listTemplates();
      expect(mockApi.get).toHaveBeenCalledWith(`${BASE}/blueprints/templates`);
    });
  });

  describe('createBlueprint', () => {
    it('calls POST /blueprints/projects/:projectId with data', async () => {
      const data = {
        name: 'Blueprint 1',
        steps: [
          {
            type: 'search',
            name: 'step1',
            parameters: {},
            mode: 'deterministic' as const,
          },
        ],
        parameters: { key: 'value' },
      };
      mockApi.post.mockResolvedValue({ id: 'b1' });
      await createBlueprint('p1', data);
      expect(mockApi.post).toHaveBeenCalledWith(
        `${BASE}/blueprints/projects/p1`,
        data
      );
    });
  });

  describe('getBlueprint', () => {
    it('calls GET /blueprints/:id', async () => {
      mockApi.get.mockResolvedValue({ id: 'b1' });
      await getBlueprint('b1');
      expect(mockApi.get).toHaveBeenCalledWith(`${BASE}/blueprints/b1`);
    });
  });

  describe('startRun', () => {
    it('calls POST /blueprints/:blueprintId/runs with parameters_override', async () => {
      const params = { temperature: 0.5 };
      mockApi.post.mockResolvedValue({ id: 'r1' });
      await startRun('b1', params);
      expect(mockApi.post).toHaveBeenCalledWith(`${BASE}/blueprints/b1/runs`, {
        parameters_override: params,
      });
    });
  });

  describe('getRun', () => {
    it('calls GET /runs/:runId', async () => {
      mockApi.get.mockResolvedValue({ id: 'r1' });
      await getRun('r1');
      expect(mockApi.get).toHaveBeenCalledWith(`${BASE}/runs/r1`);
    });
  });

  describe('pauseRun', () => {
    it('calls POST /runs/:runId/pause', async () => {
      mockApi.post.mockResolvedValue({ status: 'paused' });
      await pauseRun('r1');
      expect(mockApi.post).toHaveBeenCalledWith(`${BASE}/runs/r1/pause`);
    });
  });

  describe('resumeRun', () => {
    it('calls POST /runs/:runId/resume', async () => {
      mockApi.post.mockResolvedValue({ status: 'running' });
      await resumeRun('r1');
      expect(mockApi.post).toHaveBeenCalledWith(`${BASE}/runs/r1/resume`);
    });
  });

  describe('getRunManifest', () => {
    it('calls GET /runs/:runId/manifest', async () => {
      mockApi.get.mockResolvedValue({ manifest: {} });
      await getRunManifest('r1');
      expect(mockApi.get).toHaveBeenCalledWith(`${BASE}/runs/r1/manifest`);
    });
  });

  describe('listSteps', () => {
    it('calls GET /runs/:runId/steps', async () => {
      mockApi.get.mockResolvedValue([]);
      await listSteps('r1');
      expect(mockApi.get).toHaveBeenCalledWith(`${BASE}/runs/r1/steps`);
    });
  });

  describe('getStep', () => {
    it('calls GET /steps/:stepId', async () => {
      mockApi.get.mockResolvedValue({ id: 's1' });
      await getStep('s1');
      expect(mockApi.get).toHaveBeenCalledWith(`${BASE}/steps/s1`);
    });
  });
});
