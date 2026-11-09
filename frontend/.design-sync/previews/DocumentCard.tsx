import { DocumentCard } from '@nous/design-system';
import { DarkSurface } from './_DarkSurface';

// DocumentCard is one row in the document library grid. The select checkbox
// and the "More options" menu reveal on hover; queued/processing/failed
// documents also show the compact ProcessingStatus strip underneath.

const base = {
  user_id: 'user-7f3a',
  organization_id: 'org-nous-lab',
  metadata: {},
};

const flashAttention = {
  ...base,
  id: 'doc-flash-attn',
  title: 'FlashAttention: Fast and Memory-Efficient Exact Attention',
  filename: 'dao2022_flash_attention.pdf',
  file_type: 'pdf' as const,
  file_size: 2_457_600,
  page_count: 34,
  upload_timestamp: '2026-10-28T09:14:00Z',
  processing_status: 'indexed' as const,
};

const longformer = {
  ...base,
  id: 'doc-longformer',
  title: 'Longformer: The Long-Document Transformer',
  filename: 'beltagy2020_longformer.pdf',
  file_type: 'pdf' as const,
  file_size: 1_198_080,
  page_count: 17,
  upload_timestamp: '2026-11-03T14:02:00Z',
  processing_status: 'processing' as const,
  processing_started_at: '2026-11-03T14:02:05Z',
};

const lectureAudio = {
  ...base,
  id: 'doc-lecture-audio',
  title: 'Stanford CS25: Efficient Transformers lecture',
  filename: 'cs25_efficient_transformers.mp3',
  file_type: 'mp3' as const,
  file_size: 48_234_496,
  duration_seconds: 4_215,
  upload_timestamp: '2026-11-02T18:40:00Z',
  processing_status: 'failed' as const,
  processing_error: 'Transcription timed out after 3 retries',
  processing_retry_count: 3,
};

const readingNotes = {
  ...base,
  id: 'doc-reading-notes',
  title: 'Reading notes: sparse attention survey',
  filename: 'sparse_attention_notes.txt',
  file_type: 'txt' as const,
  file_size: 18_432,
  upload_timestamp: '2026-11-01T11:20:00Z',
  processing_status: 'queued' as const,
};

export const Indexed = () => (
  <div style={{ maxWidth: 600 }}>
    <DocumentCard document={flashAttention} onPreview={() => {}} onDelete={() => {}} />
  </div>
);

export const Selected = () => (
  <div style={{ maxWidth: 600 }}>
    <DocumentCard document={flashAttention} selected onSelect={() => {}} />
  </div>
);

export const Processing = () => (
  <div style={{ maxWidth: 600 }}>
    <DocumentCard document={longformer} />
  </div>
);

export const Failed = () => (
  <div style={{ maxWidth: 600 }}>
    <DocumentCard document={lectureAudio} onRetry={() => {}} />
  </div>
);

export const LibraryGrid = () => (
  <div className="grid grid-cols-2 gap-3" style={{ maxWidth: 760 }}>
    <DocumentCard document={flashAttention} />
    <DocumentCard document={readingNotes} showProcessingStatus={false} />
    <DocumentCard document={{ ...longformer, processing_status: 'indexed' }} />
    <DocumentCard document={{ ...lectureAudio, processing_status: 'indexed', processing_error: undefined }} />
  </div>
);

// The same document card on the dark theme, the live app's default.
export const Dark = () => (
  <DarkSurface>
    <Indexed />
  </DarkSurface>
);
