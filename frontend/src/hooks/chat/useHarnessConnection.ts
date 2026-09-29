'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { agentChatService } from '@/services/agentChatService';
import { harnessService } from '@/services/harnessService';
import type {
  HarnessDevice,
  HarnessWorkspace,
  NativeDecision,
  NativeRequestView,
} from '@/services/harnessService';
import { useAuthStore } from '@/stores/authStore';

export type HarnessProvider = 'nous' | 'codex';
export type HarnessConnectionState =
  'idle' | 'connecting' | 'connected' | 'lost' | 'reconciling' | 'stopping';

interface HarnessSelection {
  executionProvider: HarnessProvider;
  deviceId: string | null;
  workspaceId: string | null;
}

const EMPTY_SELECTION: HarnessSelection = {
  executionProvider: 'nous',
  deviceId: null,
  workspaceId: null,
};

interface HarnessRuntimeState {
  connectionState: HarnessConnectionState;
  runId: string | null;
  pendingRequests: NativeRequestView[];
}

const EMPTY_RUNTIME_STATE: HarnessRuntimeState = {
  connectionState: 'idle',
  runId: null,
  pendingRequests: [],
};

function selectionKey(userId: string | null, threadId: string | null): string {
  return `nous:harness-selection:${userId ?? 'anonymous'}:${threadId ?? 'draft'}`;
}

function readSelection(key: string): HarnessSelection {
  if (typeof window === 'undefined') return EMPTY_SELECTION;
  try {
    const value = JSON.parse(window.localStorage.getItem(key) ?? 'null');
    if (!value || typeof value !== 'object') return EMPTY_SELECTION;
    return {
      executionProvider: value.executionProvider === 'codex' ? 'codex' : 'nous',
      deviceId: typeof value.deviceId === 'string' ? value.deviceId : null,
      workspaceId:
        typeof value.workspaceId === 'string' ? value.workspaceId : null,
    };
  } catch {
    return EMPTY_SELECTION;
  }
}

function hasStoredSelection(key: string): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(key) !== null;
  } catch {
    return false;
  }
}

function isTerminal(type: string): boolean {
  return type === 'done' || type === 'error' || type === 'confirmation';
}

export interface HarnessConnectionController extends HarnessSelection {
  devices: HarnessDevice[];
  workspaces: HarnessWorkspace[];
  pendingRequests: NativeRequestView[];
  connectionState: HarnessConnectionState;
  statusLabel: string;
  disabledReason: string | null;
  canSend: boolean;
  runId: string | null;
  selectProvider(provider: HarnessProvider): void;
  selectDevice(deviceId: string | null): void;
  selectWorkspace(workspaceId: string | null): void;
  /** Carry the draft selection over to a thread this draft just created. */
  adoptDraftSelection(newThreadId: string): void;
  receive(
    event: { type: string; runId?: string; detail?: string },
    targetThreadId?: string | null
  ): void;
  markConnectionLost(targetThreadId?: string | null): void;
  loadApproval(
    requestId: string,
    targetThreadId?: string | null
  ): Promise<NativeRequestView>;
  decideRequest(requestId: string, decision: NativeDecision): Promise<void>;
  stop(): Promise<void>;
}

/**
 * Thread-owned bridge controls. Only display preferences are persisted in the
 * browser; grant tokens and native process state remain server/device-owned.
 */
