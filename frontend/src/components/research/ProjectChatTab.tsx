'use client';

/**
 * ProjectChatTab Component
 * Main tab in project detail view for managing linked chat threads
 */

import React, { useEffect, useState } from 'react';
import {
  MessageSquare,
  Plus,
  Loader2,
  AlertCircle,
  RefreshCw,
  Link2,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useProjectChat } from '@/hooks/useProjectChat';
import { useProjectChatStore } from '@/store/projectChatStore';
import { ThreadCard } from './ThreadCard';
import { StartChatModal } from './StartChatModal';
import { LinkThreadModal } from './LinkThreadModal';
import { SaveToNoteModal } from './SaveToNoteModal';
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

export interface ProjectChatTabProps {
  projectId: string;
}

export const ProjectChatTab: React.FC<ProjectChatTabProps> = ({
  projectId,
}) => {
  const {
    threads,
    isLoading,
    error,
    startChat,
    linkThread,
    unlinkThread,
    saveToNote,
    refreshThreads,
    clearError,
  } = useProjectChat(projectId);

  const [isStartChatModalOpen, setIsStartChatModalOpen] = useState(false);
  const [isLinkThreadModalOpen, setIsLinkThreadModalOpen] = useState(false);
  const [isSaveToNoteModalOpen, setIsSaveToNoteModalOpen] = useState(false);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [projectWorkspaceId, setProjectWorkspaceId] = useState<string | null>(
    null
  );

  const [unlinkDialogOpen, setUnlinkDialogOpen] = useState(false);
  const [threadToUnlink, setThreadToUnlink] = useState<string | null>(null);
  const [unlinkError, setUnlinkError] = useState<string | null>(null);
  const [isUnlinking, setIsUnlinking] = useState(false);

  /**
   * The store swallows request failures: it writes the message to
   * `errors[projectId]` and resolves normally. That slot also drives this
   * tab's "Failed to load threads" screen, so an action failure must be
   * consumed here (read + clear) and surfaced in the dialog that caused it,
   * or the whole tab hijacks it as a load failure and the dialog unmounts.
   */
  const takeActionError = (fallback: string): string => {
    const message =
      useProjectChatStore.getState().errors[projectId] || fallback;
    clearError();
    return message;
  };

  useEffect(() => {
    if (projectId && projectId !== 'undefined') {
      refreshThreads();
    }
  }, [projectId, refreshThreads]);

  useEffect(() => {
    async function fetchProjectWorkspace() {
      if (!projectId || projectId === 'undefined') return;
      try {
        const { projectService } = await import('@/services/projectService');
        const project = await projectService.getProject(projectId);
        setProjectWorkspaceId(project.workspace_id);
      } catch (err) {
        console.error('Failed to fetch project workspace:', err);
      }
    }
    fetchProjectWorkspace();
  }, [projectId]);

  const handleStartChat = async (
    initialMessage: string,
    threadTitle?: string
  ) => {
    await startChat({
      initial_message: initialMessage,
      thread_title: threadTitle,
    });
    setIsStartChatModalOpen(false);
  };

  const handleUnlinkClick = (threadId: string) => {
    setThreadToUnlink(threadId);
    setUnlinkError(null);
    setUnlinkDialogOpen(true);
  };

  const confirmUnlink = async (event: React.MouseEvent) => {
    // AlertDialogAction closes the dialog on click by default; keep it open
    // until the request has actually succeeded.
    event.preventDefault();
    if (!threadToUnlink || isUnlinking) return;
    setUnlinkError(null);
    setIsUnlinking(true);
    try {
      await unlinkThread(threadToUnlink);
      // `unlinkThreadFromProject` resolves void either way; the store's error
      // slot is the only success signal it exposes.
      if (useProjectChatStore.getState().errors[projectId]) {
        setUnlinkError(takeActionError('Failed to unlink thread'));
        return;
      }
      setThreadToUnlink(null);
      setUnlinkDialogOpen(false);
    } finally {
      setIsUnlinking(false);
    }
  };

  const handleSaveToNoteClick = (threadId: string) => {
    setSelectedThreadId(threadId);
    setIsSaveToNoteModalOpen(true);
  };

  const handleSaveToNote = async (
    noteTitle: string,
    includeCitations: boolean
  ) => {
    if (selectedThreadId) {
      const result = await saveToNote({
        thread_id: selectedThreadId,
        note_title: noteTitle,
        include_citations: includeCitations,
      });
      if (!result) {
        // Throwing keeps SaveToNoteModal open with the user's title and lets
        // it render the message inline.
        throw new Error(takeActionError('Failed to save thread to note'));
      }
      setSelectedThreadId(null);
      setIsSaveToNoteModalOpen(false);
    }
  };

  const handleLinkThread = async (threadId: string, contextNote?: string) => {
    const result = await linkThread({
      thread_id: threadId,
      context_note: contextNote,
    });
    if (!result) {
      throw new Error(takeActionError('Failed to link thread'));
    }
    setIsLinkThreadModalOpen(false);
    refreshThreads();
  };

  const handleRetry = () => {
    clearError();
    refreshThreads();
  };

  // The three content states below are rendered INSIDE a single tree with the
  // dialogs, never as early returns. An action failure writes the store's
  // error slot before the handler can consume it, which momentarily switches
  // the content to the error screen; if the dialogs lived inside that branch
  // they would unmount and lose the user's input and the error they were
  // about to show.
  let content: React.ReactNode;

  if (isLoading && threads.length === 0) {
    content = (
      <div className="flex items-center justify-center py-12">
        <div className="flex flex-col items-center gap-3">
          <Loader2 className="h-8 w-8 animate-spin text-primary" />
          <p className="text-sm text-muted-foreground">Loading threads…</p>
        </div>
      </div>
    );
  } else if (error) {
    content = (
      <div className="flex items-center justify-center py-12">
        <div className="flex flex-col items-center gap-4 max-w-md text-center">
          <div className="p-3 rounded-full bg-destructive/10 border border-destructive/30">
            <AlertCircle className="h-8 w-8 text-destructive" />
          </div>
          <div>
            <h3 className="text-lg font-semibold text-foreground mb-2">
              Failed to load threads
            </h3>
            <p className="text-sm text-muted-foreground mb-4">{error}</p>
            <Button
              variant="outline"
              onClick={handleRetry}
              className="mx-auto"
            >
              <RefreshCw className="h-4 w-4 mr-2" />
              Retry
            </Button>
          </div>
        </div>
      </div>
    );
  } else if (threads.length === 0) {
    content = (
      <div className="flex items-center justify-center py-12">
        <div className="flex flex-col items-center gap-4 max-w-md text-center">
          <div className="p-4 rounded-full bg-muted border border-border">
            <MessageSquare className="h-12 w-12 text-muted-foreground" />
          </div>
          <div>
            <h3 className="text-lg font-semibold text-foreground mb-2">
              No chat threads linked
            </h3>
            <p className="text-sm text-muted-foreground mb-4">
              Start a new chat using this project&apos;s documents as
              context, or link an existing thread.
            </p>
            <div className="flex items-center gap-3 justify-center">
              <Button
                onClick={() => setIsStartChatModalOpen(true)}
                variant="outline"
              >
                <Plus className="h-4 w-4 mr-2" />
                Start chat
              </Button>
              <Button
                onClick={() => setIsLinkThreadModalOpen(true)}
                variant="secondary"
              >
                <Link2 className="h-4 w-4 mr-2" />
                Link existing
              </Button>
            </div>
          </div>
        </div>
      </div>
    );
  } else {
    content = (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <MessageSquare className="h-5 w-5 text-primary" />
            <h3 className="text-lg font-semibold text-foreground">
              Linked threads ({threads.length})
            </h3>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setIsLinkThreadModalOpen(true)}
            >
              <Link2 className="h-4 w-4 mr-2" />
              Link existing
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setIsStartChatModalOpen(true)}
            >
              <Plus className="h-4 w-4 mr-2" />
              Start chat
            </Button>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {threads.map((thread) => (
            <ThreadCard
              key={thread.id}
              thread={thread}
              onUnlink={handleUnlinkClick}
              onSaveToNote={handleSaveToNoteClick}
            />
          ))}
        </div>
      </div>
    );
  }

  return (
    <>
      {content}

      <StartChatModal
        isOpen={isStartChatModalOpen}
        onClose={() => setIsStartChatModalOpen(false)}
        onStartChat={handleStartChat}
      />

      <SaveToNoteModal
        isOpen={isSaveToNoteModalOpen}
        onClose={() => {
          setIsSaveToNoteModalOpen(false);
          setSelectedThreadId(null);
        }}
        onSave={handleSaveToNote}
      />

      {projectWorkspaceId && (
        <LinkThreadModal
          isOpen={isLinkThreadModalOpen}
          projectWorkspaceId={projectWorkspaceId}
          onClose={() => setIsLinkThreadModalOpen(false)}
          onLinkThread={handleLinkThread}
        />
      )}

      <AlertDialog open={unlinkDialogOpen} onOpenChange={setUnlinkDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Unlink thread from project?</AlertDialogTitle>
            <AlertDialogDescription>
              This will remove the connection between this thread and the
              project. The thread and its messages will not be deleted.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {unlinkError && (
            <div
              role="alert"
              className="p-3 bg-destructive/10 border border-destructive/30 rounded text-sm text-destructive"
            >
              {unlinkError}
            </div>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isUnlinking}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmUnlink}
              disabled={isUnlinking}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {isUnlinking ? 'Unlinking…' : 'Unlink'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
};

export default ProjectChatTab;
