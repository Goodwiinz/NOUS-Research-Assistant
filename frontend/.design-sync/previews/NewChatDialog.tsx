import { NewChatDialog } from '@nous/design-system';

const researchAgents = [
  {
    id: 'research',
    name: 'Research Agent',
    description: 'Searches arXiv and your library, then answers with inline citations',
    avatar: '',
    category: 'general',
    capabilities: ['Literature review', 'Citation tracing', 'Paper discovery', 'Summaries'],
    color: 'from-(--nous-helios) to-(--nous-sol)',
  },
  {
    id: 'writing',
    name: 'Writing Agent',
    description: 'Drafts related-work sections and revises manuscripts against your sources',
    avatar: '',
    category: 'creative',
    capabilities: ['Drafting', 'Editing', 'Reference checks'],
    color: 'from-(--nous-sol) to-(--nous-corona)',
  },
  {
    id: 'data',
    name: 'Data Agent',
    description: 'Extracts tables from PDFs and runs comparisons across benchmark results',
    avatar: '',
    category: 'development',
    capabilities: ['Table extraction', 'Python analysis', 'Charts'],
    color: 'from-(--nous-terra) to-(--nous-sol)',
  },
];

export const ChooseAgent = () => (
  <NewChatDialog
    open
    onOpenChange={() => {}}
    assistants={researchAgents}
    onSelectAssistant={() => {}}
  />
);
