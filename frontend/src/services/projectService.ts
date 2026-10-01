/**
 * Project Service
 * API client for Research Assistant project endpoints
 */

import { api } from '@/services/api-client';
import { API_CONFIG } from '@/types/api';
import type { components } from '@/types/generated/api';
import type {
  ApiClaimAssessment,
  ApiClaimAssessmentCreate,
  ApiClaimCreate,
  ApiClaimLink,
  ApiClaimLinkCreate,
  ApiClaimListResponse,
  ApiClaimResponse,
  ApiStanceObservation,
} from '@/types/api/research-claims-contract';
import type {
  ApiDraftPromoteRequest,
  ApiDraftRelease,
  ApiReleaseCheck,
  ApiReleaseStatus,
} from '@/types/api/research-release-contract';
import type {
  ApiDraftDiff,
  ApiPeerReviewDecision,
  ApiPeerReviewDecisionCreate,
  ApiPeerReviewResponse,
  ApiPeerReviewResponseCreate,
  ApiPeerReviewRoundDetail,
  ApiPeerReviewRoundList,
} from '@/types/api/peer-review-contract';

// Types
export interface Project {
  id: string;
  name: string;
  description?: string;
  workspace_id: string;
  project_type?: 'research' | 'literature_review' | 'thesis' | 'paper';
  research_status?: 'active' | 'paused' | 'completed' | 'archived';
  research_goals?: string;
  deadline?: string | null;
  tags?: string[];
  is_private?: boolean;
  user_id?: string;
  created_at: string;
  updated_at: string;
  document_count?: number;
  citation_count?: number;
  note_count?: number;
  draft_count?: number;
  research_engine_project_id?: string | null;
  can_edit?: boolean;
  can_manage?: boolean;
  workspace_archived?: boolean;
}

export interface ProjectCreate {
  workspace_id: string;
  name: string;
  description?: string;
  project_type?: 'research' | 'literature_review' | 'thesis' | 'paper';
  research_goals?: string;
  deadline?: string;
  tags?: string[];
}

export interface ProjectUpdate {
  name?: string;
  description?: string;
  project_type?: 'research' | 'literature_review' | 'thesis' | 'paper';
  research_status?: 'active' | 'paused' | 'completed' | 'archived';
  research_goals?: string;
  deadline?: string;
  tags?: string[];
}

export interface ProjectDocument {
  id: string;
  project_id: string;
  document_id: string;
  added_at?: string;
  sort_order?: number;
  document?: {
    id: string;
    title: string;
    filename: string;
    status: string;
    created_at?: string;
  };
}

export interface ProjectNote {
  id: string;
  project_id: string;
  user_id?: string;
  title: string;
  content: string;
  content_preview?: string;
  linked_document_ids?: string[];
  linked_document_count?: number;
  tags?: string[];
  is_pinned: boolean;
  created_at: string;
  updated_at: string;
}

export interface ProjectNoteCreate {
  title: string;
  content: string;
  linked_document_ids?: string[];
  tags?: string[];
  is_pinned?: boolean;
}

export interface ProjectNoteUpdate {
  title?: string;
  content?: string;
  linked_document_ids?: string[];
  tags?: string[];
  is_pinned?: boolean;
}

export interface ProjectMemory {
  id: string;
  project_id: string;
  user_id?: string;
  content: string;
  source: string;
  created_at: string;
  updated_at: string;
}

export interface ProjectMemoryListResponse {
  memories: ProjectMemory[];
  total: number;
}

export interface ProjectListResponse {
  projects: Project[];
  total: number;
  page?: number;
  size?: number;
  has_next?: boolean;
  has_prev?: boolean;
  skip?: number;
  limit?: number;
}

export async function listWorkflowLinkOptions(): Promise<Project[]> {
  const projects: Project[] = [];
  const limit = 100;
  for (let skip = 0; ; skip += limit) {
    const page = await projectService.listProjects({ skip, limit });
    projects.push(...page.projects);
    if (!page.has_next && page.projects.length < limit) break;
  }
  return projects.filter(
    (project) =>
      project.can_manage === true &&
      project.workspace_archived !== true &&
      project.research_status !== 'archived' &&
      !project.research_engine_project_id
  );
}

