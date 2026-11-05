import { ConfirmationCard } from '@nous/design-system';

// The agent pauses before ingesting, creating notes or drafting, and waits
// for the researcher. The card names each action and its arguments.

export const IngestPapers = () => (
  <div style={{ maxWidth: 520 }}>
    <ConfirmationCard
      message="NOUS wants to add 2 papers to “Sparse attention survey”."
      tools={[
        {
          name: 'ingest_arxiv_papers',
          args: { arxiv_ids: ['2004.05150', '2007.14062'], project: 'Sparse attention survey' },
        },
      ]}
      onConfirm={() => {}}
      onCancel={() => {}}
    />
  </div>
);

export const NoteAndDraft = () => (
  <div style={{ maxWidth: 520 }}>
    <ConfirmationCard
      message="NOUS wants to save a reading note and start a draft."
      tools={[
        {
          name: 'create_project_note',
          args: { title: 'Reading notes: Longformer', tags: ['attention', 'long-context'] },
        },
        { name: 'create_draft', args: { title: 'Related work: sparse attention', sections: 3 } },
      ]}
      onConfirm={() => {}}
      onCancel={() => {}}
    />
  </div>
);

export const Running = () => (
  <div style={{ maxWidth: 520 }}>
    <ConfirmationCard
      message="NOUS wants to add 1 paper to “Sparse attention survey”."
      tools={[{ name: 'ingest_arxiv_papers', args: { arxiv_ids: ['2004.05150'] } }]}
      onConfirm={() => {}}
      onCancel={() => {}}
      isLoading
    />
  </div>
);
