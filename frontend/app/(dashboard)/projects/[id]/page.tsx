'use client';

/**
 * Project Detail Page
 * Displays project information with tabs for Documents, Notes, Bibliography,
 * Drafts, Chat, Matrix, and Pipeline
 */

import React, { useEffect, useState, useCallback, useRef } from 'react';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import {
  FileText,
  StickyNote,
  BookOpen,
  Sparkles,
  Plus,
  Download,
  Loader2,
  RefreshCw,
  MessageSquare,
  Grid3X3,
  GitBranch,
  Network,
  Upload,
  MoreHorizontal,
} from 'lucide-react';
import { ProjectHeader } from '@/components/research/ProjectHeader';
import { DocumentList } from '@/components/research/DocumentList';
import { ProjectKnowledgeTree } from '@/components/research/ProjectKnowledgeTree';
import { ProjectSkillsTab } from '@/components/research/ProjectSkillsTab';
import { DraftGenerator } from '@/components/research/DraftGenerator';
import { DraftViewer } from '@/components/research/DraftViewer';
import { DraftGenerationProgress } from '@/components/research/DraftGenerationProgress';
import { DraftComparison } from '@/components/research/DraftComparison';
import { DraftExportModal } from '@/components/research/DraftExportModal';
import { ProjectChatTab } from '@/components/research/ProjectChatTab';
import { ExtractionMatrix } from '@/components/research/ExtractionMatrix';
import { ResearchPipeline } from '@/components/research/ResearchPipeline';
import { NoteEditor } from '@/components/research/NoteEditor';
import { NoteList } from '@/components/research/NoteList';
import { DocumentUploadWizard } from '@/components/upload';
import {
  projectService,
  type Draft,
  type DraftComparison as DraftComparisonData,
  type GenerationStatus,
} from '@/services/projectService';
import { useProjectStore } from '@/store/projectStore';
import { useAgentChatStore } from '@/store/agentChatStore';
import { useAuthStore } from '@/stores/authStore';
import toast from 'react-hot-toast';
import { useProjectSkillCatalog } from '@/hooks/useProjectSkills';
import { APIErrorClass } from '@/types/api';
import type { ProjectNote, ProjectNoteCreate } from '@/services/projectService';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';

type TabType =
  | 'documents'
  | 'notes'
  | 'bibliography'
  | 'drafts'
  | 'chat'
  | 'matrix'
  | 'pipeline'
  | 'knowledge'
  | 'skills';

