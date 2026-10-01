import { api } from '@/services/api-client';
import type { components } from '@/types/generated/api';
import type {
  ApiIdentityEvent,
  ApiReport,
  ApiReportMergeRequest,
  ApiStudyLinkRequest,
} from '@/types/api/research-identity-contract';
import type {
  ApiMyScreeningQueue,
  ApiScreeningAdjudicateRequest,
  ApiScreeningAssignment,
  ApiScreeningAssignmentCreate,
  ApiScreeningConflict,
  ApiScreeningEvent,
  ApiScreeningObservation,
  ApiScreeningObservationCreate,
  ApiScreeningQueue,
  ApiScreeningQueueCreate,
  ApiScreeningReopenRequest,
  ApiScreeningResolution,
  ApiScreeningRevokeRequest,
} from '@/types/api/research-screening-contract';
import type {
  ApiFulltextAttemptCreate,
  ApiFulltextRequestCreate,
  ApiFulltextState,
  ApiPrismaFlow,
} from '@/types/api/research-acquisition-contract';
import type {
  ApiCitationChaseRequest,
  ApiCoverage,
  ApiCoverageRequest,
  ApiImportDeclaration,
  ApiImportFormat,
  ApiImportReceipt,
  ApiImportReceiptDetail,
} from '@/types/api/research-corpus-contract';
import type { ApiJourneyResponse } from '@/types/api/research-journey-contract';
import type {
  ApiAppraisal,
  ApiAppraisalAdjudicate,
  ApiAppraisalList,
  ApiAppraisalSubmit,
} from '@/types/api/research-appraisal-contract';
import type {
  ApiCertainty,
  ApiCertaintyCreate,
  ApiContradiction,
  ApiContradictionCreate,
  ApiEvidenceOutcomeList,
  ApiEvidenceTable,
  ApiEvidenceTableCreate,
  ApiEvidenceTablePreview,
} from '@/types/api/research-evidence-contract';
import type {
  ApiSynthesisExecute,
  ApiSynthesisList,
  ApiSynthesisPreview,
  ApiSynthesisResult,
  ApiSynthesisRoles,
} from '@/types/api/research-synthesis-contract';

const BASE = '/api/v1/research-engine';

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
  request: RunCreate | string
): Promise<RunResponse> =>
  api.post<RunResponse>(
    `${BASE}/blueprints/${blueprintId}/runs`,
    typeof request === 'string'
      ? { protocol_version_id: request, parameters_override: {} }
      : request
  );

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

// --- Report / study identity (GOO-299) -----------------------------------

export type {
  ApiIdentityEvent as IdentityEvent,
  ApiReport as ResearchReport,
  ApiReportMergeRequest as ReportMergeRequest,
  ApiStudyLinkRequest as StudyLinkRequest,
} from '@/types/api/research-identity-contract';

export const listReports = (projectId: string): Promise<ApiReport[]> =>
  api.get<ApiReport[]>(`${BASE}/projects/${projectId}/reports`);

export const listReportHistory = (
  projectId: string
): Promise<ApiIdentityEvent[]> =>
  api.get<ApiIdentityEvent[]>(`${BASE}/projects/${projectId}/reports/history`);

export const linkStudy = (
  projectId: string,
  reportId: string,
  data: ApiStudyLinkRequest
): Promise<ApiReport> =>
  api.post<ApiReport>(
    `${BASE}/projects/${projectId}/reports/${reportId}/study-link`,
    data
  );

export const mergeReports = (
  projectId: string,
  data: ApiReportMergeRequest
): Promise<ApiReport> =>
  api.post<ApiReport>(`${BASE}/projects/${projectId}/reports/merge`, data);

// --- Screening queues (GOO-301) ------------------------------------------

export type {
  ApiMyScreeningItem as MyScreeningItem,
  ApiMyScreeningQueue as MyScreeningQueue,
  ApiScreeningConflict as ScreeningConflict,
  ApiScreeningDecision as ScreeningDecision,
  ApiScreeningEvent as ScreeningEvent,
  ApiScreeningObservation as ScreeningObservation,
  ApiScreeningQueue as ScreeningQueue,
  ApiScreeningResolution as ScreeningResolution,
} from '@/types/api/research-screening-contract';

const screening = (projectId: string): string =>
  `${BASE}/projects/${projectId}/screening/queues`;

