import { create } from 'zustand';
import type { ProjectResponse } from '@/services/researchEngineService';

export type ResearchProject = ProjectResponse;

export interface ResearchRun {
  id: string;
  project_id?: string;
  research_engine_project_id?: string;
  blueprint_id: string;
  blueprint_version: number;
  status: 'pending' | 'running' | 'paused' | 'completed' | 'failed';
  total_tokens: number;
  started_at?: string;
  completed_at?: string;
}

export interface RunStepEvent {
  event: string;
  step_index?: number;
  step_type?: string;
  step_name?: string;
  token_count?: number;
  quality_marks?: Array<{
    check_type: string;
    passed: boolean;
    details?: string;
  }>;
  mode?: string;
  output?: string | Record<string, unknown>;
  sources?: string[];
  prompt?: string;
  error_message?: string;
  timestamp: string;
}

interface ResearchEngineState {
  projects: ResearchProject[];
  activeProject: ResearchProject | null;
  activeRun: ResearchRun | null;
  runEvents: RunStepEvent[];
  isLoading: boolean;
  error: string | null;
  setProjects: (projects: ResearchProject[]) => void;
  setActiveProject: (project: ResearchProject | null) => void;
  setActiveRun: (run: ResearchRun | null) => void;
  addRunEvent: (event: RunStepEvent) => void;
  clearRunEvents: () => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
}

export const useResearchEngineStore = create<ResearchEngineState>((set) => ({
  projects: [],
  activeProject: null,
  activeRun: null,
  runEvents: [],
  isLoading: false,
  error: null,
  setProjects: (projects) => set({ projects }),
  setActiveProject: (project) => set({ activeProject: project }),
  setActiveRun: (run) => set({ activeRun: run }),
  addRunEvent: (event) =>
    set((state) => ({ runEvents: [...state.runEvents, event] })),
  clearRunEvents: () => set({ runEvents: [] }),
  setLoading: (loading) => set({ isLoading: loading }),
  setError: (error) => set({ error }),
}));