export default function ProjectDetailPage() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const draftLinkKey =
    searchParams?.get('tab') === 'drafts'
      ? (searchParams.get('draftId') ?? '')
      : null;
  const projectId = params.id as string;
  const { isAuthenticated } = useAuthStore();

  const {
    currentProject,
    projectDocuments,
    projectNotes,
    bibliography,
    loading,
    documentsLoading,
    notesLoading,
    error,
    fetchProject,
    fetchProjectDocuments,
    fetchProjectNotes,
    removeDocument,
    createNote,
    updateNote,
    deleteNote,
    toggleNotePin,
    fetchBibliography,
    downloadBibliography,
    clearError,
  } = useProjectStore();

  const [mounted, setMounted] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<TabType>('documents');
  const linkedDraftIdRef = useRef<string | null>(null);
  const appliedDraftLinkRef = useRef<string | null>(null);
  const [bibFormat, setBibFormat] = useState<'bibtex' | 'ieee' | 'apa' | 'mla'>(
    'bibtex'
  );

  // Draft state
  const [currentDraft, setCurrentDraft] = useState<Draft | null>(null);
  const [draftVersions, setDraftVersions] = useState<
    Array<{
      id: string;
      version: number;
      created_at: string;
      is_current: boolean;
    }>
  >([]);
  const [draftsLoading, setDraftsLoading] = useState(false);
  const [generationTaskId, setGenerationTaskId] = useState<string | null>(null);
  const [generationStatus, setGenerationStatus] =
    useState<GenerationStatus | null>(null);
  const [compareVersionA, setCompareVersionA] = useState<number | null>(null);
  const [compareVersionB, setCompareVersionB] = useState<number | null>(null);
  const [draftComparison, setDraftComparison] =
    useState<DraftComparisonData | null>(null);
  const [comparisonDraftA, setComparisonDraftA] = useState<Draft | null>(null);
  const [comparisonDraftB, setComparisonDraftB] = useState<Draft | null>(null);
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const [showExportModal, setShowExportModal] = useState(false);
  const [exportInitialFormat, setExportInitialFormat] = useState<
    'markdown' | 'latex'
  >('markdown');

  // Note editing state
  const [editingNote, setEditingNote] = useState<ProjectNote | null>(null);
  const [showNoteEditor, setShowNoteEditor] = useState(false);
  const [selectedNoteTag, setSelectedNoteTag] = useState('');
  const [matrixId, setMatrixId] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const pollTimeoutRef = React.useRef<ReturnType<typeof setTimeout> | null>(
    null
  );
  // Roving-tabindex focus (R6-L18): arrow/Home/End must move DOM focus, not
  // just activeTab. Primary tabs are real buttons; secondary tabs live in the
  // "More" dropdown, so they fall back to focusing the dropdown trigger.
  const primaryTabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const moreTabsTriggerRef = useRef<HTMLButtonElement | null>(null);
  const [showUploadWizard, setShowUploadWizard] = useState(false);
  const [deleteNoteDialogOpen, setDeleteNoteDialogOpen] = useState(false);
  const [noteToDelete, setNoteToDelete] = useState<string | null>(null);
  const [projectError, setProjectError] = useState<{
    status: number;
    message: string;
  } | null>(null);
  // The backend catalog is the availability source of truth. Do not expose a
  // client-only feature flag for a capability that can be disabled server-side.
  const skillsCatalog = useProjectSkillCatalog(
    projectId,
    mounted && isAuthenticated && Boolean(projectId)
  );

  useEffect(() => {
    // This pre-existing hydration guard intentionally flips after the client mounts.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMounted(true);
  }, []);

  const projectDataVersion = useAgentChatStore((s) => s.projectDataVersion);

  useEffect(() => {
    if (mounted && isAuthenticated && projectId) {
      fetchProject(projectId)
        .then(() => setProjectError(null))
        .catch((err: unknown) => {
          const status =
            err instanceof APIErrorClass ? err.error.status_code : 500;
          const message =
            err instanceof Error ? err.message : 'Failed to load project';
          setProjectError({ status, message });
        })
        .finally(() => setInitialLoading(false));
      fetchProjectDocuments(projectId);
      fetchProjectNotes(projectId);
    }
  }, [
    mounted,
    isAuthenticated,
    projectId,
    fetchProject,
    fetchProjectDocuments,
    fetchProjectNotes,
  ]);

  // Refetch project data when agent tools mutate it (e.g. add_document_to_project)
  useEffect(() => {
    if (projectDataVersion > 0 && projectId) {
      fetchProject(projectId);
      fetchProjectDocuments(projectId);
      fetchProjectNotes(projectId);
    }
  }, [
    projectDataVersion,
    projectId,
    fetchProject,
    fetchProjectDocuments,
    fetchProjectNotes,
  ]);

  const loadDrafts = useCallback(
    async (preferredVersion?: number) => {
      setDraftsLoading(true);
      try {
        const response = await projectService.listDrafts(projectId, {
          limit: 50,
        });
        let versions = response.drafts.map((draft) => ({
          id: draft.id,
          version: draft.version,
          created_at: draft.created_at,
          is_current: draft.is_current,
        }));

        // A completion link can point to an older version outside this page.
        const linkedDraftId = linkedDraftIdRef.current;
        let linkedDraft: Draft | null = null;
        if (
          linkedDraftId &&
          !versions.some((draft) => draft.id === linkedDraftId)
        ) {
          linkedDraft = await projectService.getDraft(projectId, linkedDraftId);
          versions = [
            ...versions,
            {
              id: linkedDraft.id,
              version: linkedDraft.version,
              created_at: linkedDraft.created_at,
              is_current: linkedDraft.is_current,
            },
          ];
        }

        setDraftVersions(versions);

        if (versions.length === 0) {
          setCurrentDraft(null);
          setCompareVersionA(null);
          setCompareVersionB(null);
          setDraftComparison(null);
          setComparisonDraftA(null);
          setComparisonDraftB(null);
          return;
        }

        const selectedVersion =
          preferredVersion ??
          versions.find((draft) => draft.id === linkedDraftIdRef.current)
            ?.version ??
          versions.find((draft) => draft.is_current)?.version ??
          versions[0].version;

        const selectedDraftMeta =
          versions.find((draft) => draft.version === selectedVersion) ??
          versions[0];

        const selectedDraft =
          linkedDraft?.id === selectedDraftMeta.id
            ? linkedDraft
            : await projectService.getDraft(projectId, selectedDraftMeta.id);
        linkedDraftIdRef.current = null;
        setCurrentDraft(selectedDraft);

        setCompareVersionA((previous) => {
          if (
            previous &&
            versions.some((draft) => draft.version === previous)
          ) {
            return previous;
          }
          return versions[0].version;
        });

        setCompareVersionB((previous) => {
          if (
            previous &&
            versions.some((draft) => draft.version === previous)
          ) {
            return previous;
          }
          return versions.length > 1
            ? versions[1].version
            : versions[0].version;
        });
      } catch (err) {
        console.error('Failed to load drafts:', err);
      } finally {
        setDraftsLoading(false);
      }
    },
    [projectId]
  );

  useEffect(() => {
    if (
      !mounted ||
      !isAuthenticated ||
      draftLinkKey === appliedDraftLinkRef.current
    ) {
      return;
    }
    appliedDraftLinkRef.current = draftLinkKey;
    if (draftLinkKey === null) return;
    linkedDraftIdRef.current = draftLinkKey || null;
    if (activeTab === 'drafts') {
      // A new draft link on this page needs to reload the selected version.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      void loadDrafts();
    } else {
      setActiveTab('drafts');
    }
  }, [activeTab, draftLinkKey, isAuthenticated, loadDrafts, mounted]);

  useEffect(() => {
    if (activeTab === 'drafts' && mounted && isAuthenticated && projectId) {
      // The effect synchronizes server draft state when the tab becomes active.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      void loadDrafts();
    }
  }, [activeTab, mounted, isAuthenticated, projectId, loadDrafts]);

  useEffect(() => {
    if (
      projectDataVersion > 0 &&
      activeTab === 'drafts' &&
      mounted &&
      isAuthenticated &&
      projectId
    ) {
      // Agent mutations invalidate the server-backed draft view.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      void loadDrafts();
    }
  }, [
    activeTab,
    isAuthenticated,
    loadDrafts,
    mounted,
    projectDataVersion,
    projectId,
  ]);

  // Clean up draft generation polling on unmount
  useEffect(() => {
    return () => {
      if (pollTimeoutRef.current) {
        clearTimeout(pollTimeoutRef.current);
      }
    };
  }, []);

  const handleDraftVersionChange = useCallback(
    async (version: number) => {
      setDraftComparison(null);
      setComparisonDraftA(null);
      setComparisonDraftB(null);
      await loadDrafts(version);
    },
    [loadDrafts]
  );

  const handleCompareDrafts = useCallback(async () => {
    if (compareVersionA === null || compareVersionB === null) {
      return;
    }
    if (compareVersionA === compareVersionB) {
      return;
    }

    const draftA = draftVersions.find(
      (draft) => draft.version === compareVersionA
    );
    const draftB = draftVersions.find(
      (draft) => draft.version === compareVersionB
    );

    if (!draftA || !draftB) {
      return;
    }

    setComparisonLoading(true);
    try {
      const [comparison, draftAData, draftBData] = await Promise.all([
        projectService.compareDrafts(
          projectId,
          compareVersionA,
          compareVersionB
        ),
        projectService.getDraft(projectId, draftA.id),
        projectService.getDraft(projectId, draftB.id),
      ]);
      setDraftComparison(comparison);
      setComparisonDraftA(draftAData);
      setComparisonDraftB(draftBData);
    } catch (err) {
      console.error('Failed to compare drafts:', err);
    } finally {
      setComparisonLoading(false);
    }
  }, [compareVersionA, compareVersionB, draftVersions, projectId]);

  const handleTabChange = useCallback(
    (tab: TabType) => {
      setActiveTab(tab);
      if (tab === 'bibliography') {
        fetchBibliography(projectId, bibFormat);
      }
    },
    [projectId, bibFormat, fetchBibliography]
  );

  const handleRemoveDocument = async (documentId: string) => {
    try {
      await removeDocument(projectId, documentId);
    } catch (err) {
      console.error('Failed to remove document:', err);
    }
  };

  const handleCreateNote = () => {
    setEditingNote(null);
    setShowNoteEditor(true);
  };

  const handleEditNote = (note: ProjectNote) => {
    setEditingNote(note);
    setShowNoteEditor(true);
  };

  const handleSaveNote = async (noteData: ProjectNoteCreate) => {
    // Errors propagate to NoteEditor, which keeps the dialog open and shows
    // the failure inline (R6-H7). Closing here would fake a successful save.
    if (editingNote) {
      await updateNote(projectId, editingNote.id, {
        title: noteData.title.trim(),
        content: noteData.content,
        tags: noteData.tags,
        linked_document_ids: noteData.linked_document_ids,
      });
    } else {
      await createNote(projectId, noteData);
    }
    setShowNoteEditor(false);
    setEditingNote(null);
  };

  const handleDeleteNote = (noteId: string) => {
    setNoteToDelete(noteId);
    setDeleteNoteDialogOpen(true);
  };

  const confirmDeleteNote = async () => {
    if (!noteToDelete) return;
    try {
      await deleteNote(projectId, noteToDelete);
    } catch (err) {
      console.error('Failed to delete note:', err);
    } finally {
      setNoteToDelete(null);
      setDeleteNoteDialogOpen(false);
    }
  };

  const handleTogglePin = async (noteId: string) => {
    try {
      await toggleNotePin(projectId, noteId);
    } catch (err) {
      console.error('Failed to toggle pin:', err);
    }
  };

  const handleExportBibliography = async () => {
    try {
      await downloadBibliography(projectId, bibFormat);
    } catch (err) {
      console.error('Failed to download bibliography:', err);
    }
  };

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const refreshTasks: Array<Promise<unknown>> = [
        fetchProject(projectId),
        fetchProjectDocuments(projectId),
        fetchProjectNotes(projectId),
      ];

      if (activeTab === 'bibliography') {
        refreshTasks.push(fetchBibliography(projectId, bibFormat));
      }

      if (activeTab === 'drafts') {
        refreshTasks.push(loadDrafts());
      }

      await Promise.allSettled(refreshTasks);
    } finally {
      setRefreshing(false);
    }
  }, [
    activeTab,
    bibFormat,
    fetchBibliography,
    fetchProject,
    fetchProjectDocuments,
    fetchProjectNotes,
    loadDrafts,
    projectId,
  ]);

  const handleUploadComplete = useCallback(
    async (documentIds: string[]) => {
      setShowUploadWizard(false);
      try {
        await Promise.all(
          documentIds.map((docId) =>
            projectService.addDocumentToProject(projectId, docId)
          )
        );
        await fetchProjectDocuments(projectId);
      } catch {
        // linking may partially fail, still refresh to show what succeeded
        await fetchProjectDocuments(projectId);
      }
    },
    [projectId, fetchProjectDocuments]
  );

  if (!mounted || initialLoading) {
    return (
      <div
        role="status"
        aria-busy="true"
        aria-label="Loading project"
        className="p-3 sm:p-6 pb-20 md:pb-6 max-w-7xl mx-auto"
      >
        <div className="mb-4 sm:mb-8 space-y-3 sm:space-y-4">
          <div className="h-4 w-20 rounded-md bg-muted animate-pulse" />
          <div className="rounded-xl border border-border bg-card p-4 sm:p-6 space-y-3">
            <div className="h-6 w-1/2 rounded-md bg-muted animate-pulse" />
            <div className="h-4 w-3/4 rounded-md bg-muted animate-pulse" />
            <div className="h-3 w-40 rounded-md bg-muted animate-pulse" />
          </div>
        </div>
        <div className="flex gap-2 border-b border-border pb-2 mb-6">
          {Array.from({ length: 5 }).map((_, i) => (
            <div
              key={i}
              className="h-8 w-20 rounded-md bg-muted animate-pulse"
            />
          ))}
        </div>
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              className="h-16 rounded-lg border border-border bg-card animate-pulse"
            />
          ))}
        </div>
      </div>
    );
  }

  if (projectError) {
    const isNotFound = projectError.status === 404;
    return (
      <div
        role="alert"
        className="mx-auto mt-10 max-w-md rounded-xl border border-border bg-card p-6 text-center"
      >
        <p className="text-base font-medium text-foreground">
          {isNotFound ? 'Project not found' : 'Could not load this project'}
        </p>
        <p className="mt-1.5 text-sm text-muted-foreground">
          {isNotFound
            ? 'It may have been deleted or you may not have access.'
            : projectError.message}
        </p>
        <div className="mt-5 flex items-center justify-center gap-3">
          {!isNotFound && (
            <button
              onClick={() => {
                setInitialLoading(true);
                setProjectError(null);
                fetchProject(projectId)
                  .then(() => setProjectError(null))
                  .catch((err: unknown) => {
                    const status =
                      err instanceof APIErrorClass
                        ? err.error.status_code
                        : 500;
                    const message =
                      err instanceof Error
                        ? err.message
                        : 'Failed to load project';
                    setProjectError({ status, message });
                  })
                  .finally(() => setInitialLoading(false));
              }}
              className="inline-flex items-center rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              Try again
            </button>
          )}
          <button
            onClick={() => router.push('/projects')}
            className="inline-flex items-center rounded-md border border-border bg-card px-3.5 py-2 text-sm text-foreground transition-colors hover:bg-muted focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            Back to projects
          </button>
        </div>
      </div>
    );
  }

  if (!currentProject) {
    return (
      <div className="mx-auto mt-10 max-w-md rounded-xl border border-border bg-card p-6 text-center">
        <p className="text-base font-medium text-foreground">
          Project not found
        </p>
        <p className="mt-1.5 text-sm text-muted-foreground">
          It may have been deleted or you may not have access.
        </p>
        <button
          onClick={() => router.push('/projects')}
          className="mt-5 inline-flex items-center rounded-md border border-border bg-card px-3.5 py-2 text-sm text-foreground transition-colors hover:bg-muted focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          Back to projects
        </button>
      </div>
    );
  }

  const primaryTabs: Array<{
    id: TabType;
    label: string;
    mobileLabel: string;
    icon: React.ElementType;
    count?: number;
  }> = [
    {
      id: 'documents',
      label: 'Documents',
      mobileLabel: 'Docs',
      icon: FileText,
      count: projectDocuments.length,
    },
    {
      id: 'notes',
      label: 'Notes',
      mobileLabel: 'Notes',
      icon: StickyNote,
      count: projectNotes.length,
    },
    {
      id: 'drafts',
      label: 'Drafts',
      mobileLabel: 'Drafts',
      icon: Sparkles,
      count: draftVersions.length || undefined,
    },
    { id: 'chat', label: 'Chat', mobileLabel: 'Chat', icon: MessageSquare },
  ];

  const secondaryTabs: Array<{
    id: TabType;
    label: string;
    icon: React.ElementType;
  }> = [
    { id: 'bibliography', label: 'Bibliography', icon: BookOpen },
    { id: 'matrix', label: 'Matrix', icon: Grid3X3 },
    { id: 'pipeline', label: 'Pipeline', icon: GitBranch },
    { id: 'knowledge', label: 'Knowledge', icon: Network },
  ];

  if (skillsCatalog.isSuccess) {
    secondaryTabs.push({ id: 'skills', label: 'Skills', icon: Sparkles });
  }

  const allTabs = [
    ...primaryTabs,
    ...secondaryTabs.map((t) => ({ ...t, mobileLabel: t.label.slice(0, 4) })),
  ];
  const isSecondaryTabActive = secondaryTabs.some((t) => t.id === activeTab);

  return (
    <div className="p-3 sm:p-6 pb-20 md:pb-6 max-w-7xl mx-auto">
      {/* Header */}
      <ProjectHeader project={currentProject} />

      {/* Error Display */}
      {error && (
        <div
          role="alert"
          className="mb-6 p-4 bg-destructive/10 border border-destructive/30 rounded-lg"
        >
          <p className="text-destructive text-sm">{error}</p>
          <button
            onClick={clearError}
            className="mt-2 text-xs text-destructive underline-offset-4 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 rounded"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Tabs */}
      <div className="mb-3 sm:mb-6 border-b border-border pb-2">
        <div className="flex items-center gap-2 mb-2 sm:mb-0 sm:float-right">
          <button
            onClick={() => setShowUploadWizard(true)}
            className="inline-flex items-center justify-center gap-1.5 rounded-md bg-primary px-2.5 py-1.5 sm:px-3 sm:py-2 text-xs sm:text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            <Upload aria-hidden="true" className="h-3.5 w-3.5 sm:h-4 sm:w-4" />
            Upload
          </button>
          <button
            onClick={() => {
              void handleRefresh();
            }}
            disabled={refreshing}
            aria-label={refreshing ? 'Refreshing project' : 'Refresh project'}
            className="inline-flex items-center justify-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 sm:px-3 sm:py-2 text-xs sm:text-sm text-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            <RefreshCw
              aria-hidden="true"
              className={`h-3.5 w-3.5 sm:h-4 sm:w-4 ${refreshing ? 'animate-spin' : ''}`}
            />
            Refresh
          </button>
        </div>
        <div className="overflow-x-auto -mx-3 px-3 sm:mx-0 sm:px-0">
          <div
            role="tablist"
            className="flex items-center gap-0.5 sm:gap-1 whitespace-nowrap"
            onKeyDown={(e) => {
              const currentIndex = allTabs.findIndex((t) => t.id === activeTab);
              let nextIndex = -1;
              if (e.key === 'ArrowRight') {
                nextIndex = (currentIndex + 1) % allTabs.length;
              } else if (e.key === 'ArrowLeft') {
                nextIndex =
                  (currentIndex - 1 + allTabs.length) % allTabs.length;
              } else if (e.key === 'Home') {
                nextIndex = 0;
              } else if (e.key === 'End') {
                nextIndex = allTabs.length - 1;
              }
              if (nextIndex !== -1 && nextIndex !== currentIndex) {
                e.preventDefault();
                handleTabChange(allTabs[nextIndex].id);
                // Move DOM focus along with the selection so keyboard users
                // aren't stranded on the previous tab (R6-L18).
                const nextTab = allTabs[nextIndex];
                const primaryIdx = primaryTabs.findIndex(
                  (t) => t.id === nextTab.id
                );
                if (primaryIdx >= 0) {
                  primaryTabRefs.current[primaryIdx]?.focus();
                } else {
                  moreTabsTriggerRef.current?.focus();
                }
              }
            }}
          >
            {primaryTabs.map((tab, tabIndex) => (
              <button
                key={tab.id}
                ref={(node) => {
                  primaryTabRefs.current[tabIndex] = node;
                }}
                onClick={() => handleTabChange(tab.id)}
                role="tab"
                aria-selected={activeTab === tab.id}
                tabIndex={activeTab === tab.id ? 0 : -1}
                className={`relative flex items-center gap-1 sm:gap-2 px-2 py-2 sm:px-3 sm:py-2.5 text-xs sm:text-sm rounded-t-md transition-colors shrink-0 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ${
                  activeTab === tab.id
                    ? 'text-foreground bg-muted/60 border-b-2 border-primary'
                    : 'text-muted-foreground hover:text-foreground hover:bg-muted/30'
                }`}
              >
                <tab.icon
                  aria-hidden="true"
                  className="h-3.5 w-3.5 sm:h-4 sm:w-4"
                />
                <span className="hidden sm:inline">{tab.label}</span>
                <span className="sm:hidden">{tab.mobileLabel}</span>
                {tab.count !== undefined && tab.count > 0 && (
                  <span
                    className={`ml-0.5 sm:ml-1 text-[10px] sm:text-xs rounded-full px-1 sm:px-1.5 py-0.5 ${
                      activeTab === tab.id
                        ? 'bg-primary/15 text-primary'
                        : 'bg-muted text-muted-foreground'
                    }`}
                  >
                    {tab.count}
                  </span>
                )}
              </button>
            ))}

            {/* More dropdown for secondary tabs */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  ref={moreTabsTriggerRef}
                  className={`relative flex items-center gap-1 sm:gap-2 px-2 py-2 sm:px-3 sm:py-2.5 text-xs sm:text-sm rounded-t-md transition-colors shrink-0 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ${
                    isSecondaryTabActive
                      ? 'text-foreground bg-muted/60 border-b-2 border-primary'
                      : 'text-muted-foreground hover:text-foreground hover:bg-muted/30'
                  }`}
                  aria-label="More tabs"
                >
                  <MoreHorizontal
                    aria-hidden="true"
                    className="h-3.5 w-3.5 sm:h-4 sm:w-4"
                  />
                  <span className="hidden sm:inline">More</span>
                  {isSecondaryTabActive && (
                    <span className="ml-0.5 sm:ml-1 text-[10px] sm:text-xs rounded-full px-1 sm:px-1.5 py-0.5 bg-primary/15 text-primary">
                      {secondaryTabs
                        .find((t) => t.id === activeTab)
                        ?.label.slice(0, 4)}
                    </span>
                  )}
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                {secondaryTabs.map((tab) => (
                  <DropdownMenuItem
                    key={tab.id}
                    onClick={() => handleTabChange(tab.id)}
                    className={`gap-2 ${activeTab === tab.id ? 'bg-primary/10 text-primary' : ''}`}
                  >
                    <tab.icon aria-hidden="true" className="h-4 w-4" />
                    {tab.label}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>

      {/* Tab Content */}
      <div className="min-h-[200px] sm:min-h-[400px]">
        {/* Documents Tab */}
        {activeTab === 'documents' && (
          <DocumentList
            documents={projectDocuments}
            loading={documentsLoading}
            onRemove={(documentId) => {
              void handleRemoveDocument(documentId);
            }}
          />
        )}

        {/* Notes Tab */}
        {activeTab === 'notes' && (
          <div>
            <div className="flex justify-end mb-4">
              <button
                onClick={handleCreateNote}
                className="flex items-center gap-2 px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-md text-sm font-medium hover:bg-primary/20 transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                <Plus aria-hidden="true" className="h-4 w-4" />
                New note
              </button>
            </div>

            {notesLoading ? (
              <div
                role="status"
                aria-busy="true"
                aria-label="Loading notes"
                className="grid grid-cols-1 sm:grid-cols-2 gap-3"
              >
                {Array.from({ length: 4 }).map((_, i) => (
                  <div
                    key={i}
                    className="rounded-lg border border-border bg-card p-4 space-y-3"
                  >
                    <div className="h-4 w-2/3 rounded-md bg-muted animate-pulse" />
                    <div className="h-3 w-full rounded-md bg-muted animate-pulse" />
                    <div className="h-3 w-4/5 rounded-md bg-muted animate-pulse" />
                  </div>
                ))}
              </div>
            ) : (
              <NoteList
                notes={projectNotes}
                selectedTag={selectedNoteTag}
                onTagChange={setSelectedNoteTag}
                onEdit={handleEditNote}
                onDelete={(noteId) => {
                  void handleDeleteNote(noteId);
                }}
                onTogglePin={(noteId) => {
                  void handleTogglePin(noteId);
                }}
              />
            )}
          </div>
        )}

        {/* Bibliography Tab */}
        {activeTab === 'bibliography' && (
          <div>
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <span className="text-sm text-muted-foreground">Format:</span>
                <select
                  value={bibFormat}
                  aria-label="Citation format"
                  onChange={(e) => {
                    const format = e.target.value as typeof bibFormat;
                    setBibFormat(format);
                    fetchBibliography(projectId, format);
                  }}
                  className="px-3 py-1.5 bg-muted border border-border rounded-md text-sm text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                >
                  <option value="bibtex">BibTeX</option>
                  <option value="ieee">IEEE</option>
                  <option value="apa">APA</option>
                  <option value="mla">MLA</option>
                </select>
              </div>
              <button
                onClick={handleExportBibliography}
                disabled={!bibliography}
                className="flex items-center gap-2 px-4 py-2 bg-primary/10 text-primary border border-primary/30 rounded-md text-sm font-medium hover:bg-primary/20 transition-colors disabled:opacity-50 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                <Download aria-hidden="true" className="h-4 w-4" />
                Download
              </button>
            </div>

            {loading ? (
              <div
                role="status"
                aria-busy="true"
                aria-label="Loading bibliography"
                className="rounded-lg border border-border bg-card p-4 space-y-2"
              >
                <div className="h-3 w-32 rounded-md bg-muted animate-pulse mb-4" />
                {Array.from({ length: 8 }).map((_, i) => (
                  <div
                    key={i}
                    className="h-3 rounded-md bg-muted animate-pulse"
                    style={{ width: `${90 - (i % 3) * 18}%` }}
                  />
                ))}
              </div>
            ) : bibliography ? (
              <div className="bg-card border border-border rounded-lg p-4">
                <div className="flex items-center justify-between mb-3">
                  <span className="text-sm text-muted-foreground">
                    {bibliography.citation_count} citations
                  </span>
                  <span className="text-xs text-muted-foreground">
                    Generated{' '}
                    {new Date(bibliography.generated_at).toLocaleString()}
                  </span>
                </div>
                <pre className="text-sm text-foreground font-(--nous-font-mono) overflow-x-auto whitespace-pre-wrap max-h-[500px] overflow-y-auto rounded-md bg-muted/40 p-3">
                  {bibliography.content}
                </pre>
              </div>
            ) : (
              <div className="flex flex-col items-center justify-center py-16 rounded-xl border border-dashed border-border">
                <BookOpen
                  aria-hidden="true"
                  className="h-10 w-10 text-muted-foreground/50 mb-3"
                />
                <p className="text-sm font-medium text-foreground">
                  No bibliography yet
                </p>
                <p className="text-xs text-muted-foreground mt-1 max-w-sm text-center">
                  Add documents with extracted citations, then a bibliography
                  will be generated here.
                </p>
              </div>
            )}
          </div>
        )}

        {/* Drafts Tab */}
        {activeTab === 'drafts' && (
          <div className="space-y-4">
            {/* Show generation progress if active */}
            {generationTaskId && generationStatus && (
              <DraftGenerationProgress
                status={generationStatus}
                onCancel={async () => {
                  if (pollTimeoutRef.current)
                    clearTimeout(pollTimeoutRef.current);
                  await projectService.cancelGeneration(
                    projectId,
                    generationTaskId
                  );
                  setGenerationTaskId(null);
                  setGenerationStatus(null);
                }}
                onComplete={async (draftId) => {
                  if (pollTimeoutRef.current)
                    clearTimeout(pollTimeoutRef.current);
                  setGenerationTaskId(null);
                  setGenerationStatus(null);
                  try {
                    const draft = await projectService.getDraft(
                      projectId,
                      draftId
                    );
                    await loadDrafts(draft.version);
                  } catch (err) {
                    console.error('Failed to load draft:', err);
                    await loadDrafts();
                  }
                }}
              />
            )}

            {/* Show current draft or generator */}
            {!generationTaskId && (
              <div className="space-y-4">
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                  {/* Draft Generator */}
                  <div className="lg:col-span-1 space-y-4">
                    <DraftGenerator
                      loading={draftsLoading}
                      documentCount={projectDocuments.length}
                      onGenerate={async (config) => {
                        setDraftsLoading(true);
                        try {
                          const result = await projectService.generateDraft(
                            projectId,
                            config.themes,
                            {
                              style: config.style,
                              maxSections: config.maxSections,
                              includeAbstract: config.includeAbstract,
                            }
                          );
                          setGenerationTaskId(result.task_id);
                          // Poll until terminal. Transient poll failures are
                          // retried with backoff (bounded) instead of killing
                          // the loop on the first hiccup (R6-M17); exhausting
                          // the budget surfaces a terminal failed state so the
                          // progress card can never freeze silently.
                          const POLL_RETRY_LIMIT = 5;
                          let pollFailures = 0;
                          const pollStatus = async () => {
                            try {
                              const status =
                                await projectService.getGenerationStatus(
                                  projectId,
                                  result.task_id
                                );
                              pollFailures = 0;
                              setGenerationStatus(status);
                              if (
                                !['completed', 'failed', 'cancelled'].includes(
                                  status.status
                                )
                              ) {
                                pollTimeoutRef.current = setTimeout(
                                  pollStatus,
                                  1000
                                );
                              }
                            } catch {
                              pollFailures += 1;
                              if (pollFailures >= POLL_RETRY_LIMIT) {
                                setGenerationStatus({
                                  task_id: result.task_id,
                                  status: 'failed',
                                  progress: 0,
                                  current_step:
                                    'Lost contact with the generation job after repeated errors.',
                                  started_at: new Date().toISOString(),
                                });
                                return;
                              }
                              pollTimeoutRef.current = setTimeout(
                                pollStatus,
                                Math.min(1000 * 2 ** pollFailures, 8000)
                              );
                            }
                          };
                          void pollStatus();
                        } catch (err) {
                          console.error('Generation failed:', err);
                          toast.error(
                            err instanceof Error
                              ? `Failed to start draft generation: ${err.message}`
                              : 'Failed to start draft generation'
                          );
                        } finally {
                          setDraftsLoading(false);
                        }
                      }}
                    />

                    {draftVersions.length > 0 && (
                      <div className="bg-card border border-border rounded-lg p-4">
                        <h3 className="text-sm font-medium text-foreground mb-3">
                          Draft versions
                        </h3>
                        <div className="space-y-2 max-h-[220px] overflow-y-auto">
                          {draftVersions.map((draftVersion) => (
                            <button
                              key={draftVersion.id}
                              onClick={() => {
                                void handleDraftVersionChange(
                                  draftVersion.version
                                );
                              }}
                              className={`w-full text-left px-3 py-2 rounded-md border text-xs transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ${
                                currentDraft?.version === draftVersion.version
                                  ? 'bg-primary/10 border-primary/40 text-primary'
                                  : 'bg-muted border-border text-muted-foreground hover:border-muted-foreground/30'
                              }`}
                            >
                              <div className="flex items-center justify-between">
                                <span className="font-medium tabular-nums">
                                  Version {draftVersion.version}
                                </span>
                                {draftVersion.is_current && (
                                  <span className="rounded-full bg-primary/15 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                                    Current
                                  </span>
                                )}
                              </div>
                              <div className="text-[10px] text-muted-foreground mt-1 tabular-nums">
                                {new Date(
                                  draftVersion.created_at
                                ).toLocaleString()}
                              </div>
                            </button>
                          ))}
                        </div>
                      </div>
                    )}

                    {draftVersions.length > 1 && (
                      <div className="bg-card border border-border rounded-lg p-4 space-y-3">
                        <h3 className="text-sm font-medium text-foreground">
                          Compare versions
                        </h3>
                        <div className="grid grid-cols-2 gap-2">
                          <select
                            value={compareVersionA ?? ''}
                            aria-label="First version to compare"
                            onChange={(e) => {
                              setCompareVersionA(Number(e.target.value));
                              setDraftComparison(null);
                              setComparisonDraftA(null);
                              setComparisonDraftB(null);
                            }}
                            className="px-3 py-2 bg-muted border border-border rounded-md text-xs tabular-nums text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                          >
                            {draftVersions.map((draftVersion) => (
                              <option
                                key={`a-${draftVersion.id}`}
                                value={draftVersion.version}
                              >
                                v{draftVersion.version}
                              </option>
                            ))}
                          </select>
                          <select
                            value={compareVersionB ?? ''}
                            aria-label="Second version to compare"
                            onChange={(e) => {
                              setCompareVersionB(Number(e.target.value));
                              setDraftComparison(null);
                              setComparisonDraftA(null);
                              setComparisonDraftB(null);
                            }}
                            className="px-3 py-2 bg-muted border border-border rounded-md text-xs tabular-nums text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                          >
                            {draftVersions.map((draftVersion) => (
                              <option
                                key={`b-${draftVersion.id}`}
                                value={draftVersion.version}
                              >
                                v{draftVersion.version}
                              </option>
                            ))}
                          </select>
                        </div>
                        <button
                          onClick={() => {
                            void handleCompareDrafts();
                          }}
                          disabled={
                            comparisonLoading ||
                            compareVersionA === null ||
                            compareVersionB === null ||
                            compareVersionA === compareVersionB
                          }
                          aria-busy={comparisonLoading}
                          className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-primary/10 text-primary border border-primary/30 rounded-md text-xs font-medium hover:bg-primary/20 transition-colors disabled:opacity-50 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                        >
                          {comparisonLoading && (
                            <Loader2
                              aria-hidden="true"
                              className="h-3.5 w-3.5 animate-spin"
                            />
                          )}
                          {comparisonLoading ? 'Comparing…' : 'Compare drafts'}
                        </button>
                      </div>
                    )}
                  </div>

                  {/* Draft Viewer */}
                  <div className="lg:col-span-2">
                    {currentDraft ? (
                      <DraftViewer
                        draft={currentDraft}
                        versions={draftVersions.map((draftVersion) => ({
                          version: draftVersion.version,
                          created_at: draftVersion.created_at,
                        }))}
                        onVersionChange={(version) => {
                          void handleDraftVersionChange(version);
                        }}
                        onExport={async (format) => {
                          setExportInitialFormat(format);
                          setShowExportModal(true);
                        }}
                      />
                    ) : (
                      <div className="flex flex-col items-center justify-center py-16 rounded-xl border border-dashed border-border">
                        <Sparkles
                          aria-hidden="true"
                          className="h-10 w-10 text-muted-foreground/50 mb-3"
                        />
                        <p className="text-sm font-medium text-foreground">
                          No draft yet
                        </p>
                        <p className="text-xs text-muted-foreground mt-1 max-w-sm text-center">
                          Choose your themes on the left, then generate a
                          literature review draft.
                        </p>
                      </div>
                    )}
                  </div>
                </div>

                {draftComparison && (
                  <DraftComparison
                    comparison={draftComparison}
                    draftA={
                      comparisonDraftA
                        ? { content: comparisonDraftA.content }
                        : undefined
                    }
                    draftB={
                      comparisonDraftB
                        ? { content: comparisonDraftB.content }
                        : undefined
                    }
                  />
                )}
              </div>
            )}
          </div>
        )}

        {/* Chat Tab */}
        {activeTab === 'chat' && <ProjectChatTab projectId={projectId} />}

        {/* Matrix Tab */}
        {activeTab === 'matrix' && (
          <ExtractionMatrix
            projectId={projectId}
            matrixId={matrixId ?? undefined}
            documents={projectDocuments.map((doc) => ({
              id: doc.document_id,
              title:
                doc.document?.title || doc.document?.filename || 'Untitled',
            }))}
          />
        )}

        {/* Pipeline Tab */}
        {activeTab === 'pipeline' && <ResearchPipeline projectId={projectId} />}

        {/* Knowledge Tab */}
        {activeTab === 'knowledge' && (
          <ProjectKnowledgeTree projectId={projectId} />
        )}

        {activeTab === 'skills' && <ProjectSkillsTab projectId={projectId} />}
      </div>

      <NoteEditor
        isOpen={showNoteEditor}
        initialNote={editingNote}
        availableDocuments={projectDocuments}
        onClose={() => {
          setShowNoteEditor(false);
          setEditingNote(null);
        }}
        onSave={handleSaveNote}
      />

      {currentDraft && (
        <DraftExportModal
          isOpen={showExportModal}
          onClose={() => setShowExportModal(false)}
          draftTitle={currentDraft.title}
          initialFormat={exportInitialFormat}
          onExport={async (format, includeBibliography, bibliographyFormat) => {
            await projectService.downloadDraftExport(
              projectId,
              currentDraft.id,
              format,
              includeBibliography,
              bibliographyFormat
            );
          }}
        />
      )}

      <DocumentUploadWizard
        isOpen={showUploadWizard}
        onClose={() => setShowUploadWizard(false)}
        onComplete={handleUploadComplete}
      />

      <AlertDialog
        open={deleteNoteDialogOpen}
        onOpenChange={setDeleteNoteDialogOpen}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete note?</AlertDialogTitle>
            <AlertDialogDescription>
              This will permanently delete this note. This action cannot be
              undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmDeleteNote}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