export interface ProjectDocumentListResponse {
  documents: ProjectDocument[];
  total: number;
}

export interface ProjectNoteListResponse {
  notes: ProjectNote[];
  total: number;
  page?: number;
  size?: number;
}

export interface ProjectBibliography {
  project_id: string;
  project_name: string;
  format: string;
  content: string;
  citation_count: number;
  generated_at: string;
}

export const projectService = {
  // =========================================================================
  // Project CRUD (T061)
  // =========================================================================

  /**
   * List all projects for current user.
   *
   * `options.signal` can be an AbortSignal; when it fires, axios cancels the
   * in-flight request (used by the page effect to drop stale responses when
   * the workspace changes mid-fetch).
   */
  async listProjects(
    params?: {
      workspace_id?: string;
      skip?: number;
      limit?: number;
      search?: string;
      project_status?: 'active' | 'paused' | 'completed' | 'archived';
      project_type?: 'research' | 'literature_review' | 'thesis' | 'paper';
      tag?: string;
    },
    options?: { signal?: AbortSignal }
  ): Promise<ProjectListResponse> {
    const qs = params
      ? new URLSearchParams(
          Object.entries(params)
            .filter(([, v]) => v !== undefined)
            .map(([k, v]) => [k, String(v)])
        ).toString()
      : '';
    return api.get<ProjectListResponse>(`/projects${qs ? `?${qs}` : ''}`, {
      signal: options?.signal,
    });
  },

  /**
   * Create a new project
   */
  async createProject(data: ProjectCreate): Promise<Project> {
    return api.post<Project>('/projects', data);
  },

  /**
   * Get a single project by ID
   */
  async getProject(projectId: string): Promise<Project> {
    return api.get<Project>(`/projects/${projectId}`);
  },

  /**
   * Update a project
   */
  async updateProject(
    projectId: string,
    data: ProjectUpdate
  ): Promise<Project> {
    return api.patch<Project>(`/projects/${projectId}`, data);
  },

  /**
   * Delete a project
   */
  async deleteProject(projectId: string): Promise<void> {
    await api.delete(`/projects/${projectId}`);
  },

  // =========================================================================
  // Project Documents (T062)
  // =========================================================================

  /**
   * List documents in a project
   */
  async listProjectDocuments(
    projectId: string,
    params?: {
      skip?: number;
      limit?: number;
    }
  ): Promise<ProjectDocumentListResponse> {
    const qs = params
      ? new URLSearchParams(
          Object.entries(params)
            .filter(([, v]) => v !== undefined)
            .map(([k, v]) => [k, String(v)])
        ).toString()
      : '';
    return api.get<ProjectDocumentListResponse>(
      `/projects/${projectId}/documents${qs ? `?${qs}` : ''}`
    );
  },

  /**
   * Add a document to a project
   */
  async addDocumentToProject(
    projectId: string,
    documentId: string
  ): Promise<ProjectDocument> {
    return api.post<ProjectDocument>(
      `/projects/${projectId}/documents?document_id=${documentId}`
    );
  },

  /**
   * Remove a document from a project
   */
  async removeDocumentFromProject(
    projectId: string,
    documentId: string
  ): Promise<void> {
    await api.delete(`/projects/${projectId}/documents/${documentId}`);
  },

  // =========================================================================
  // Project Notes (T063)
  // =========================================================================

  /**
   * List notes in a project
   */
  async listProjectNotes(
    projectId: string,
    params?: {
      skip?: number;
      limit?: number;
      pinned_only?: boolean;
    }
  ): Promise<ProjectNoteListResponse> {
    const qs = params
      ? new URLSearchParams(
          Object.entries(params)
            .filter(([, v]) => v !== undefined)
            .map(([k, v]) => [k, String(v)])
        ).toString()
      : '';
    return api.get<ProjectNoteListResponse>(
      `/projects/${projectId}/notes${qs ? `?${qs}` : ''}`
    );
  },

  /**
   * Create a note in a project
   */
  async createNote(
    projectId: string,
    data: ProjectNoteCreate
  ): Promise<ProjectNote> {
    return api.post<ProjectNote>(`/projects/${projectId}/notes`, data);
  },

  /**
   * Get a single note
   */
  async getNote(projectId: string, noteId: string): Promise<ProjectNote> {
    return api.get<ProjectNote>(`/projects/${projectId}/notes/${noteId}`);
  },

  /**
   * Update a note
   */
  async updateNote(
    projectId: string,
    noteId: string,
    data: ProjectNoteUpdate
  ): Promise<ProjectNote> {
    return api.patch<ProjectNote>(
      `/projects/${projectId}/notes/${noteId}`,
      data
    );
  },

  /**
   * Delete a note
   */
  async deleteNote(projectId: string, noteId: string): Promise<void> {
    await api.delete(`/projects/${projectId}/notes/${noteId}`);
  },

  /**
   * List a project's saved memories (durable facts the agent recalls).
   */
  async listMemories(projectId: string): Promise<ProjectMemoryListResponse> {
    return api.get<ProjectMemoryListResponse>(
      `/projects/${projectId}/memories`
    );
  },

  /**
   * Save a durable fact for a project.
   */
  async createMemory(
    projectId: string,
    content: string,
    source: string = 'manual'
  ): Promise<ProjectMemory> {
    return api.post<ProjectMemory>(`/projects/${projectId}/memories`, {
      content,
      source,
    });
  },

  /**
   * Delete a project memory.
   */
  async deleteMemory(projectId: string, memoryId: string): Promise<void> {
    await api.delete(`/projects/${projectId}/memories/${memoryId}`);
  },

  /**
   * Toggle note pinned status
   */
  async toggleNotePin(projectId: string, noteId: string): Promise<ProjectNote> {
    return api.post<ProjectNote>(`/projects/${projectId}/notes/${noteId}/pin`);
  },

  // =========================================================================
  // Project Bibliography (T064)
  // =========================================================================

  /**
   * Get project bibliography in specified format
   */
  async getProjectBibliography(
    projectId: string,
    format: 'bibtex' | 'ieee' | 'apa' | 'mla' = 'bibtex'
  ): Promise<ProjectBibliography> {
    return api.get<ProjectBibliography>(
      `/projects/${projectId}/bibliography?format=${format}`
    );
  },

  /**
   * Download project bibliography as file
   */
  async downloadBibliography(
    projectId: string,
    format: 'bibtex' | 'ieee' | 'apa' | 'mla' = 'bibtex',
    filename?: string
  ): Promise<void> {
    const bibliography = await this.getProjectBibliography(projectId, format);
    const extension = format === 'bibtex' ? 'bib' : format;

    // Create blob and download
    const blob = new Blob([bibliography.content], {
      type: format === 'bibtex' ? 'application/x-bibtex' : 'text/plain',
    });

    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download =
      filename || `${bibliography.project_name}-bibliography.${extension}`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);
  },

  // =========================================================================
  // Draft Generation (T089)
  // =========================================================================

  /**
   * Generate a literature review draft
   */
  async generateDraft(
    projectId: string,
    themes: string[],
    options?: {
      documentIds?: string[];
      style?: 'academic' | 'technical' | 'summary';
      maxSections?: number;
      includeAbstract?: boolean;
    }
  ): Promise<DraftGenerationResponse> {
    const params = new URLSearchParams();
    themes.forEach((t) => params.append('themes', t));
    if (options?.documentIds) {
      options.documentIds.forEach((id) => params.append('document_ids', id));
    }
    if (options?.style) params.append('style', options.style);
    if (options?.maxSections)
      params.append('max_sections', options.maxSections.toString());
    if (options?.includeAbstract !== undefined) {
      params.append('include_abstract', options.includeAbstract.toString());
    }

    return api.post<DraftGenerationResponse>(
      `/projects/${projectId}/drafts?${params.toString()}`
    );
  },

  /**
   * List drafts for a project
   */
  async listDrafts(
    projectId: string,
    options?: {
      includeContent?: boolean;
      skip?: number;
      limit?: number;
    }
  ): Promise<DraftListResponse> {
    const params = {
      include_content: options?.includeContent,
      skip: options?.skip,
      limit: options?.limit,
    };

    const qs = new URLSearchParams(
      Object.entries(params)
        .filter(([, v]) => v !== undefined)
        .map(([k, v]) => [k, String(v)])
    ).toString();
    return api.get<DraftListResponse>(
      `/projects/${projectId}/drafts${qs ? `?${qs}` : ''}`
    );
  },

  async listDraftReviews(projectId: string): Promise<DraftReviewListResponse> {
    return api.get<DraftReviewListResponse>(
      `/projects/${projectId}/drafts/reviews`
    );
  },

  /** GOO-306: claims whose version is pinned to this draft (read-only). */
  async listClaims(
    projectId: string,
    draftId: string
  ): Promise<ApiClaimListResponse> {
    const qs = new URLSearchParams({ draft_id: draftId }).toString();
    return api.get<ApiClaimListResponse>(`/projects/${projectId}/claims?${qs}`);
  },

  /** GOO-308: a claim over the exact code-point span of a draft version. */
  async createClaim(
    projectId: string,
    data: ApiClaimCreate
  ): Promise<ApiClaimResponse> {
    return api.post<ApiClaimResponse>(`/projects/${projectId}/claims`, data);
  },

  /** GOO-308: link the claim's tip version to evidence. */
  async linkClaimEvidence(
    projectId: string,
    claimId: string,
    data: ApiClaimLinkCreate
  ): Promise<ApiClaimLink> {
    return api.post<ApiClaimLink>(
      `/projects/${projectId}/claims/${claimId}/links`,
      data
    );
  },

  /** GOO-308: snapshot the evidence meter's stance for one link. */
  async observeClaimLink(
    projectId: string,
    claimId: string,
    linkId: string,
    idempotencyKey: string
  ): Promise<ApiStanceObservation> {
    return api.post<ApiStanceObservation>(
      `/projects/${projectId}/claims/${claimId}/links/${linkId}/observations`,
      { idempotency_key: idempotencyKey }
    );
  },

  /** GOO-308: an adjudicator's judgement of the claim's tip version. */
  async assessClaim(
    projectId: string,
    claimId: string,
    data: ApiClaimAssessmentCreate
  ): Promise<ApiClaimAssessment> {
    return api.post<ApiClaimAssessment>(
      `/projects/${projectId}/claims/${claimId}/assessments`,
      data
    );
  },

  /** GOO-306: the reconstructable claims evidence package for one draft. */
  async downloadClaimsExport(
    projectId: string,
    draftId: string
  ): Promise<void> {
    const qs = new URLSearchParams({ draft_id: draftId }).toString();
    // The server names the file after the body hash.
    await api.download(`/projects/${projectId}/claims/export?${qs}`);
  },

  /** GOO-307: release status, blockers and invalidation of one version. */
  async getDraftRelease(
    projectId: string,
    draftId: string,
    version: number
  ): Promise<ApiReleaseCheck> {
    return api.get<ApiReleaseCheck>(
      `/projects/${projectId}/drafts/${draftId}/versions/${version}/release`
    );
  },

  /** GOO-307: promote this exact version (adjudicator or supervisor). */
  async promoteDraft(
    projectId: string,
    draftId: string,
    version: number,
    body: ApiDraftPromoteRequest
  ): Promise<ApiDraftRelease> {
    return api.post<ApiDraftRelease>(
      `/projects/${projectId}/drafts/${draftId}/versions/${version}/promote`,
      body
    );
  },

  /**
   * Get the current draft
   */
  async getCurrentDraft(projectId: string): Promise<Draft> {
    return api.get<Draft>(`/projects/${projectId}/drafts/current`);
  },

  /**
   * Get a specific draft
   */
  async getDraft(projectId: string, draftId: string): Promise<Draft> {
    return api.get<Draft>(`/projects/${projectId}/drafts/${draftId}`);
  },

  /**
   * Delete a draft
   */
  async deleteDraft(projectId: string, draftId: string): Promise<void> {
    await api.delete(`/projects/${projectId}/drafts/${draftId}`);
  },

  /**
   * Get draft citations
   */
  async getDraftCitations(
    projectId: string,
    draftId: string
  ): Promise<DraftCitationsResponse> {
    return api.get<DraftCitationsResponse>(
      `/projects/${projectId}/drafts/${draftId}/citations`
    );
  },

  /**
   * Compare two draft versions
   */
  async compareDrafts(
    projectId: string,
    versionA: number,
    versionB: number
  ): Promise<DraftComparison> {
    return api.get<DraftComparison>(
      `/projects/${projectId}/drafts/compare?version_a=${versionA}&version_b=${versionB}`
    );
  },

  /** GOO-314: sentence-level anchored diff between two saved versions. */
  async diffDrafts(
    projectId: string,
    fromDraftId: string,
    toDraftId: string
  ): Promise<ApiDraftDiff> {
    const qs = new URLSearchParams({
      from_draft_id: fromDraftId,
      to_draft_id: toDraftId,
    }).toString();
    return api.get<ApiDraftDiff>(`/projects/${projectId}/drafts/diff?${qs}`);
  },

  /** GOO-314: the project's external peer-review rounds. */
  async listPeerReviewRounds(
    projectId: string
  ): Promise<ApiPeerReviewRoundList> {
    return api.get<ApiPeerReviewRoundList>(
      `/projects/${projectId}/peer-review/rounds`
    );
  },

  /** GOO-314: one round, anchors checked against the target version. */
  async getPeerReviewRound(
    projectId: string,
    roundId: string,
    targetDraftId?: string
  ): Promise<ApiPeerReviewRoundDetail> {
    const qs = targetDraftId
      ? `?${new URLSearchParams({ target_draft_id: targetDraftId })}`
      : '';
    return api.get<ApiPeerReviewRoundDetail>(
      `/projects/${projectId}/peer-review/rounds/${roundId}${qs}`
    );
  },

  /** GOO-314: a response version (409 when the tip moved). */
  async respondToPeerReviewComment(
    projectId: string,
    commentRootId: string,
    data: ApiPeerReviewResponseCreate
  ): Promise<ApiPeerReviewResponse> {
    return api.post<ApiPeerReviewResponse>(
      `/projects/${projectId}/peer-review/comments/${commentRootId}/responses`,
      data
    );
  },

  /** GOO-314: assign (EDIT) or resolve/reopen (ADJUDICATE) a comment. */
  async decidePeerReviewComment(
    projectId: string,
    commentRootId: string,
    data: ApiPeerReviewDecisionCreate
  ): Promise<ApiPeerReviewDecision> {
    return api.post<ApiPeerReviewDecision>(
      `/projects/${projectId}/peer-review/comments/${commentRootId}/decisions`,
      data
    );
  },

  /** GOO-314: the response export (Markdown or JSON attachment). */
  async downloadPeerReviewExport(
    projectId: string,
    roundId: string,
    format: 'markdown' | 'json'
  ): Promise<void> {
    await api.download(
      `/projects/${projectId}/peer-review/rounds/${roundId}/export?format=${format}`
    );
  },

  /**
   * Export draft to file format
   */
  async exportDraft(
    projectId: string,
    draftId: string,
    format: 'markdown' | 'latex' = 'markdown',
    includeBibliography: boolean = true,
    bibliographyFormat: 'bibtex' | 'biblatex' = 'bibtex'
  ): Promise<void> {
    await this.downloadDraftExport(
      projectId,
      draftId,
      format,
      includeBibliography,
      bibliographyFormat
    );
  },

  /**
   * Download draft export as file (markdown or latex zip)
   */
  async downloadDraftExport(
    projectId: string,
    draftId: string,
    format: 'markdown' | 'latex' = 'markdown',
    includeBibliography: boolean = true,
    bibliographyFormat: 'bibtex' | 'biblatex' = 'bibtex'
  ): Promise<void> {
    const qs = new URLSearchParams({
      format,
      include_bibliography: String(includeBibliography),
      bib_format: bibliographyFormat,
    }).toString();
    await api.download(
      `/projects/${projectId}/drafts/${draftId}/export?${qs}`,
      format === 'latex' ? 'draft.zip' : 'draft.md'
    );
  },

  /**
   * Get generation status
   */
  async getGenerationStatus(
    projectId: string,
    taskId?: string
  ): Promise<GenerationStatus> {
    const params = taskId ? { task_id: taskId } : undefined;
    const qs = params
      ? new URLSearchParams(
          Object.entries(params)
            .filter(([, v]) => v !== undefined)
            .map(([k, v]) => [k, String(v)])
        ).toString()
      : '';
    return api.get<GenerationStatus>(
      `/projects/${projectId}/drafts/status${qs ? `?${qs}` : ''}`
    );
  },

  /**
   * Cancel ongoing generation
   */
  async cancelGeneration(projectId: string, taskId?: string): Promise<void> {
    const params = taskId ? { task_id: taskId } : undefined;
    const cancelQs = params
      ? new URLSearchParams(
          Object.entries(params)
            .filter(([, v]) => v !== undefined)
            .map(([k, v]) => [k, String(v)])
        ).toString()
      : '';
    await api.post(
      `/projects/${projectId}/drafts/cancel${cancelQs ? `?${cancelQs}` : ''}`
    );
  },
};

