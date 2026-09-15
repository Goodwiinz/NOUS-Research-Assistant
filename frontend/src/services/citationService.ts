/**
 * Citation Service
 * API client for Research Assistant citation endpoints
 */

import { api } from '@/services/api-client';
import type {
  CitationCreate,
  CitationResponse,
  CitationListResponse,
} from '@/types/research';
import type {
  ApiCitationCreateInput,
  ApiCitationExtractRequest,
  ApiCitationListResponse,
  ApiCitationResponse,
} from '@/types/api/citation-contract';

const normalizeCitationResponse = (
  citation: ApiCitationResponse
): CitationResponse => ({
  id: citation.id,
  messageId: citation.message_id,
  documentId: citation.document_id,
  externalReferenceId: citation.external_reference_id,
  documentTitle: citation.document_title,
  documentType: citation.document_type,
  authors: citation.authors ?? null,
  year: citation.year,
  venue: citation.venue,
  doi: citation.doi,
  arxivId: citation.arxiv_id,
  abstract: citation.abstract,
  snippet: citation.snippet,
  pageNumber: citation.page_number,
  chunkId: citation.chunk_id,
  chunkIndex: citation.chunk_index,
  rerankScore: citation.rerank_score,
  score: citation.score,
  metadataSource: citation.metadata_source,
  needsReview: citation.needs_review,
  createdAt: citation.created_at,
  updatedAt: citation.updated_at,
});

const normalizeCitationListResponse = (
  response: ApiCitationListResponse
): CitationListResponse => ({
  ...response,
  citations: response.citations.map(normalizeCitationResponse),
});

