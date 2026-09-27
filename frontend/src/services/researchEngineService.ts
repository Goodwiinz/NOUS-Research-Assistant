import { api } from '@/services/api-client';
import type { components } from '@/types/generated/api';

const BASE = '/api/v1/research-engine';

// Types
export type ProjectCreate =
  components['schemas']['src__schemas__research_engine__ProjectCreate'];
export type ProjectResponse =
  components['schemas']['src__schemas__research_engine__ProjectResponse'];

export interface BlueprintStepDef {
  type: string;
  name: string;
  description?: string;
  parameters: Record<string, unknown>;
  model_id?: string;
  model_version?: string;
  mode: 'deterministic' | 'exploratory';
  temperature?: number;
  seed?: number;
}

export interface BlueprintCreate {
  name: string;
  template_source?: string;
  steps: BlueprintStepDef[];
  parameters: Record<string, unknown>;
}

// Functions
export const listProjects = () => api.get(`${BASE}/projects`);

export const createProject = (data: ProjectCreate) =>
  api.post(`${BASE}/projects`, data);

export const getProject = (id: string) =>
  api.get(`${BASE}/projects/${id}`);

export const linkProject = (
  id: string,
  collectionId: string
): Promise<ProjectResponse> =>
  api.patch<ProjectResponse>(`${BASE}/projects/${id}/collection`, {
    collection_id: collectionId,
  });

export const listTemplates = () =>
  api.get(`${BASE}/blueprints/templates`);

export const createBlueprint = (projectId: string, data: BlueprintCreate) =>
  api.post(`${BASE}/blueprints/projects/${projectId}`, data);

export const getBlueprint = (id: string) =>
  api.get(`${BASE}/blueprints/${id}`);

export const startRun = (
  blueprintId: string,
  parametersOverride: Record<string, unknown>
) =>
  api.post(`${BASE}/blueprints/${blueprintId}/runs`, {
    parameters_override: parametersOverride,
  });

export const getRun = (runId: string) => api.get(`${BASE}/runs/${runId}`);

export const pauseRun = (runId: string) =>
  api.post(`${BASE}/runs/${runId}/pause`);

export const resumeRun = (runId: string) =>
  api.post(`${BASE}/runs/${runId}/resume`);

export const getRunManifest = (runId: string) =>
  api.get(`${BASE}/runs/${runId}/manifest`);

export const listSteps = (runId: string) =>
  api.get(`${BASE}/runs/${runId}/steps`);

export const getStep = (stepId: string) =>
  api.get(`${BASE}/steps/${stepId}`);