export function useHarnessConnection(
  threadId: string | null
): HarnessConnectionController {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const queryClient = useQueryClient();
  const key = selectionKey(userId, threadId);
  const draftKey = selectionKey(userId, null);
  const [selections, setSelections] = useState<
    Record<string, HarnessSelection>
  >(() => ({ [key]: readSelection(key) }));
  const selection = selections[key] ?? readSelection(key);
  const [runtimeByKey, setRuntimeByKey] = useState<
    Record<string, HarnessRuntimeState>
  >({});
  const runtime = runtimeByKey[key] ?? EMPTY_RUNTIME_STATE;
  const { connectionState, runId, pendingRequests } = runtime;

  // Adoption is an explicit call from the send path that created the thread,
  // not inferred from threadId going null -> id: that transition also happens
  // when a draft user opens an existing thread, which must not inherit the
  // draft's Codex selection.
  const adoptDraftSelection = useCallback(
    (newThreadId: string) => {
      const newKey = selectionKey(userId, newThreadId);
      const draftSelection = selections[draftKey] ?? readSelection(draftKey);
      setSelections((current) => ({ ...current, [newKey]: draftSelection }));
      try {
        window.localStorage.setItem(newKey, JSON.stringify(draftSelection));
      } catch {
        // Display preference only; state above still applies this session.
      }
    },
    [draftKey, selections, userId]
  );

  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(key, JSON.stringify(selection));
  }, [key, selection]);

  const patchRuntime = useCallback(
    (
      patch:
        | Partial<HarnessRuntimeState>
        | ((current: HarnessRuntimeState) => Partial<HarnessRuntimeState>),
      targetThreadId?: string | null
    ) => {
      const targetKey = selectionKey(
        userId,
        targetThreadId === undefined ? threadId : targetThreadId
      );
      setRuntimeByKey((current) => ({
        ...current,
        [targetKey]: {
          ...(current[targetKey] ?? EMPTY_RUNTIME_STATE),
          ...(typeof patch === 'function'
            ? patch(current[targetKey] ?? EMPTY_RUNTIME_STATE)
            : patch),
        },
      }));
    },
    [threadId, userId]
  );

  const devicesQuery = useQuery({
    queryKey: ['harness', 'devices', userId],
    queryFn: () => harnessService.listDevices(),
    enabled: Boolean(userId && selection.executionProvider === 'codex'),
    staleTime: 30_000,
  });
  const workspacesQuery = useQuery({
    queryKey: ['harness', 'workspaces', userId, selection.deviceId],
    queryFn: () => harnessService.listWorkspaces(selection.deviceId!),
    enabled: Boolean(
      userId &&
        selection.executionProvider === 'codex' &&
        selection.deviceId
    ),
    staleTime: 30_000,
  });

  const stopMutation = useMutation({
    mutationFn: async () => {
      if (!threadId || !runId) throw new Error('No active harness run to stop');
      await agentChatService.cancelActiveRun(threadId, runId);
    },
  });
  const decisionMutation = useMutation({
    mutationFn: async ({
      requestId,
      decision,
    }: {
      requestId: string;
      decision: NativeDecision;
    }) => harnessService.decideRequest(requestId, decision),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ['harness', 'requests'] });
    },
  });

  const updateSelection = useCallback(
    (next: Partial<HarnessSelection>) =>
      setSelections((current) => ({
        ...current,
        [key]: { ...(current[key] ?? selection), ...next },
      })),
    [key, selection, setSelections]
  );
  const receive = useCallback(
    (
      event: { type: string; runId?: string; detail?: string },
      targetThreadId?: string | null
    ) => {
      const patch: Partial<HarnessRuntimeState> = {};
      if (event.runId) patch.runId = event.runId;
      if (
        event.type === 'stopping' ||
        event.detail?.toLowerCase().includes('stop requested')
      ) {
        patch.connectionState = 'stopping';
      } else if (event.type === 'accepted' || event.type === 'status') {
        patch.connectionState = 'connected';
      } else if (event.type === 'interrupt_ack') {
        // Receipt means the targeted interrupt reached the device. It is not
        // evidence that Codex stopped; keep the composer locked until terminal.
        patch.connectionState = 'stopping';
      } else if (event.type === 'reconciling') {
        patch.connectionState = 'reconciling';
      } else if (isTerminal(event.type)) {
        patch.connectionState = 'idle';
        patch.runId = null;
      }
      patchRuntime(patch, targetThreadId);
    },
    [patchRuntime]
  );
  const markConnectionLost = useCallback(
    (targetThreadId?: string | null) => {
      patchRuntime({ connectionState: 'lost' }, targetThreadId);
    },
    [patchRuntime]
  );
  const loadApproval = useCallback(
    async (requestId: string, targetThreadId?: string | null) => {
      const request = await harnessService.readRequest(requestId);
      if (!request.consumed && !request.expired) {
        patchRuntime(
          (current) => ({
            pendingRequests: [
              ...current.pendingRequests.filter(
                (item) => item.id !== request.id
              ),
              request,
            ],
          }),
          targetThreadId
        );
      }
      return request;
    },
    [patchRuntime]
  );
  const decideRequest = useCallback(
    async (requestId: string, decision: NativeDecision) => {
      // Fetch the challenge immediately before deciding so a changed target
      // cannot inherit an earlier approval click.
      const current = await harnessService.readRequest(requestId);
      if (current.consumed || current.expired) {
        throw new Error('This native request is no longer available.');
      }
      await decisionMutation.mutateAsync({
        requestId,
        decision: { ...decision, targetHash: current.targetHash },
      });
      patchRuntime((current) => ({
        pendingRequests: current.pendingRequests.filter(
          (request) => request.id !== requestId
        ),
      }));
    },
    [decisionMutation, patchRuntime]
  );
  const stop = useCallback(async () => {
    if (!threadId || !runId) return;
    patchRuntime({ connectionState: 'stopping' });
    await stopMutation.mutateAsync();
  }, [patchRuntime, runId, stopMutation, threadId]);

  const deviceExists = devicesQuery.data?.some(
    (device) => device.id === selection.deviceId
  );
  const workspaceExists = workspacesQuery.data?.some(
    (workspace) => workspace.workspace_id === selection.workspaceId
  );
  const disabledReason =
    selection.executionProvider === 'nous'
      ? null
      : !userId
        ? 'Sign in to connect Codex.'
        : !devicesQuery.data?.length
          ? 'Pair this computer with NOUS before using Codex.'
          : !selection.deviceId || !deviceExists
            ? 'Choose a paired computer.'
            : !workspacesQuery.data?.length
              ? 'Bind a project workspace to this computer.'
              : !selection.workspaceId || !workspaceExists
                ? 'Choose a bound project workspace.'
                : null;
  const blocked =
    connectionState === 'lost' ||
    connectionState === 'reconciling' ||
    connectionState === 'stopping';
  const statusLabel =
    connectionState === 'lost'
      ? 'Connection lost — checking execution state'
      : connectionState === 'reconciling'
        ? 'Reconciling execution state'
        : connectionState === 'stopping'
          ? 'Stopping'
          : connectionState === 'connected'
            ? 'Connected'
            : selection.executionProvider === 'codex'
              ? (disabledReason ?? 'Codex ready')
              : 'NOUS';

  return useMemo(
    () => ({
      ...selection,
      devices: devicesQuery.data ?? [],
      workspaces: workspacesQuery.data ?? [],
      pendingRequests,
      connectionState,
      statusLabel,
      disabledReason,
      canSend: !blocked && !disabledReason && !runId,
      runId,
      selectProvider: (executionProvider: HarnessProvider) =>
        updateSelection({ executionProvider }),
      selectDevice: (deviceId: string | null) =>
        updateSelection({ deviceId, workspaceId: null }),
      selectWorkspace: (workspaceId: string | null) =>
        updateSelection({ workspaceId }),
      adoptDraftSelection,
      receive,
      markConnectionLost,
      loadApproval,
      decideRequest,
      stop,
    }),
    [
      adoptDraftSelection,
      blocked,
      connectionState,
      decideRequest,
      devicesQuery.data,
      disabledReason,
      loadApproval,
      markConnectionLost,
      pendingRequests,
      receive,
      runId,
      selection,
      statusLabel,
      stop,
      updateSelection,
      workspacesQuery.data,
    ]
  );
}
