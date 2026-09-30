'use client';

import React, { useState, useEffect } from 'react';
import { Link2, Loader2, Search, MessageSquare, Clock } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { workspaceService } from '@/services/workspaceService';
import type { Thread } from '@/types/workspace';

export interface LinkThreadModalProps {
  isOpen: boolean;
  projectWorkspaceId: string;
  onClose: () => void;
  onLinkThread: (threadId: string, contextNote?: string) => Promise<void>;
}

export const LinkThreadModal: React.FC<LinkThreadModalProps> = ({
  isOpen,
  projectWorkspaceId,
  onClose,
  onLinkThread,
}) => {
  const [threads, setThreads] = useState<Thread[]>([]);
  const [filteredThreads, setFilteredThreads] = useState<Thread[]>([]);
  const [isLoadingThreads, setIsLoadingThreads] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [contextNote, setContextNote] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen && projectWorkspaceId) {
      fetchThreads();
    }
  }, [isOpen, projectWorkspaceId]);

  useEffect(() => {
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      setFilteredThreads(
        threads.filter(
          (t) =>
            t.title?.toLowerCase().includes(query) ||
            t.id.toLowerCase().includes(query)
        )
      );
    } else {
      setFilteredThreads(threads);
    }
  }, [searchQuery, threads]);

  const fetchThreads = async () => {
    setIsLoadingThreads(true);
    setError(null);
    try {
      const conversationsResponse = await workspaceService.listConversations(
        projectWorkspaceId,
        { limit: 100 }
      );

      const allThreads: Thread[] = [];
      for (const conv of conversationsResponse.conversations) {
        try {
          const threadsResponse = await workspaceService.listThreads(conv.id, {
            limit: 50,
          });
          allThreads.push(...threadsResponse.threads);
        } catch (err) {
          console.warn(`Failed to fetch threads for conversation ${conv.id}`);
        }
      }

      allThreads.sort(
        (a, b) =>
          new Date(b.last_message_at || b.created_at).getTime() -
          new Date(a.last_message_at || a.created_at).getTime()
      );

      setThreads(allThreads);
      setFilteredThreads(allThreads);
    } catch (err) {
      console.error('Failed to fetch threads:', err);
      setError('Failed to load threads');
    } finally {
      setIsLoadingThreads(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!selectedThreadId) {
      setError('Please select a thread to link');
      return;
    }

    setIsSubmitting(true);
    try {
      await onLinkThread(selectedThreadId, contextNote.trim() || undefined);
      setSelectedThreadId(null);
      setContextNote('');
      setSearchQuery('');
      onClose();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '';
      if (message.includes('already linked')) {
        setError('This thread is already linked to this project');
      } else {
        setError(message || 'Failed to link thread');
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleOpenChange = (open: boolean) => {
    if (!open && !isSubmitting) {
      setSelectedThreadId(null);
      setContextNote('');
      setSearchQuery('');
      setError(null);
      onClose();
    }
  };

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr);
    return date.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  return (
    <Dialog open={isOpen} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-2xl max-h-[80vh] flex flex-col">
        <DialogHeader className="shrink-0">
          <DialogTitle className="flex items-center gap-2">
            <Link2 className="h-5 w-5 text-primary" />
            Link existing thread
          </DialogTitle>
          <DialogDescription>
            Select a thread from your workspace to link to this project
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={handleSubmit}
          className="flex flex-col flex-1 overflow-hidden"
        >
          <div className="space-y-4 overflow-y-auto flex-1">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search threads..."
                disabled={isSubmitting}
                className="pl-10"
              />
            </div>

            <div className="space-y-2 max-h-64 overflow-y-auto">
              {isLoadingThreads ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="h-6 w-6 animate-spin text-primary" />
                </div>
              ) : filteredThreads.length === 0 ? (
                <div className="text-center py-8 text-muted-foreground text-sm">
                  {threads.length === 0
                    ? 'No threads found in this workspace'
                    : 'No threads match your search'}
                </div>
              ) : (
                filteredThreads.map((thread) => (
                  <button
                    key={thread.id}
                    type="button"
                    onClick={() => setSelectedThreadId(thread.id)}
                    disabled={isSubmitting}
                    className={`w-full p-3 rounded-lg border text-left transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring ${
                      selectedThreadId === thread.id
                        ? 'bg-primary/10 border-primary/50'
                        : 'bg-card border-border hover:border-primary/30'
                    } disabled:opacity-50`}
                  >
                    <div className="flex items-start gap-3">
                      <MessageSquare
                        className={`h-4 w-4 mt-0.5 shrink-0 ${
                          selectedThreadId === thread.id
                            ? 'text-primary'
                            : 'text-muted-foreground'
                        }`}
                      />
                      <div className="flex-1 min-w-0">
                        <p
                          className={`text-sm truncate ${
                            selectedThreadId === thread.id
                              ? 'text-primary font-medium'
                              : 'text-foreground'
                          }`}
                        >
                          {thread.title || 'Untitled thread'}
                        </p>
                        <div className="flex items-center gap-3 mt-1 text-xs text-muted-foreground">
                          <span>{thread.message_count} messages</span>
                          <span className="flex items-center gap-1">
                            <Clock className="h-3 w-3" />
                            {formatDate(
                              thread.last_message_at || thread.created_at
                            )}
                          </span>
                        </div>
                      </div>
                      {selectedThreadId === thread.id && (
                        <div className="w-2 h-2 rounded-full bg-primary shrink-0 mt-1.5" />
                      )}
                    </div>
                  </button>
                ))
              )}
            </div>

            <div>
              <Label htmlFor="context-note" className="mb-1.5 block">
                Context note <span className="text-muted-foreground">(optional)</span>
              </Label>
              <textarea
                id="context-note"
                value={contextNote}
                onChange={(e) => setContextNote(e.target.value)}
                placeholder="Add a note about why this thread is linked..."
                disabled={isSubmitting}
                maxLength={500}
                className="w-full h-20 px-3 py-2 bg-background border border-border rounded text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 resize-none"
              />
              <p className="text-xs text-muted-foreground mt-1 text-right">
                {contextNote.length}/500
              </p>
            </div>

            {error && (
              <div
                role="alert"
                className="p-3 bg-destructive/10 border border-destructive/30 rounded text-sm text-destructive"
              >
                {error}
              </div>
            )}
          </div>

          <DialogFooter className="shrink-0 mt-4">
            <Button
              type="button"
              variant="outline"
              onClick={handleOpenChange.bind(null, false)}
              disabled={isSubmitting}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={isSubmitting || !selectedThreadId}
            >
              {isSubmitting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                  Linking...
                </>
              ) : (
                <>
                  <Link2 className="h-4 w-4 mr-2" />
                  Link thread
                </>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
};

export default LinkThreadModal;
