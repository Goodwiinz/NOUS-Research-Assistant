/**
 * TypeScript types for Research Assistant feature
 */

import type {
  ApiCitationListResponse,
  ApiCitationResponse,
} from './api/citation-contract';

// ============================================================================
// Citation Types
// ============================================================================

export interface CitationCreate {
  messageId?: string;
  documentId?: string;
  externalReferenceId?: string;
  documentTitle: string;
  documentType?: string;
  authors?: string[];
  year?: number;
  venue?: string;
  doi?: string;
  arxivId?: string;
  abstract?: string;
  snippet?: string;
  pageNumber?: number;
  score?: number;
  metadataSource?: 'manual' | 'arxiv' | 'crossref' | 'semantic_scholar' | 'rag';
  needsReview?: boolean;
}

/** CamelCase presentation model derived from the generated wire response. */
export type CitationResponse = {
  id: ApiCitationResponse['id'];
  messageId: ApiCitationResponse['message_id'];
  documentId: ApiCitationResponse['document_id'];
  externalReferenceId: ApiCitationResponse['external_reference_id'];
  documentTitle: ApiCitationResponse['document_title'];
  documentType: ApiCitationResponse['document_type'];
  authors: Exclude<ApiCitationResponse['authors'], undefined>;
  year: ApiCitationResponse['year'];
  venue: ApiCitationResponse['venue'];
  doi: ApiCitationResponse['doi'];
  arxivId: ApiCitationResponse['arxiv_id'];
  abstract: ApiCitationResponse['abstract'];
  snippet: ApiCitationResponse['snippet'];
  pageNumber: ApiCitationResponse['page_number'];
  chunkId: ApiCitationResponse['chunk_id'];
  chunkIndex: ApiCitationResponse['chunk_index'];
  rerankScore: ApiCitationResponse['rerank_score'];
  score: ApiCitationResponse['score'];
  metadataSource: ApiCitationResponse['metadata_source'];
  needsReview: ApiCitationResponse['needs_review'];
  createdAt: ApiCitationResponse['created_at'];
  updatedAt: ApiCitationResponse['updated_at'];
};

export type CitationListResponse = Omit<
  ApiCitationListResponse,
  'citations'
> & {
  citations: CitationResponse[];
};

// ============================================================================
// Project Types
// ============================================================================

export interface ProjectCreate {
  name: string;
  description?: string;
  projectType?: 'research' | 'literature_review' | 'thesis' | 'paper';
  researchStatus?: 'active' | 'paused' | 'completed' | 'archived';
  researchGoals?: string;
  deadline?: string;
  tags?: string[];
}

export interface ProjectResponse {
  id: string;
  userId: string;
  workspace_id: string;
  name: string;
  description?: string;
  projectType: string;
  researchStatus: string;
  researchGoals?: string;
  deadline?: string;
  tags: string[];
  isPrivate: boolean;
  createdAt: string;
  updatedAt: string;
}

// ============================================================================
// Note Types
// ============================================================================

export interface NoteCreate {
  projectId: string;
  title: string;
  content: string;
  linkedDocumentIds?: string[];
  tags?: string[];
  isPinned?: boolean;
}

export interface NoteResponse {
  id: string;
  projectId: string;
  userId: string;
  title: string;
  content: string;
  linkedDocumentIds: string[];
  tags: string[];
  isPinned: boolean;
  createdAt: string;
  updatedAt: string;
}

// ============================================================================
// Draft Types
// ============================================================================

export interface DraftGenerateRequest {
  themes: string[];
  documentIds?: string[];
  style?: 'academic' | 'technical' | 'summary';
  maxSections?: number;
  includeAbstract?: boolean;
}

export interface DraftResponse {
  id: string;
  projectId: string;
  version: number;
  title: string;
  content: string;
  themes: string[];
  wordCount?: number;
  citationCount?: number;
  generationParams: Record<string, any>;
  generationTimeMs?: number;
  isCurrent: boolean;
  createdAt: string;
}

export interface DraftComparisonResponse {
  versionA: DraftResponse;
  versionB: DraftResponse;
  wordCountDiff: number;
  similarityScore: number;
}