export const listScreeningQueues = (
  projectId: string
): Promise<ApiScreeningQueue[]> =>
  api.get<ApiScreeningQueue[]>(screening(projectId));

export const createScreeningQueue = (
  projectId: string,
  data: ApiScreeningQueueCreate
): Promise<ApiScreeningQueue> =>
  api.post<ApiScreeningQueue>(screening(projectId), data);

export const assignScreeningReviewer = (
  projectId: string,
  queueId: string,
  data: ApiScreeningAssignmentCreate
): Promise<ApiScreeningAssignment> =>
  api.post<ApiScreeningAssignment>(
    `${screening(projectId)}/${queueId}/assignments`,
    data
  );

export const revokeScreeningAssignment = (
  projectId: string,
  queueId: string,
  assignmentId: string,
  data: ApiScreeningRevokeRequest
): Promise<ApiScreeningAssignment> =>
  api.post<ApiScreeningAssignment>(
    `${screening(projectId)}/${queueId}/assignments/${assignmentId}/revoke`,
    data
  );

export const getMyScreeningQueue = (
  projectId: string,
  queueId: string
): Promise<ApiMyScreeningQueue> =>
  api.get<ApiMyScreeningQueue>(`${screening(projectId)}/${queueId}/mine`);

export const submitScreeningObservation = (
  projectId: string,
  queueId: string,
  data: ApiScreeningObservationCreate
): Promise<ApiScreeningObservation> =>
  api.post<ApiScreeningObservation>(
    `${screening(projectId)}/${queueId}/observations`,
    data
  );

export const listScreeningHistory = (
  projectId: string,
  queueId: string
): Promise<ApiScreeningEvent[]> =>
  api.get<ApiScreeningEvent[]>(`${screening(projectId)}/${queueId}/history`);

// GOO-302: adjudicator reads and events.
export const listScreeningConflicts = (
  projectId: string,
  queueId: string
): Promise<ApiScreeningConflict[]> =>
  api.get<ApiScreeningConflict[]>(
    `${screening(projectId)}/${queueId}/conflicts`
  );

export const adjudicateScreening = (
  projectId: string,
  queueId: string,
  reportId: string,
  data: ApiScreeningAdjudicateRequest
): Promise<ApiScreeningResolution> =>
  api.post<ApiScreeningResolution>(
    `${screening(projectId)}/${queueId}/reports/${reportId}/adjudicate`,
    data
  );

export const reopenScreening = (
  projectId: string,
  queueId: string,
  reportId: string,
  data: ApiScreeningReopenRequest
): Promise<ApiScreeningResolution> =>
  api.post<ApiScreeningResolution>(
    `${screening(projectId)}/${queueId}/reports/${reportId}/reopen`,
    data
  );

// --- Full-text acquisition + PRISMA flow (GOO-303) ------------------------

export type {
  ApiFulltextState as FulltextState,
  ApiFulltextStatus as FulltextStatus,
  ApiPrismaFlow as PrismaFlow,
} from '@/types/api/research-acquisition-contract';

const fulltext = (projectId: string): string =>
  `${BASE}/projects/${projectId}/fulltext`;

export const listFulltext = (projectId: string): Promise<ApiFulltextState[]> =>
  api.get<ApiFulltextState[]>(fulltext(projectId));

export const requestFulltext = (
  projectId: string,
  data: ApiFulltextRequestCreate
): Promise<ApiFulltextState> =>
  api.post<ApiFulltextState>(`${fulltext(projectId)}/requests`, data);

export const recordFulltextAttempt = (
  projectId: string,
  requestId: string,
  data: ApiFulltextAttemptCreate
): Promise<ApiFulltextState> =>
  api.post<ApiFulltextState>(
    `${fulltext(projectId)}/requests/${requestId}/attempts`,
    data
  );

export const getPrismaFlow = (projectId: string): Promise<ApiPrismaFlow> =>
  api.get<ApiPrismaFlow>(`${BASE}/projects/${projectId}/prisma`);

export const downloadPrismaFlow = (
  projectId: string,
  format: 'json' | 'md'
): Promise<void> =>
  api.download(
    `/research-engine/projects/${projectId}/prisma/export?${new URLSearchParams({ format })}`
  );

