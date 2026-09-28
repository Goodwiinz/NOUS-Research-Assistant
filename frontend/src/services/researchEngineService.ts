import { api } from '@/services/api-client';
import type { components } from '@/types/generated/api';

const BASE = '/api/v1/research-engine';

// Types
type GeneratedProjectCreate =
  components['schemas']['src__schemas__research_engine__ProjectCreate'];
export type ProjectCreate = Omit<GeneratedProjectCreate, 'collection_id'> & {
  collection_id: string;
};

type GeneratedProjectResponse =
  components['schemas']['src__schemas__research_engine__ProjectResponse'];
export type ProjectResponse = GeneratedProjectResponse & {
  research_engine_project_id: string;
};

export type LegacyProjectResponse =
  components['schemas']['LegacyProjectResponse'];

export type ResearchProjectRole = components['schemas']['ResearchProjectRole'];

export type ProjectRoleAssignment = Omit<
  components['schemas']['ResearchProjectRoleResponse'],
  'role'
> & {
  role: ResearchProjectRole;
};

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
export const listProjects = (): Promise<ProjectResponse[]> =>
  api.get<ProjectResponse[]>(`${BASE}/projects`);

export const createProject = (data: ProjectCreate): Promise<ProjectResponse> =>
  api.post<ProjectResponse>(`${BASE}/projects`, data);

export const getProject = (collectionId: string): Promise<ProjectResponse> =>
  api.get<ProjectResponse>(`${BASE}/projects/${collectionId}`);

export const getLegacyProject = (
  engineProjectId: string
): Promise<LegacyProjectResponse> =>
  api.get<LegacyProjectResponse>(`${BASE}/legacy-projects/${engineProjectId}`);

export const linkProject = (
  id: string,
  collectionId: string
): Promise<ProjectResponse> =>
  api.patch<ProjectResponse>(`${BASE}/projects/${id}/collection`, {
    collection_id: collectionId,
  });

export const listProjectRoles = (
  collectionId: string
): Promise<ProjectRoleAssignment[]> =>
  api.get<ProjectRoleAssignment[]>(`${BASE}/projects/${collectionId}/roles`);

export const assignProjectRole = (
  collectionId: string,
  userId: string,
  role: ResearchProjectRole
): Promise<ProjectRoleAssignment> =>
  api.put<ProjectRoleAssignment>(`${BASE}/projects/${collectionId}/roles`, {
    user_id: userId,
    role,
  });

export const removeProjectRole = (
  collectionId: string,
  userId: string,
  role: ResearchProjectRole
): Promise<void> =>
  api.delete(`${BASE}/projects/${collectionId}/roles/${userId}/${role}`);

export const listTemplates = () => api.get(`${BASE}/blueprints/templates`);

export const createBlueprint = (projectId: string, data: BlueprintCreate) =>
  api.post(`${BASE}/blueprints/projects/${projectId}`, data);

export const getBlueprint = (id: string) => api.get(`${BASE}/blueprints/${id}`);

export const startRun = (
  blueprintId: string,
  protocolVersionId: string
) =>
  api.post(`${BASE}/blueprints/${blueprintId}/runs`, {
    protocol_version_id: protocolVersionId,
    parameters_override: {},
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

export const getStep = (stepId: string) => api.get(`${BASE}/steps/${stepId}`);
