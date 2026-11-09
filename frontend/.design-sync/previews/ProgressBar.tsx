import { ProgressBar } from '@nous/design-system';

// Ported from ArxivManagement's ingestion tracker.
export const IngestionProgress = () => (
  <div style={{ maxWidth: 420 }}>
    <ProgressBar value={62} label="Ingesting 18 of 29 arXiv papers" />
  </div>
);

export const Stages = () => (
  <div className="grid gap-4" style={{ maxWidth: 420 }}>
    <ProgressBar value={8} label="Fetching metadata" />
    <ProgressBar value={47} label="Downloading PDFs" />
    <ProgressBar value={100} label="Indexed" />
  </div>
);
