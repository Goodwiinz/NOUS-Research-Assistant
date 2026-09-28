import { api } from '@/services/api-client';
import type { components } from '@/types/generated/api';

const BASE = '/api/v1/research-engine';

export type ProjectCreate =
  components['schemas']['src__schemas__research_engine__ProjectCreate'];
export type ProjectResponse =
  components['schemas']['src__schemas__research_engine__ProjectResponse'];
export type BlueprintStepDef = components['schemas']['BlueprintStepDefinition'];
export type BlueprintCreate = components['schemas']['BlueprintCreate'];
export type BlueprintResponse = components['schemas']['BlueprintResponse'];
export type BlueprintTemplateDetail =
  components['schemas']['BlueprintTemplateDetailResponse'];
export type ConnectorCapability =
  components['schemas']['ConnectorCapabilityResponse'];
export type DailyBriefScopeConfirmation =
  components['schemas']['DailyBriefScopeConfirmation'];
export type RunCreate = components['schemas']['RunCreate'];
export type RunResponse = components['schemas']['RunResponse'];
export type RunResumeRequest = components['schemas']['RunResumeRequest'];
export type PendingReviewResponse =
  components['schemas']['PendingReviewResponse'];
export type StageReviewRequest = components['schemas']['StageReviewRequest'];
export type StageReviewResponse = components['schemas']['StageReviewResponse'];
export type ResearchExportFormat =
  components['schemas']['src__schemas__research_engine__ExportFormat'];
export type StepResponse = components['schemas']['StepResponse'];

/** Presentation model for the legacy untyped template-summary endpoint. */
export type BlueprintTemplateSummary = {
  id: string;
  slug: string;
  name: string;
  description?: string;
  step_count: number;
};

type BlueprintTemplateWireSummary = Omit<BlueprintTemplateSummary, 'id'> & {
  id?: string;
};

export const listProjects = (): Promise<ProjectResponse[]> =>
  api.get<ProjectResponse[]>(`${BASE}/projects`);

export const createProject = (data: ProjectCreate): Promise<ProjectResponse> =>
  api.post<ProjectResponse>(`${BASE}/projects`, data);

export const getProject = (id: string): Promise<ProjectResponse> =>
  api.get<ProjectResponse>(`${BASE}/projects/${id}`);

export const listTemplates = async (): Promise<BlueprintTemplateSummary[]> => {
  const templates = await api.get<BlueprintTemplateWireSummary[]>(
    `${BASE}/blueprints/templates`
  );
  return templates.map((template) => ({
    ...template,
    id: template.id ?? template.slug,
  }));
};

export const getTemplateDetail = (
  slug: string
): Promise<BlueprintTemplateDetail> =>
  api.get<BlueprintTemplateDetail>(`${BASE}/blueprints/templates/${slug}`);

export const getCapabilities = (): Promise<ConnectorCapability[]> =>
  api.get<ConnectorCapability[]>(`${BASE}/capabilities`);

export const createBlueprint = (
  projectId: string,
  data: BlueprintCreate
): Promise<BlueprintResponse> =>
  api.post<BlueprintResponse>(`${BASE}/blueprints/projects/${projectId}`, data);

export const getBlueprint = (id: string): Promise<BlueprintResponse> =>
  api.get<BlueprintResponse>(`${BASE}/blueprints/${id}`);

export const startRun = (
  blueprintId: string,
  request: RunCreate
): Promise<RunResponse> =>
  api.post<RunResponse>(`${BASE}/blueprints/${blueprintId}/runs`, request);

export const getRun = (runId: string): Promise<RunResponse> =>
  api.get<RunResponse>(`${BASE}/runs/${runId}`);

export const pauseRun = (runId: string): Promise<RunResponse> =>
  api.post<RunResponse>(`${BASE}/runs/${runId}/pause`);

export const resumeRun = (
  runId: string,
  request: RunResumeRequest = { continue_unverified: false }
): Promise<RunResponse> =>
  api.post<RunResponse>(`${BASE}/runs/${runId}/resume`, request);

export const getPendingReview = (
  runId: string
): Promise<PendingReviewResponse> =>
  api.get<PendingReviewResponse>(`${BASE}/runs/${runId}/reviews/pending`);

export const submitReview = (
  runId: string,
  stepIndex: number,
  request: StageReviewRequest
): Promise<StageReviewResponse> =>
  api.post<StageReviewResponse>(
    `${BASE}/runs/${runId}/reviews/${stepIndex}`,
    request
  );

export const getRunExportUrl = (
  runId: string,
  format: ResearchExportFormat
): string => `${BASE}/runs/${runId}/export?${new URLSearchParams({ format })}`;

export const downloadRunExport = (
  runId: string,
  format: ResearchExportFormat
): Promise<void> =>
  api.download(
    `/research-engine/runs/${runId}/export?${new URLSearchParams({ format })}`
  );

export const getRunManifest = (runId: string): Promise<unknown> =>
  api.get<unknown>(`${BASE}/runs/${runId}/manifest`);

export const listSteps = (runId: string): Promise<StepResponse[]> =>
  api.get<StepResponse[]>(`${BASE}/runs/${runId}/steps`);

export const getStep = (stepId: string): Promise<StepResponse> =>
  api.get<StepResponse>(`${BASE}/steps/${stepId}`);