export const citationService = {
  /**
   * Create a new citation from chat message context
   */
  async createCitation(data: CitationCreate): Promise<CitationResponse> {
    // Transform camelCase to snake_case for backend API
    const apiData: ApiCitationCreateInput = {};
    if (data.messageId) apiData.message_id = data.messageId;
    if (data.documentId) apiData.document_id = data.documentId;
    if (data.externalReferenceId)
      apiData.external_reference_id = data.externalReferenceId;
    if (data.documentTitle) apiData.document_title = data.documentTitle;
    if (data.documentType) apiData.document_type = data.documentType;
    if (data.authors) apiData.authors = data.authors;
    if (data.year) apiData.year = data.year;
    if (data.venue) apiData.venue = data.venue;
    if (data.doi) apiData.doi = data.doi;
    if (data.arxivId) apiData.arxiv_id = data.arxivId;
    if (data.abstract) apiData.abstract = data.abstract;
    if (data.snippet) apiData.snippet = data.snippet;
    if (data.pageNumber) apiData.page_number = data.pageNumber;
    if (data.score !== undefined) apiData.score = data.score;
    if (data.metadataSource) apiData.metadata_source = data.metadataSource;
    if (data.needsReview !== undefined) apiData.needs_review = data.needsReview;

    const response = await api.post<ApiCitationResponse>('/citations', apiData);
    return normalizeCitationResponse(response);
  },

  /**
   * Get a single citation by ID
   */
  async getCitation(citationId: string): Promise<CitationResponse> {
    const response = await api.get<ApiCitationResponse>(
      `/citations/${citationId}`
    );
    return normalizeCitationResponse(response);
  },

  /**
   * List citations with optional filters
   */
  async listCitations(params?: {
    messageId?: string;
    documentId?: string;
    arxivId?: string;
    doi?: string;
    needsReview?: boolean;
    skip?: number;
    limit?: number;
  }): Promise<CitationListResponse> {
    // Transform camelCase params to snake_case for backend API
    const apiParams: Record<string, any> = {};
    if (params?.messageId) apiParams.message_id = params.messageId;
    if (params?.documentId) apiParams.document_id = params.documentId;
    if (params?.arxivId) apiParams.arxiv_id = params.arxivId;
    if (params?.doi) apiParams.doi = params.doi;
    if (params?.needsReview !== undefined)
      apiParams.needs_review = params.needsReview;
    if (params?.skip !== undefined) apiParams.skip = params.skip;
    if (params?.limit !== undefined) apiParams.limit = params.limit;

    const queryString = new URLSearchParams(
      Object.entries(apiParams)
        .filter(([, v]) => v !== undefined)
        .map(([k, v]) => [k, String(v)])
    ).toString();
    const response = await api.get<ApiCitationListResponse>(
      `/citations${queryString ? `?${queryString}` : ''}`
    );
    return normalizeCitationListResponse(response);
  },

  /**
   * Fetch citations for a specific message
   */
  async getCitationsForMessage(messageId: string): Promise<CitationResponse[]> {
    const response = await this.listCitations({ messageId });
    return response.citations;
  },

  /**
   * Fetch citations for a specific document
   */
  async getCitationsForDocument(
    documentId: string
  ): Promise<CitationResponse[]> {
    const response = await this.listCitations({ documentId });
    return response.citations;
  },

  /**
   * Get citations that need review (incomplete metadata)
   */
  async getCitationsNeedingReview(): Promise<CitationResponse[]> {
    const response = await this.listCitations({ needsReview: true });
    return response.citations;
  },

  /**
   * Extract citation metadata using hybrid extraction pipeline
   * @param documentId - Document ID to extract from
   * @param strategy - Extraction strategy (auto/arxiv/semantic_scholar/crossref)
   */
  async extractCitations(
    documentId?: string,
    strategy: string = 'auto'
  ): Promise<CitationResponse> {
    const apiData: ApiCitationExtractRequest = {
      document_id: documentId,
      strategy,
    };
    const response = await api.post<ApiCitationResponse>(
      '/citations/extract',
      apiData
    );
    return normalizeCitationResponse(response);
  },

  /**
   * Lookup citation by ArXiv ID, DOI, or title
   * Returns citation metadata without persisting to database
   */
  async lookupCitation(params: {
    arxivId?: string;
    doi?: string;
    title?: string;
  }): Promise<CitationResponse> {
    // Backend expects query parameters, not body
    const queryParams: Record<string, string> = {};
    if (params.arxivId) queryParams.arxiv_id = params.arxivId;
    if (params.doi) queryParams.doi = params.doi;
    if (params.title) queryParams.title = params.title;

    const qs = new URLSearchParams(queryParams).toString();
    const response = await api.post<ApiCitationResponse>(
      `/citations/lookup${qs ? `?${qs}` : ''}`
    );
    return normalizeCitationResponse(response);
  },

  /**
   * Export bibliography in specified format
   * @param format - Bibliography format (bibtex/ieee/apa/mla)
   * @param citationIds - Optional list of citation IDs to export
   * @param projectId - Optional project ID to export all citations from
   * @returns Formatted bibliography as text
   */
  async exportBibliography(
    format: 'bibtex' | 'ieee' | 'apa' | 'mla',
    citationIds?: string[],
    projectId?: string
  ): Promise<string> {
    return api.post<string>('/citations/export', {
      format,
      citation_ids: citationIds,
      project_id: projectId,
    });
  },

  /**
   * Download bibliography as a file
   * @param format - Bibliography format
   * @param citationIds - Optional list of citation IDs
   * @param projectId - Optional project ID
   * @param filename - Optional custom filename
   */
  async downloadBibliography(
    format: 'bibtex' | 'ieee' | 'apa' | 'mla',
    citationIds?: string[],
    projectId?: string,
    filename?: string
  ): Promise<void> {
    const bibliography = await this.exportBibliography(
      format,
      citationIds,
      projectId
    );
    const extension = format === 'bibtex' ? 'bib' : format;

    // Create blob and download
    const blob = new Blob([bibliography], {
      type: format === 'bibtex' ? 'application/x-bibtex' : 'text/plain',
    });

    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename || `bibliography.${extension}`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);
  },

  // =========================================================================
  // Citation Graph Methods (T058)
  // =========================================================================

  /**
   * Get citation graph data for visualization
   * @param projectId - Optional project filter
   * @param documentId - Optional document filter
   * @param depth - Graph traversal depth (1-5)
   * @param includeExternal - Include external papers
   */
  async getCitationGraph(params?: {
    projectId?: string;
    documentId?: string;
    depth?: number;
    includeExternal?: boolean;
  }): Promise<GraphData> {
    const qs = new URLSearchParams(
      Object.entries({
        project_id: params?.projectId,
        document_id: params?.documentId,
        depth: String(params?.depth || 2),
        include_external: String(params?.includeExternal ?? true),
      }).filter(
        (entry): entry is [string, string] =>
          entry[1] !== undefined && entry[1] !== 'undefined'
      )
    ).toString();
    return api.get<GraphData>(`/citations/graph${qs ? `?${qs}` : ''}`);
  },

  /**
   * Get detailed information about a specific graph node
   * @param citationId - Citation ID to get details for
   */
  async getGraphNodeDetails(citationId: string): Promise<GraphNodeDetails> {
    return api.get<GraphNodeDetails>(`/citations/graph/node/${citationId}`);
  },

  /**
   * List citation relationships
   * @param sourceId - Optional source citation filter
   * @param targetId - Optional target citation filter
   */
  async listRelationships(params?: {
    sourceId?: string;
    targetId?: string;
    relationshipType?: string;
  }): Promise<RelationshipsResponse> {
    const qs = new URLSearchParams(
      Object.entries({
        source_id: params?.sourceId,
        target_id: params?.targetId,
        relationship_type: params?.relationshipType,
      }).filter(([, v]) => v !== undefined) as [string, string][]
    ).toString();
    return api.get<RelationshipsResponse>(
      `/citations/relationships${qs ? `?${qs}` : ''}`
    );
  },

  /**
   * Create a citation relationship
   * @param sourceCitationId - The citing paper
   * @param targetCitationId - The cited paper
   * @param relationshipType - Type of relationship
   * @param citationContext - Optional text context
   * @param confidence - Confidence score
   */
  async createRelationship(params: {
    sourceCitationId: string;
    targetCitationId: string;
    relationshipType?: string;
    citationContext?: string;
    confidence?: number;
  }): Promise<RelationshipResponse> {
    const qs = new URLSearchParams(
      Object.entries({
        source_citation_id: params.sourceCitationId,
        target_citation_id: params.targetCitationId,
        relationship_type: params.relationshipType || 'CITES',
        citation_context: params.citationContext,
        confidence: String(params.confidence || 1.0),
      }).filter(([, v]) => v !== undefined) as [string, string][]
    ).toString();
    return api.post<RelationshipResponse>(
      `/citations/relationships${qs ? `?${qs}` : ''}`
    );
  },
};

// Graph-related types
export interface GraphNode {
  id: string;
  title?: string;
  authors?: string[];
  year?: number;
  venue?: string;
  doi?: string;
  arxiv_id?: string;
  document_id?: string;
  is_uploaded?: boolean;
  citation_count?: number;
  position?: { x: number; y: number } | null;
  influence_score?: number;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type?: string;
  confidence?: number;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  metadata?: {
    total_nodes: number;
    total_edges: number;
    depth: number;
    include_external: boolean;
  };
}

export interface GraphNodeDetails {
  id: string;
  document_id?: string;
  title?: string;
  authors?: string[];
  year?: number;
  venue?: string;
  doi?: string;
  arxiv_id?: string;
  is_uploaded?: boolean;
  cited_by: number;
  cites: number;
  influence_score: number;
}

export interface RelationshipResponse {
  id: string;
  source_citation_id: string;
  target_citation_id: string;
  relationship_type: string;
  citation_context?: string;
  confidence: number;
  created_at?: string;
}

export interface RelationshipsResponse {
  relationships: RelationshipResponse[];
  total: number;
}

export default citationService;
