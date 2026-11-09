import { EmptyState } from '@nous/design-system';
import { DarkSurface } from './_DarkSurface';
import { AlertCircle, FileSearch, FolderOpen } from 'lucide-react';

export const NoProjects = () => (
  <EmptyState
    icon={FolderOpen}
    title="No projects yet"
    description="A project holds the papers, threads and notes for one research question."
    action={{ label: 'New project', onClick: () => {} }}
  />
);

export const NoResults = () => (
  <EmptyState
    icon={FileSearch}
    title="No matching passages"
    description="Nothing in this project mentions “mixture of experts”. Try a broader query or add more sources."
  />
);

export const ServiceError = () => (
  <EmptyState
    icon={AlertCircle}
    title="Search is unavailable"
    description="NOUS can't reach the search service right now. Your projects and documents are safe. Try again in a minute."
    action={{ label: 'Try again', onClick: () => {} }}
  />
);

// The same empty state on the dark theme, the live app's default.
export const Dark = () => (
  <DarkSurface>
    <NoResults />
  </DarkSurface>
);
