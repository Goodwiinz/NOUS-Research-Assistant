'use client';

/**
 * DraftStep - Step 4 of the Research Pipeline
 * Draft generation + viewer with a collapsible RAG chat side panel.
 * Completion criteria: draft generated.
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  ArrowRight,
  ArrowLeft,
  MessageSquare,
  X,
  Sparkles,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { DraftGenerator } from '@/components/research/DraftGenerator';
import { DraftViewer } from '@/components/research/DraftViewer';
import { DraftGenerationProgress } from '@/components/research/DraftGenerationProgress';
import { ProjectChatTab } from '@/components/research/ProjectChatTab';
import {
  projectService,
  type Draft,
  type GenerationStatus,
} from '@/services/projectService';
import { useDraftReviews } from '@/hooks/useDraftReviews';
import { DraftReviewSummary } from '@/components/research/DraftReviewSummary';

interface DraftStepProps {
  projectId: string;
  documentCount: number;
  onContinue: () => void;
  onBack: () => void;
}

export const DraftStep: React.FC<DraftStepProps> = ({
  projectId,
  documentCount,
  onContinue,
  onBack,
}) => {
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
  const [chatOpen, setChatOpen] = useState(false);
  const draftReviews = useDraftReviews(projectId);
  const latestReview = draftReviews.data?.reviews[0] ?? null;

  const loadDrafts = useCallback(
    async (preferredVersion?: number) => {
      setDraftsLoading(true);
      try {
        const response = await projectService.listDrafts(projectId, {
          limit: 50,
        });
        const versions = response.drafts.map((draft) => ({
          id: draft.id,
          version: draft.version,
          created_at: draft.created_at,
          is_current: draft.is_current,
        }));
        setDraftVersions(versions);

        if (versions.length === 0) {
          setCurrentDraft(null);
          return;
        }

        const selectedVersion =
          preferredVersion ??
          versions.find((d) => d.is_current)?.version ??
          versions[0].version;
        const selectedMeta =
          versions.find((d) => d.version === selectedVersion) ?? versions[0];
        const draft = await projectService.getDraft(projectId, selectedMeta.id);
        setCurrentDraft(draft);
      } catch (err) {
        console.error('Failed to load drafts:', err);
      } finally {
        setDraftsLoading(false);
      }
    },
    [projectId]
  );

  useEffect(() => {
    void Promise.resolve().then(() => loadDrafts());
  }, [loadDrafts]);

  const hasDraft = currentDraft !== null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-foreground">
            Generate draft
          </h3>
          <p className="text-sm text-muted-foreground mt-1">
            Generate a literature review draft from your project documents and
            citations.
          </p>
        </div>
        <button
          onClick={() => setChatOpen(!chatOpen)}
          className={`flex items-center gap-2 px-3 py-1.5 rounded text-xs transition-colors ${
            chatOpen
              ? 'bg-accent text-accent-foreground border border-border'
              : 'text-muted-foreground border border-border hover:text-foreground hover:border-muted-foreground/50'
          }`}
        >
          {chatOpen ? (
            <X className="h-3.5 w-3.5" />
          ) : (
            <MessageSquare className="h-3.5 w-3.5" />
          )}
          {chatOpen ? 'Close chat' : 'RAG chat'}
        </button>
      </div>

      {/* Generation progress */}
      {generationTaskId && generationStatus && (
        <DraftGenerationProgress
          status={generationStatus}
          onCancel={async () => {
            await projectService.cancelGeneration(projectId, generationTaskId);
            setGenerationTaskId(null);
            setGenerationStatus(null);
          }}
          onComplete={async (draftId) => {
            setGenerationTaskId(null);
            setGenerationStatus(null);
            try {
              const draft = await projectService.getDraft(projectId, draftId);
              await loadDrafts(draft.version);
            } catch {
              await loadDrafts();
            }
          }}
        />
      )}

      {/* Main content: draft generator/viewer + optional chat panel */}
      {!generationTaskId && (
        <>
          {latestReview && <DraftReviewSummary review={latestReview} />}
          <div
            className={`grid gap-4 ${chatOpen ? 'grid-cols-1 lg:grid-cols-3' : 'grid-cols-1'}`}
          >
            <div className={chatOpen ? 'lg:col-span-2' : ''}>
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                <div className="lg:col-span-1">
                  <DraftGenerator
                    loading={draftsLoading}
                    documentCount={documentCount}
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
                        const pollStatus = async (): Promise<void> => {
                          try {
                            const status =
                              await projectService.getGenerationStatus(
                                projectId,
                                result.task_id
                              );
                            setGenerationStatus(status);
                            if (
                              ![
                                'completed',
                                'failed',
                                'cancelled',
                                'interrupted',
                              ].includes(status.status)
                            ) {
                              setTimeout(pollStatus, 1000);
                            } else {
                              await draftReviews.refetch();
                              if (status.status !== 'completed') {
                                setGenerationTaskId(null);
                                setGenerationStatus(null);
                              }
                            }
                          } catch (err) {
                            console.error('Poll error:', err);
                          }
                        };
                        pollStatus();
                      } catch (err) {
                        console.error('Generation failed:', err);
                      } finally {
                        setDraftsLoading(false);
                      }
                    }}
                  />
                </div>
                <div className="lg:col-span-2">
                  {currentDraft ? (
                    <DraftViewer
                      draft={currentDraft}
                      versions={draftVersions.map((v) => ({
                        version: v.version,
                        created_at: v.created_at,
                      }))}
                      onVersionChange={(version) => {
                        void loadDrafts(version);
                      }}
                    />
                  ) : (
                    <div className="bg-card border border-dashed border-border rounded-lg p-12 text-center">
                      <Sparkles className="h-12 w-12 text-muted-foreground/50 mx-auto mb-4" />
                      <p className="text-muted-foreground">
                        No draft generated yet
                      </p>
                      <p className="text-sm text-muted-foreground/70 mt-2">
                        Configure themes and generate a literature review draft
                      </p>
                    </div>
                  )}
                </div>
              </div>
            </div>

            {/* Collapsible chat side panel */}
            {chatOpen && (
              <div className="lg:col-span-1 bg-card border border-border rounded-lg overflow-hidden">
                <div className="p-2 border-b border-border flex items-center justify-between">
                  <span className="text-xs text-muted-foreground">
                    RAG chat
                  </span>
                  <button
                    onClick={() => setChatOpen(false)}
                    className="p-1 text-muted-foreground hover:text-foreground"
                    aria-label="Close chat panel"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
                <div className="h-[500px]">
                  <ProjectChatTab projectId={projectId} />
                </div>
              </div>
            )}
          </div>
        </>
      )}

      <div className="flex items-center justify-between pt-4 border-t border-border">
        <Button variant="ghost" onClick={onBack}>
          <ArrowLeft className="h-4 w-4 mr-2" />
          Back
        </Button>
        <Button onClick={onContinue} disabled={!hasDraft}>
          Continue
          <ArrowRight className="h-4 w-4 ml-2" />
        </Button>
      </div>
    </div>
  );
};
