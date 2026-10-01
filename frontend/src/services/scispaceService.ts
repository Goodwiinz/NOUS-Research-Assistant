/**
 * SciSpace integration API service.
 * Handles all API calls for extraction matrix, table extraction,
 * tone engine, and integrity detection features.
 */

import { api } from '@/services/api-client';
import type {
  CreateMatrixRequest,
  CreateMatrixResponse,
  ExtractionMatrix,
  ExtractionTaskStatus,
  ExtractRegionRequest,
  ExtractRegionResponse,
  IntegrityCheckResponse,
  IntegrityScoreResponse,
  OutlineRequest,
  OutlineResponse,
  PipelineState,
  RewriteRequest,
  RewriteResponse,
  TablesResponse,
  TriggerExtractionRequest,
  TriggerExtractionResponse,
  UpdateMatrixRequest,
  UpdateMatrixResponse,
  UpdatePipelineRequest,
  WriteRequest,
  WriteResponse,
} from '@/types/scispace';
import type {
  ApiExtractionAcceptCreate,
  ApiExtractionAcceptedValue,
  ApiExtractionCellObservations,
  ApiExtractionFormVersion,
  ApiExtractionObservation,
  ApiExtractionObservationCreate,
} from '@/types/api/research-extraction-contract';

// ============================================================================
// Feature 1: Extraction Matrix
// ============================================================================

const RESEARCH_BASE = '/research';

export const listMatrices = (projectId: string) =>
  api.get<{
    matrices: Array<{
      id: string;
      project_id: string;
      name: string;
      columns: CreateMatrixRequest['columns'];
      created_at: string;
    }>;
    total: number;
  }>(`${RESEARCH_BASE}/projects/${projectId}/matrices`);

export const createMatrix = (projectId: string, data: CreateMatrixRequest) =>
  api.post<CreateMatrixResponse>(
    `${RESEARCH_BASE}/projects/${projectId}/matrices`,
    data
  );

export const getMatrix = (matrixId: string) =>
  api.get<ExtractionMatrix>(`${RESEARCH_BASE}/matrices/${matrixId}`);

export const triggerExtraction = (
  matrixId: string,
  data: TriggerExtractionRequest
) =>
  api.post<TriggerExtractionResponse>(
    `${RESEARCH_BASE}/matrices/${matrixId}/extract`,
    data
  );

export const updateMatrix = (matrixId: string, data: UpdateMatrixRequest) =>
  api.patch<UpdateMatrixResponse>(
    `${RESEARCH_BASE}/matrices/${matrixId}`,
    data
  );

export const deleteMatrix = (matrixId: string) =>
  api.delete<{ message: string; matrix_id: string }>(
    `${RESEARCH_BASE}/matrices/${matrixId}`
  );

export const listFormVersions = (
  matrixId: string
): Promise<ApiExtractionFormVersion[]> =>
  api.get<ApiExtractionFormVersion[]>(
    `${RESEARCH_BASE}/matrices/${matrixId}/form-versions`
  );

export const getCellObservations = (
  matrixId: string,
  documentId: string,
  fieldId: string
): Promise<ApiExtractionCellObservations> =>
  api.get<ApiExtractionCellObservations>(
    `${RESEARCH_BASE}/matrices/${matrixId}/observations?${new URLSearchParams({
      document_id: documentId,
      field_id: fieldId,
    })}`
  );

/** A reviewer's own observation; `anchor_start` picks one citation occurrence. */
export const createObservation = (
  matrixId: string,
  data: ApiExtractionObservationCreate
): Promise<ApiExtractionObservation> =>
  api.post<ApiExtractionObservation>(
    `${RESEARCH_BASE}/matrices/${matrixId}/observations`,
    data
  );

/** Adjudicator accept; `anchor_start` / `accept_unverified` resolve the anchor. */
export const acceptExtractionValue = (
  matrixId: string,
  data: ApiExtractionAcceptCreate
): Promise<ApiExtractionAcceptedValue> =>
  api.post<ApiExtractionAcceptedValue>(
    `${RESEARCH_BASE}/matrices/${matrixId}/accepted-values`,
    data
  );

export const getExtractionTaskStatus = (taskId: string) =>
  api.get<ExtractionTaskStatus>(`${RESEARCH_BASE}/extraction-tasks/${taskId}`);

// ============================================================================
// Feature 2: Table/Math Extraction
// ============================================================================

const DOCUMENTS_BASE = '/documents';

export const extractTables = (documentId: string) =>
  api.get<TablesResponse>(`${DOCUMENTS_BASE}/${documentId}/tables`);

export const extractRegion = (documentId: string, data: ExtractRegionRequest) =>
  api.post<ExtractRegionResponse>(
    `${DOCUMENTS_BASE}/${documentId}/extract-region`,
    data
  );

// ============================================================================
// Feature 3: Tone Engine
// ============================================================================

export const rewriteText = (data: RewriteRequest) =>
  api.post<RewriteResponse>(`${RESEARCH_BASE}/rewrite`, data);

// ============================================================================
// Feature 5: Integrity Detection
// ============================================================================

export const triggerIntegrityCheck = (documentId: string) =>
  api.post<IntegrityCheckResponse>(
    `${DOCUMENTS_BASE}/${documentId}/integrity-check`
  );

export const getIntegrityScore = (documentId: string) =>
  api.get<IntegrityScoreResponse>(
    `${DOCUMENTS_BASE}/${documentId}/integrity-score`
  );

// ============================================================================
// Feature 6: AI Writer
// ============================================================================

export const writeText = (data: WriteRequest) =>
  api.post<WriteResponse>(`${RESEARCH_BASE}/write`, data, { timeout: 300000 });

export const generateOutline = (data: OutlineRequest) =>
  api.post<OutlineResponse>(`${RESEARCH_BASE}/outline`, data, {
    timeout: 300000,
  });

// ============================================================================
// Feature 7: Research Pipeline
// ============================================================================

export const getPipeline = (projectId: string) =>
  api.get<PipelineState>(`${RESEARCH_BASE}/projects/${projectId}/pipeline`);

export const updatePipeline = (
  projectId: string,
  data: UpdatePipelineRequest
) =>
  api.patch<PipelineState>(
    `${RESEARCH_BASE}/projects/${projectId}/pipeline`,
    data
  );

export const resetPipeline = (projectId: string) =>
  api.post<PipelineState>(
    `${RESEARCH_BASE}/projects/${projectId}/pipeline/reset`
  );