// Draft types
export interface Draft {
  id: string;
  project_id: string;
  version: number;
  title: string;
  content: string;
  themes: string[];
  word_count: number;
  citation_count: number;
  generation_params?: Record<string, unknown>;
  is_current: boolean;
  created_at: string;
  // GOO-307: the draft routes return untyped dicts, so these stay hand-written.
  release_status?: ApiReleaseStatus;
  content_hash?: string;
}

export interface DraftListResponse {
  drafts: Omit<Draft, 'content'>[];
  total: number;
  skip: number;
  limit: number;
}

export interface DraftGenerationResponse {
  task_id: string;
  status: string;
  message: string;
}

type GeneratedDraftReview = components['schemas']['DraftReviewResponse'];
type GeneratedDraftReviewPayload = components['schemas']['DraftReviewPayload'];
export type DraftReview = Omit<GeneratedDraftReview, 'outcome' | 'review'> & {
  outcome: 'passed' | 'blocked';
  review: Omit<
    GeneratedDraftReviewPayload,
    'uncited_assertions' | 'verdicts'
  > & {
    uncited_assertions?: Array<{ text?: string }>;
    verdicts?: Array<{
      verdict?: string;
      evidence?: string;
      checks?: {
        identity?: { status?: string; available?: boolean };
        support?: { status?: string; available?: boolean };
        publication?: {
          status?: string;
          available?: boolean;
          performed?: boolean;
          observation_status?: string;
          observations?: Array<{
            field?: string;
            value?: unknown;
            source?: string;
          }>;
        };
      };
    }>;
  };
};
export type DraftReviewListResponse = Omit<
  components['schemas']['DraftReviewListResponse'],
  'reviews'
> & { reviews: DraftReview[] };

export type GenerationStatus = components['schemas']['DraftTaskStatusResponse'];

export interface DraftCitation {
  id: string;
  citation_index: number;
  document_id?: string;
  citation_id?: string;
  snippet: string;
  context: string;
}

export interface DraftCitationsResponse {
  citations: DraftCitation[];
  total: number;
}

export interface DraftComparison {
  version_a: {
    version: number;
    word_count: number;
    citation_count: number;
    created_at: string;
  };
  version_b: {
    version: number;
    word_count: number;
    citation_count: number;
    created_at: string;
  };
  word_count_diff: number;
  citation_count_diff: number;
  similarity_score: number;
}

export interface DraftExportResponse {
  format: string;
  filename?: string;
  content?: string;
  mime_type?: string;
  files?: Array<{
    filename: string;
    content: string;
    mime_type: string;
  }>;
}

export default projectService;
