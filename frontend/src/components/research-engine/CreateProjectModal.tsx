'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Loader2, X } from 'lucide-react';
import { createProject } from '@/services/researchEngineService';
import { projectService } from '@/services/projectService';

export interface CreateProjectModalProps {
  isOpen: boolean;
  onClose: () => void;
  onCreated: () => void;
}

export function CreateProjectModal({
  isOpen,
  onClose,
  onCreated,
}: CreateProjectModalProps) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [collectionId, setCollectionId] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: collections = [] } = useQuery({
    queryKey: ['projects', 'research-engine-link-options'],
    queryFn: async () =>
      (await projectService.listProjects({ limit: 200 })).projects,
    enabled: isOpen,
  });

  if (!isOpen) return null;

  const reset = () => {
    setName('');
    setDescription('');
    setCollectionId('');
    setError(null);
  };

  const handleClose = () => {
    if (submitting) return;
    reset();
    onClose();
  };

  const handleSubmit = async () => {
    if (!name.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await createProject({
        name: name.trim(),
        description: description.trim() || undefined,
        collection_id: collectionId || undefined,
      });
      reset();
      onClose();
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create project');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
      <div className="bg-card border border-border rounded-lg w-full max-w-xl p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-mono font-bold text-sol">
            New Research Project
          </h2>
          <button
            onClick={handleClose}
            className="p-1 text-muted-foreground hover:text-foreground"
            aria-label="Close modal"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {error && (
          <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded text-sm text-red-400 font-mono">
            {error}
          </div>
        )}

        <div className="space-y-4">
          <div>
            <label
              htmlFor="engine-project-collection"
              className="block text-xs text-muted-foreground font-mono uppercase tracking-wide mb-1"
            >
              Existing project
            </label>
            <select
              id="engine-project-collection"
              value={collectionId}
              onChange={(event) => setCollectionId(event.target.value)}
              className="w-full px-3 py-2 bg-muted border border-border rounded text-sm text-foreground"
            >
              <option value="">No linked project</option>
              {collections.map((collection) => (
                <option key={collection.id} value={collection.id}>
                  {collection.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs text-muted-foreground font-mono uppercase tracking-wide mb-1">
              Project Name *
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g., Transformer Architecture Survey"
              className="w-full px-3 py-2 bg-muted border border-border rounded text-sm font-mono text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary"
              autoFocus
            />
          </div>

          <div>
            <label className="block text-xs text-muted-foreground font-mono uppercase tracking-wide mb-1">
              Description
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Brief description of the research project"
              rows={3}
              className="w-full px-3 py-2 bg-muted border border-border rounded text-sm font-mono text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary resize-none"
            />
          </div>
        </div>

        <div className="flex justify-end gap-3 mt-6">
          <button
            onClick={handleClose}
            className="px-4 py-2 text-sm font-mono text-muted-foreground hover:text-foreground transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!name.trim() || submitting}
            className="flex items-center gap-2 px-4 py-2 bg-sol/10 text-sol border border-sol/30 rounded font-mono text-sm hover:bg-sol/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
            Create Project
          </button>
        </div>
      </div>
    </div>
  );
}

export default CreateProjectModal;
