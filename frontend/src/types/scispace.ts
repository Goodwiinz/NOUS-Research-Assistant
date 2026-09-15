/**
 * TypeScript types for SciSpace integration features.
 * Mirrors backend schemas from backend/src/shared/scispace_schemas.py
 */

// ============================================================================
// Feature 1: Extraction Matrix
// ============================================================================

export interface ExtractionColumn {
  name: string;
  description?: string;
}

export interface ExtractionCell {
  document_id: string;
  column_name: string;
  value: string | null;
  citation_snippet: string | null;
  confidence: number | null;
}

export interface ExtractionMatrix {
  id: string;
  project_id: string;
  name: string;
  columns: ExtractionColumn[];
  cells: ExtractionCell[];
  created_at: string | null;
  updated_at: string | null;
}

export interface CreateMatrixRequest {
  name: string;
  columns: ExtractionColumn[];
}

export interface TriggerExtractionRequest {
  document_ids: string[];
}

export interface UpdateMatrixRequest {
  name?: string;
  columns?: ExtractionColumn[];
  clear_stale_cells?: boolean;
}

export interface UpdateMatrixResponse {
  id: string;
  project_id: string;
  name: string;
  columns: ExtractionColumn[];
  columns_changed: boolean;
  stale_document_ids: string[];
  updated_at: string | null;
}

export interface CreateMatrixResponse {
  id: string;
  project_id: string;
  name: string;
  columns: ExtractionColumn[];
  created_at: string;
  extraction_task_id: string | null;
}

export interface TriggerExtractionResponse {
  matrix_id: string;
  document_ids: string[];
  status: string;
  message: string;
}

export interface ExtractionTaskStatus {
  status: 'running' | 'completed' | 'failed';
  matrix_id: string;
  total: number;
  completed: number;
  failed: number;
  skipped: number;
  error?: string;
}

// ============================================================================
// Feature 2: Table/Math Extraction
// ============================================================================

export interface ExtractedTable {
  page: number;
  table_index: number;
  rows: string[][];
  markdown: string;
  method: string;
}

export interface TablesResponse {
  document_id: string;
  table_count: number;
  tables: ExtractedTable[];
}

export interface ExtractRegionRequest {
  page: number;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface ExtractRegionResponse {
  document_id: string;
  page: number;
  region: { x1: number; y1: number; x2: number; y2: number };
  format: 'csv' | 'latex' | 'markdown';
  content: string;
  confidence: number;
}

// ============================================================================
// Feature 3: Tone Engine
// ============================================================================

export type ToneOption = 'academic' | 'simplified' | 'concise' | 'expanded';

export interface RewriteRequest {
  text: string;
  tone: ToneOption;
  preserve_citations?: boolean;
  model?: string;
}

export interface RewriteResponse {
  original: string;
  rewritten: string;
  tone_applied: ToneOption;
  citations_preserved: string[];
}

// ============================================================================
// Feature 4: Source Connectors
// ============================================================================

export type SourceConnectorType =
  'arxiv' | 'semantic_scholar' | 'openalex' | 'crossref' | 'pubmed';

export interface SourceConnectorOption {
  id: SourceConnectorType;
  label: string;
  description: string;
  icon: string;
}

// ============================================================================
// Feature 5: Integrity Detector
// ============================================================================

export interface IntegritySegmentScore {
  text_preview: string;
  ai_probability: number;
}

export interface IntegrityScoreResponse {
  document_id: string;
  ai_probability: number;
  human_probability: number;
  method: string;
  analyzed_at: string | null;
  segment_scores: IntegritySegmentScore[];
}

export interface IntegrityCheckResponse {
  document_id: string;
  status: string;
  ai_probability: number;
  human_probability: number;
  method: string;
  analyzed_at: string;
}

export type IntegrityLevel = 'human' | 'mixed' | 'ai';

export function getIntegrityLevel(aiProbability: number): IntegrityLevel {
  if (aiProbability < 0.3) return 'human';
  if (aiProbability <= 0.7) return 'mixed';
  return 'ai';
}

// ============================================================================
// Feature 6: AI Writer
// ============================================================================

export type WriterAction = 'complete' | 'generate_section' | 'generate_outline';

export type SectionType =
  | 'introduction'
  | 'methodology'
  | 'results'
  | 'discussion'
  | 'conclusion'
  | 'abstract'
  | 'custom';

export interface WriteRequest {
  action: WriterAction;
  cursor_context: string;
  section_type?: SectionType;
  style?: 'academic' | 'technical' | 'summary';
  document_ids?: string[];
}

export interface WriteResponse {
  generated: string;
  action: WriterAction;
  section_type: SectionType | null;
  citations_used: string[];
  confidence: number;
}

export interface OutlineSection {
  title: string;
  section_type: SectionType;
  description: string;
  suggested_word_count: number;
}

export interface OutlineRequest {
  research_question: string;
  style?: 'academic' | 'technical' | 'summary';
  document_ids?: string[];
  section_types?: SectionType[];
}

export interface OutlineResponse {
  research_question: string;
  sections: OutlineSection[];
  style: 'academic' | 'technical' | 'summary';
  total_suggested_words: number;
}

// ============================================================================
// Feature 7: Research Pipeline
// ============================================================================

export type PipelineStepStatus =
  'completed' | 'active' | 'skipped' | 'upcoming' | 'invalidated';

export interface PipelineStep {
  index: number;
  label: string;
  skippable: boolean;
}

export const PIPELINE_STEPS: PipelineStep[] = [
  { index: 0, label: 'Collect', skippable: false },
  { index: 1, label: 'Extract', skippable: true },
  { index: 2, label: 'Cite', skippable: false },
  { index: 3, label: 'Draft', skippable: false },
  { index: 4, label: 'Export', skippable: false },
];

export interface PipelineState {
  id: string;
  project_id: string;
  current_step: number;
  completed_steps: number[];
  skipped_steps: number[];
  step_data: Record<string, unknown>;
  invalidated_steps: number[];
  created_at: string | null;
  updated_at: string | null;
}

export interface UpdatePipelineRequest {
  current_step?: number;
  completed_steps?: number[];
  skipped_steps?: number[];
  step_data?: Record<string, unknown>;
  invalidated_steps?: number[];
}
