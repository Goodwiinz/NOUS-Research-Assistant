import { NousNav } from '@nous/design-system';

const items = [
  { label: 'Workspace', href: '#workspace' },
  { label: 'Agents', href: '#agents' },
  { label: 'Sources', href: '#sources' },
  { label: 'Graph', href: '#graph' },
];

export const WorkspaceActive = () => (
  <NousNav items={items} activeHref="#workspace" user="Maya Chen" />
);

export const SourcesActive = () => (
  <NousNav items={items} activeHref="#sources" user="Arjun Patel" />
);
