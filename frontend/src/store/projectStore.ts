/**
 * Project Store - Zustand state management for Research Projects
 */

import { create } from 'zustand';
import { captureAccountSession } from '@/lib/account-session';
import { getAppQueryClient } from '@/lib/query-client';
import { projectService } from '@/services/projectService';
import type {
  Project,
  ProjectCreate,
  ProjectUpdate,
  ProjectDocument,
  ProjectNote,
  ProjectNoteCreate,
  ProjectNoteUpdate,
  ProjectBibliography,
} from '@/services/projectService';

function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === 'string') {
    return error;
  }
  return fallback;
}

// In-flight dedup for fetchProject: /chat's layout effect (and the project
// page) re-invoke it on render churn before the first GET settles, queueing
// parallel identical /projects/{id} requests (Sentry JAVASCRIPT-NEXTJS-3Q).
// Callers awaiting the same id share one request.
let inflightProjectFetch: { id: string; promise: Promise<void> } | null = null;

// Identity for the "owner" of currentProject/projectDocuments/projectNotes.
// inflightProjectFetch above only dedups the NETWORK call for a repeated
// same-id request; it does nothing for a slow fetchProject('A') resolving
// after fetchProject('B') already committed — that would overwrite B's
// state with A's. Module scope (outside Zustand), unique token objects, same
// pattern as pipelineStore's pipelineRequestToken. A dedup joiner (returns
// inflightProjectFetch.promise) never installs its own token — it's sharing
// the original caller's in-flight request and that caller already owns it.
let projectFetchToken: object | null = null;

// Dual-cache reconciliation (docs/engineering/frontend.md, "Legacy
// server-state stores"): the context rail caches the same project documents/
// notes via useProjectWorkingFolders under ['project', id, 'documents'|
// 'notes'] with a 5-minute staleTime. Every store-side mutation must
// invalidate the Query copy, or the rail serves stale lists until staleTime
// expires. (Reads don't invalidate — a refetch doesn't change server state.)
function invalidateProjectQueries(
  projectId: string,
  scope: 'documents' | 'notes'
): void {
  void getAppQueryClient()?.invalidateQueries({
    queryKey: ['project', projectId, scope],
  });
}

interface ProjectState {
  // State
  projects: Project[];
  currentProject: Project | null;
  projectDocuments: ProjectDocument[];
  projectNotes: ProjectNote[];
  bibliography: ProjectBibliography | null;
  loading: boolean;
  documentsLoading: boolean;
  notesLoading: boolean;
  mutating: boolean;
  error: string | null;
  total: number;

  // Project Actions
  fetchProjects: (
    params?: {
      workspace_id?: string;
      skip?: number;
      limit?: number;
      search?: string;
      project_status?: 'active' | 'paused' | 'completed' | 'archived';
      project_type?: 'research' | 'literature_review' | 'thesis' | 'paper';
      tag?: string;
    },
    options?: { signal?: AbortSignal }
  ) => Promise<void>;
  fetchProject: (projectId: string) => Promise<void>;
  createProject: (data: ProjectCreate) => Promise<Project>;
  updateProject: (projectId: string, data: ProjectUpdate) => Promise<void>;
  deleteProject: (projectId: string) => Promise<void>;
  setCurrentProject: (project: Project | null) => void;

  // Document Actions
  fetchProjectDocuments: (projectId: string) => Promise<void>;
  addDocument: (projectId: string, documentId: string) => Promise<void>;
  removeDocument: (projectId: string, documentId: string) => Promise<void>;

  // Note Actions
  fetchProjectNotes: (projectId: string, pinnedOnly?: boolean) => Promise<void>;
  createNote: (
    projectId: string,
    data: ProjectNoteCreate
  ) => Promise<ProjectNote>;
  updateNote: (
    projectId: string,
    noteId: string,
    data: ProjectNoteUpdate
  ) => Promise<void>;
  deleteNote: (projectId: string, noteId: string) => Promise<void>;
  toggleNotePin: (projectId: string, noteId: string) => Promise<void>;

