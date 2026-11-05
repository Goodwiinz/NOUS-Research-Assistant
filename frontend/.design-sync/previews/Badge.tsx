import { Badge } from '@nous/design-system';
import { DarkSurface } from './_DarkSurface';

export const Variants = () => (
  <div className="flex flex-wrap items-center gap-2">
    <Badge>Default</Badge>
    <Badge variant="secondary">Literature review</Badge>
    <Badge variant="outline">arXiv</Badge>
    <Badge variant="accent">Featured</Badge>
    <Badge variant="muted">Draft</Badge>
    <Badge variant="success">Indexed</Badge>
    <Badge variant="warning">Processing</Badge>
    <Badge variant="info">Queued</Badge>
    <Badge variant="destructive">Failed</Badge>
  </div>
);

export const DocumentStatus = () => (
  <div className="flex flex-wrap items-center gap-2">
    <Badge variant="success" className="text-[10px]">Indexed</Badge>
    <Badge variant="warning" className="text-[10px]">Processing</Badge>
    <Badge variant="info" className="text-[10px]">Queued</Badge>
    <Badge variant="destructive" className="text-[10px]">Failed</Badge>
  </div>
);

export const InContext = () => (
  <div className="flex items-center gap-2">
    <span className="text-sm font-medium">Retrieval-augmented generation survey</span>
    <Badge variant="outline" className="text-xs">12 sources</Badge>
    <Badge variant="warning">Pending approval</Badge>
  </div>
);

// The same status set on the dark theme, the live app's default.
export const Dark = () => (
  <DarkSurface>
    <DocumentStatus />
  </DarkSurface>
);
