import { ProjectCard } from '@nous/design-system';

// ProjectCard is the research-engine dashboard tile (3-up grid). Clicking it
// routes to the project's blueprint; projects linked to a collection also get
// an "Open project workspace" link. `status` drives the badge.

const project = (over: Record<string, unknown>) => ({
  id: 're-proj-1',
  project_id: 'proj-1',
  research_engine_project_id: 're-proj-1',
  name: 'Long-context attention survey',
  description:
    'Systematic review of exact and approximate attention for 32k+ token training, with a reproducible benchmark on A100s.',
  status: 'active',
  settings: {},
  created_at: '2026-10-12T09:00:00Z',
  updated_at: '2026-11-03T14:30:00Z',
  ...over,
});

const active = project({ collection_id: 'col-attention' });
const paused = project({
  id: 're-proj-2',
  name: 'Retrieval-augmented citation accuracy',
  description: 'Measuring citation faithfulness of RAG answers against held-out arXiv abstracts.',
  status: 'paused',
  created_at: '2026-09-02T10:00:00Z',
});
const completed = project({
  id: 're-proj-3',
  name: 'Protein language model benchmarks',
  description: 'Replication of ESM-2 contact-prediction results across three model sizes.',
  status: 'completed',
  created_at: '2026-06-18T08:00:00Z',
  collection_id: 'col-protein',
});
const archived = project({
  id: 're-proj-4',
  name: 'Diffusion sampler ablations',
  description: 'Superseded by the consistency-model study.',
  status: 'archived',
  created_at: '2026-03-04T12:00:00Z',
});

export const Active = () => (
  <div style={{ maxWidth: 360 }}>
    <ProjectCard project={active} />
  </div>
);

export const StatusSweep = () => (
  <div className="grid grid-cols-2 gap-4" style={{ maxWidth: 760 }}>
    <ProjectCard project={active} />
    <ProjectCard project={paused} />
    <ProjectCard project={completed} />
    <ProjectCard project={archived} />
  </div>
);

export const MinimalProject = () => (
  <div style={{ maxWidth: 360 }}>
    <ProjectCard
      project={project({
        id: 're-proj-5',
        name: 'Sparse mixture-of-experts routing',
        description: null,
      })}
    />
  </div>
);