  // Bibliography Actions
  fetchBibliography: (
    projectId: string,
    format?: 'bibtex' | 'ieee' | 'apa' | 'mla'
  ) => Promise<void>;
  downloadBibliography: (
    projectId: string,
    format?: 'bibtex' | 'ieee' | 'apa' | 'mla'
  ) => Promise<void>;

  // Utility
  clearError: () => void;
  reset: () => void;
}

export const useProjectStore = create<ProjectState>((set, get) => ({
  // Initial state
  projects: [],
  currentProject: null,
  projectDocuments: [],
  projectNotes: [],
  bibliography: null,
  loading: false,
  mutating: false,
  documentsLoading: false,
  notesLoading: false,
  error: null,
  total: 0,

  // =========================================================================
  // Project Actions
  // =========================================================================

  fetchProjects: async (params, options) => {
    const isCurrentAccount = captureAccountSession();
    set({ loading: true, error: null });
    try {
      const response = await projectService.listProjects(params, options);
      if (!isCurrentAccount()) return;
      set({
        projects: response.projects,
        total: response.total,
        loading: false,
      });
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      // Aborted requests (e.g., workspace switched or component unmounted
      // before the previous fetch settled) must still clear `loading` — the
      // common case is that a superseding fetch has already set it back to
      // true, but on unmount nothing replaces us and the spinner would
      // otherwise stick in the global store until the next visit.
      if (
        (error instanceof Error && error.name === 'AbortError') ||
        (error instanceof Error && error.name === 'CanceledError')
      ) {
        set({ loading: false });
        return;
      }
      console.error('[ProjectStore] Failed to fetch projects:', error);
      set({
        error: getErrorMessage(error, 'Failed to fetch projects'),
        loading: false,
      });
    }
  },

  fetchProject: async (projectId) => {
    const isCurrentAccount = captureAccountSession();
    if (inflightProjectFetch?.id === projectId) {
      return inflightProjectFetch.promise;
    }
    const requestToken = {};
    projectFetchToken = requestToken;
    set({ loading: true, error: null });
    const promise = (async () => {
      try {
        const project = await projectService.getProject(projectId);
        if (!isCurrentAccount()) return;
        if (projectFetchToken !== requestToken) return; // superseded
        // Clear stale documents/notes from the previous project on switch
        const prev = get().currentProject;
        if (prev && prev.id !== projectId) {
          set({
            currentProject: project,
            projectDocuments: [],
            projectNotes: [],
            loading: false,
          });
        } else {
          set({ currentProject: project, loading: false });
        }
      } catch (error: unknown) {
        if (!isCurrentAccount()) return;
        if (projectFetchToken !== requestToken) return; // superseded
        console.error('[ProjectStore] Failed to fetch project:', error);
        set({
          error: getErrorMessage(error, 'Failed to fetch project'),
          loading: false,
        });
      } finally {
        if (
          projectFetchToken === requestToken &&
          inflightProjectFetch?.id === projectId
        ) {
          inflightProjectFetch = null;
        }
      }
    })();
    inflightProjectFetch = { id: projectId, promise };
    return promise;
  },

  createProject: async (data) => {
    const isCurrentAccount = captureAccountSession();
    set({ mutating: true, error: null });
    try {
      const project = await projectService.createProject(data);
      if (!isCurrentAccount())
        throw new DOMException('Account changed', 'AbortError');
      set((state) => ({
        projects: [project, ...state.projects],
        total: state.total + 1,
        mutating: false,
      }));
      return project;
    } catch (error: unknown) {
      if (!isCurrentAccount()) throw error;
      console.error('[ProjectStore] Failed to create project:', error);
      set({
        error: getErrorMessage(error, 'Failed to create project'),
        mutating: false,
      });
      throw error;
    }
  },

  updateProject: async (projectId, data) => {
    const isCurrentAccount = captureAccountSession();
    set({ mutating: true, error: null });
    try {
      const updated = await projectService.updateProject(projectId, data);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projects: state.projects.map((p) => (p.id === projectId ? updated : p)),
        currentProject:
          state.currentProject?.id === projectId
            ? updated
            : state.currentProject,
        mutating: false,
      }));
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to update project:', error);
      set({
        error: getErrorMessage(error, 'Failed to update project'),
        mutating: false,
      });
      throw error;
    }
  },

  deleteProject: async (projectId) => {
    const isCurrentAccount = captureAccountSession();
    set({ mutating: true, error: null });
    try {
      await projectService.deleteProject(projectId);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projects: state.projects.filter((p) => p.id !== projectId),
        currentProject:
          state.currentProject?.id === projectId ? null : state.currentProject,
        total: state.total - 1,
        mutating: false,
      }));
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to delete project:', error);
      set({
        error: getErrorMessage(error, 'Failed to delete project'),
        mutating: false,
      });
      throw error;
    }
  },

  setCurrentProject: (project) => {
    const prev = get().currentProject;
    // Clear stale documents/notes when switching to a different project
    if (prev && project && prev.id !== project.id) {
      set({ currentProject: project, projectDocuments: [], projectNotes: [] });
    } else {
      set({ currentProject: project });
    }
  },

  // =========================================================================
  // Document Actions
  // =========================================================================

  fetchProjectDocuments: async (projectId) => {
    const isCurrentAccount = captureAccountSession();
    set({ documentsLoading: true, error: null });
    try {
      const response = await projectService.listProjectDocuments(projectId);
      if (!isCurrentAccount()) return;
      set({
        projectDocuments: response.documents,
        documentsLoading: false,
      });
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to fetch documents:', error);
      set({
        error: getErrorMessage(error, 'Failed to fetch documents'),
        documentsLoading: false,
      });
    }
  },

  addDocument: async (projectId, documentId) => {
    const isCurrentAccount = captureAccountSession();
    set({ documentsLoading: true, error: null });
    try {
      const doc = await projectService.addDocumentToProject(
        projectId,
        documentId
      );
      if (!isCurrentAccount()) return;
      set((state) => ({
        projectDocuments: [...state.projectDocuments, doc],
        documentsLoading: false,
      }));
      invalidateProjectQueries(projectId, 'documents');
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to add document:', error);
      set({
        error: getErrorMessage(error, 'Failed to add document'),
        documentsLoading: false,
      });
      throw error;
    }
  },

  removeDocument: async (projectId, documentId) => {
    const isCurrentAccount = captureAccountSession();
    set({ documentsLoading: true, error: null });
    try {
      await projectService.removeDocumentFromProject(projectId, documentId);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projectDocuments: state.projectDocuments.filter(
          (d) => d.document_id !== documentId
        ),
        documentsLoading: false,
      }));
      invalidateProjectQueries(projectId, 'documents');
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to remove document:', error);
      set({
        error: getErrorMessage(error, 'Failed to remove document'),
        documentsLoading: false,
      });
      throw error;
    }
  },

  // =========================================================================
  // Note Actions
  // =========================================================================

  fetchProjectNotes: async (projectId, pinnedOnly = false) => {
    const isCurrentAccount = captureAccountSession();
    set({ notesLoading: true, error: null });
    try {
      const response = await projectService.listProjectNotes(projectId, {
        pinned_only: pinnedOnly,
      });
      if (!isCurrentAccount()) return;
      set({
        projectNotes: response.notes,
        notesLoading: false,
      });
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to fetch notes:', error);
      set({
        error: getErrorMessage(error, 'Failed to fetch notes'),
        notesLoading: false,
      });
    }
  },

  createNote: async (projectId, data) => {
    const isCurrentAccount = captureAccountSession();
    set({ notesLoading: true, error: null });
    try {
      const note = await projectService.createNote(projectId, data);
      if (!isCurrentAccount())
        throw new DOMException('Account changed', 'AbortError');
      set((state) => ({
        projectNotes: [note, ...state.projectNotes],
        notesLoading: false,
      }));
      invalidateProjectQueries(projectId, 'notes');
      return note;
    } catch (error: unknown) {
      if (!isCurrentAccount()) throw error;
      console.error('[ProjectStore] Failed to create note:', error);
      set({
        error: getErrorMessage(error, 'Failed to create note'),
        notesLoading: false,
      });
      throw error;
    }
  },

  updateNote: async (projectId, noteId, data) => {
    const isCurrentAccount = captureAccountSession();
    set({ notesLoading: true, error: null });
    try {
      const updated = await projectService.updateNote(projectId, noteId, data);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projectNotes: state.projectNotes.map((n) =>
          n.id === noteId ? updated : n
        ),
        notesLoading: false,
      }));
      invalidateProjectQueries(projectId, 'notes');
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to update note:', error);
      set({
        error: getErrorMessage(error, 'Failed to update note'),
        notesLoading: false,
      });
      throw error;
    }
  },

  deleteNote: async (projectId, noteId) => {
    const isCurrentAccount = captureAccountSession();
    set({ notesLoading: true, error: null });
    try {
      await projectService.deleteNote(projectId, noteId);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projectNotes: state.projectNotes.filter((n) => n.id !== noteId),
        notesLoading: false,
      }));
      invalidateProjectQueries(projectId, 'notes');
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to delete note:', error);
      set({
        error: getErrorMessage(error, 'Failed to delete note'),
        notesLoading: false,
      });
      throw error;
    }
  },

  toggleNotePin: async (projectId, noteId) => {
    const isCurrentAccount = captureAccountSession();
    try {
      const updated = await projectService.toggleNotePin(projectId, noteId);
      if (!isCurrentAccount()) return;
      set((state) => ({
        projectNotes: state.projectNotes.map((n) =>
          n.id === noteId ? updated : n
        ),
      }));
      invalidateProjectQueries(projectId, 'notes');
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to toggle pin:', error);
      set({
        error: getErrorMessage(error, 'Failed to toggle pin'),
      });
      throw error;
    }
  },

  // =========================================================================
  // Bibliography Actions
  // =========================================================================

  fetchBibliography: async (projectId, format = 'bibtex') => {
    const isCurrentAccount = captureAccountSession();
    set({ loading: true, error: null });
    try {
      const bibliography = await projectService.getProjectBibliography(
        projectId,
        format
      );
      if (!isCurrentAccount()) return;
      set({ bibliography, loading: false });
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to fetch bibliography:', error);
      set({
        error: getErrorMessage(error, 'Failed to fetch bibliography'),
        loading: false,
      });
    }
  },

  downloadBibliography: async (projectId, format = 'bibtex') => {
    const isCurrentAccount = captureAccountSession();
    try {
      await projectService.downloadBibliography(projectId, format);
      if (!isCurrentAccount()) return;
    } catch (error: unknown) {
      if (!isCurrentAccount()) return;
      console.error('[ProjectStore] Failed to download bibliography:', error);
      set({
        error: getErrorMessage(error, 'Failed to download bibliography'),
      });
      throw error;
    }
  },

  // =========================================================================
  // Utility
  // =========================================================================

  clearError: () => {
    set({ error: null });
  },

  reset: () => {
    projectFetchToken = null;
    inflightProjectFetch = null;
    set({
      projects: [],
      currentProject: null,
      projectDocuments: [],
      projectNotes: [],
      bibliography: null,
      loading: false,
      mutating: false,
      documentsLoading: false,
      notesLoading: false,
      error: null,
      total: 0,
    });
  },
}));

export default useProjectStore;