// --- Search import / citation chase / corpus export (GOO-300) --------------

export type {
  ApiCoverage as CorpusCoverage,
  ApiImportDeclaration as ImportDeclaration,
  ApiImportFormat as ImportFormat,
  ApiImportReceipt as ImportReceipt,
  ApiImportReceiptDetail as ImportReceiptDetail,
} from '@/types/api/research-corpus-contract';

export const importSearchResults = (
  projectId: string,
  file: File,
  format: ApiImportFormat,
  declaration: ApiImportDeclaration
): Promise<ApiImportReceipt> =>
  api.upload<ApiImportReceipt>(`${BASE}/projects/${projectId}/imports`, file, {
    metadata: { format, declaration: JSON.stringify(declaration) },
  });

export const listImports = (projectId: string): Promise<ApiImportReceipt[]> =>
  api.get<ApiImportReceipt[]>(`${BASE}/projects/${projectId}/imports`);

export const getImport = (
  projectId: string,
  receiptId: string
): Promise<ApiImportReceiptDetail> =>
  api.get<ApiImportReceiptDetail>(
    `${BASE}/projects/${projectId}/imports/${receiptId}`
  );

export const chaseCitations = (
  projectId: string,
  data: ApiCitationChaseRequest
): Promise<ApiImportReceipt> =>
  api.post<ApiImportReceipt>(
    `${BASE}/projects/${projectId}/citation-chases`,
    data
  );

export const getCorpusCoverage = (
  projectId: string,
  data: ApiCoverageRequest = { known: [] }
): Promise<ApiCoverage> =>
  api.post<ApiCoverage>(`${BASE}/projects/${projectId}/corpus/coverage`, data);

export const downloadCorpus = (
  projectId: string,
  format: 'json' | 'zip'
): Promise<void> =>
  api.download(
    `/research-engine/projects/${projectId}/corpus/export?${new URLSearchParams({ format })}`
  );

// --- Plan-to-write journey + audit bundle (GOO-308) -------------------------

export const getJourney = (projectId: string): Promise<ApiJourneyResponse> =>
  api.get<ApiJourneyResponse>(`${BASE}/projects/${projectId}/journey`);

/** One zip: every export, a manifest and SHA256SUMS (the server names it). */
export const downloadAuditBundle = (projectId: string): Promise<void> =>
  api.download(`/research-engine/projects/${projectId}/audit-bundle`);

// --- Study-design appraisal (GOO-309) ----------------------------------------

export type {
  ApiAppraisal as Appraisal,
  ApiAppraisalAdjudicate as AppraisalAdjudicate,
  ApiAppraisalDesign as AppraisalDesign,
  ApiAppraisalDomain as AppraisalDomain,
  ApiAppraisalEvidenceOption as AppraisalEvidenceOption,
  ApiAppraisalInstrument as AppraisalInstrument,
  ApiAppraisalList as AppraisalList,
  ApiAppraisalResult as AppraisalResult,
  ApiAppraisalStatus as AppraisalStatus,
  ApiAppraisalSubmit as AppraisalSubmit,
} from '@/types/api/research-appraisal-contract';

const appraisals = (projectId: string): string =>
  `${BASE}/projects/${projectId}/appraisals`;

export const listAppraisals = (projectId: string): Promise<ApiAppraisalList> =>
  api.get<ApiAppraisalList>(appraisals(projectId));

export const submitAppraisal = (
  projectId: string,
  data: ApiAppraisalSubmit
): Promise<ApiAppraisal> => api.post<ApiAppraisal>(appraisals(projectId), data);

export const adjudicateAppraisal = (
  projectId: string,
  data: ApiAppraisalAdjudicate
): Promise<ApiAppraisal> =>
  api.post<ApiAppraisal>(`${appraisals(projectId)}/adjudications`, data);

/** The visible rows only; the server names the file. */
export const exportAppraisals = (projectId: string): Promise<void> =>
  api.download(`/research-engine/projects/${projectId}/appraisals/export`);

// --- Evidence tables, contradictions and certainty (GOO-310) ----------------

export type {
  ApiCertainty as Certainty,
  ApiCertaintyCreate as CertaintyCreate,
  ApiCertaintyLevel as CertaintyLevel,
  ApiCertaintyRatings as CertaintyRatings,
  ApiContradiction as Contradiction,
  ApiContradictionCreate as ContradictionCreate,
  ApiContradictionDissent as ContradictionDissent,
  ApiEvidenceCell as EvidenceCell,
  ApiEvidenceCellState as EvidenceCellState,
  ApiEvidenceOutcome as EvidenceOutcome,
  ApiEvidenceOutcomeList as EvidenceOutcomeList,
  ApiEvidenceRow as EvidenceRow,
  ApiEvidenceTable as EvidenceTable,
  ApiEvidenceTableCreate as EvidenceTableCreate,
  ApiEvidenceTablePreview as EvidenceTablePreview,
  ApiEvidenceTip as EvidenceTip,
  ApiEvidenceUnreviewedCell as EvidenceUnreviewedCell,
  ApiStanceSuggestionGroup as StanceSuggestionGroup,
} from '@/types/api/research-evidence-contract';

const evidence = (projectId: string): string =>
  `${BASE}/projects/${projectId}/evidence`;

export const listEvidence = (
  projectId: string
): Promise<ApiEvidenceOutcomeList> =>
  api.get<ApiEvidenceOutcomeList>(evidence(projectId));

export interface EvidencePreviewQuery {
  outcome_key: string;
  timepoint: string;
  matrix_id: string;
  field_ids: string[];
}

export const previewEvidenceTable = (
  projectId: string,
  query: EvidencePreviewQuery
): Promise<ApiEvidenceTablePreview> => {
  const params = new URLSearchParams({
    outcome_key: query.outcome_key,
    timepoint: query.timepoint,
    matrix_id: query.matrix_id,
  });
  query.field_ids.forEach((id) => params.append('field_ids', id));
  return api.get<ApiEvidenceTablePreview>(
    `${evidence(projectId)}/tables/preview?${params.toString()}`
  );
};

export const createEvidenceTable = (
  projectId: string,
  data: ApiEvidenceTableCreate
): Promise<ApiEvidenceTable> =>
  api.post<ApiEvidenceTable>(`${evidence(projectId)}/tables`, data);

export const recordContradiction = (
  projectId: string,
  data: ApiContradictionCreate
): Promise<ApiContradiction> =>
  api.post<ApiContradiction>(`${evidence(projectId)}/contradictions`, data);

export const assessCertainty = (
  projectId: string,
  data: ApiCertaintyCreate
): Promise<ApiCertainty> =>
  api.post<ApiCertainty>(`${evidence(projectId)}/certainty`, data);

/** Every version, stale ones included; the server names the file. */
export const exportEvidence = (projectId: string): Promise<void> =>
  api.download(`/research-engine/projects/${projectId}/evidence/export`);

// --- Quantitative synthesis (GOO-311) ----------------------------------------

export type {
  ApiSynthesisExclusion as SynthesisExclusion,
  ApiSynthesisExecute as SynthesisExecute,
  ApiSynthesisIncluded as SynthesisIncluded,
  ApiSynthesisList as SynthesisList,
  ApiSynthesisPreview as SynthesisPreview,
  ApiSynthesisResult as SynthesisResult,
  ApiSynthesisRoles as SynthesisRoles,
  ApiSynthesisSelection as SynthesisSelection,
} from '@/types/api/research-synthesis-contract';

const synthesis = (projectId: string): string =>
  `${BASE}/projects/${projectId}/synthesis`;

export const listSynthesis = (projectId: string): Promise<ApiSynthesisList> =>
  api.get<ApiSynthesisList>(synthesis(projectId));

export const previewSynthesis = (
  projectId: string,
  tableVersionId: string,
  roles: ApiSynthesisRoles
): Promise<ApiSynthesisPreview> => {
  const params = new URLSearchParams({
    table_version_id: tableVersionId,
    ...roles,
  });
  return api.get<ApiSynthesisPreview>(
    `${synthesis(projectId)}/preview?${params.toString()}`
  );
};

export const executeSynthesis = (
  projectId: string,
  data: ApiSynthesisExecute
): Promise<ApiSynthesisResult> =>
  api.post<ApiSynthesisResult>(synthesis(projectId), data);

/** Every result with its inputs; the numbers recompute offline. */
export const exportSynthesis = (projectId: string): Promise<void> =>
  api.download(`/research-engine/projects/${projectId}/synthesis/export`);
