'use client';

import { useState, type ReactElement } from 'react';
import { useHarnessConnection } from '@/hooks/chat/useHarnessConnection';
import type { NativeDecision } from '@/services/harnessService';

interface HarnessSelectorProps {
  controller: ReturnType<typeof useHarnessConnection>;
  disabled?: boolean;
}

function requestLabel(request: {
  method: string;
  target: Record<string, unknown>;
}): string {
  const target = request.target;
  if (typeof target.command === 'string') return target.command;
  if (typeof target.reason === 'string') return target.reason;
  if (Array.isArray(target.questions)) return 'Codex is asking for input';
  return 'Codex needs permission to continue';
}

export function HarnessSelector({
  controller,
  disabled = false,
}: HarnessSelectorProps): ReactElement {
  const [busyRequestId, setBusyRequestId] = useState<string | null>(null);
  const [answers, setAnswers] = useState<
    Record<string, Record<string, string>>
  >({});
  const {
    executionProvider,
    devices,
    workspaces,
    pendingRequests,
    statusLabel,
    disabledReason,
    selectProvider,
    selectDevice,
    selectWorkspace,
    decideRequest,
  } = controller;

  const decide = async (requestId: string, allow: boolean): Promise<void> => {
    setBusyRequestId(requestId);
    try {
      const decision: NativeDecision = { kind: 'decision', allow };
      await decideRequest(requestId, decision);
    } finally {
      setBusyRequestId(null);
    }
  };
  const answer = async (
    requestId: string,
    questions: Array<Record<string, unknown>>
  ): Promise<void> => {
    const response = answers[requestId] ?? {};
    const exactAnswers: Record<string, string[]> = {};
    for (const question of questions) {
      if (typeof question.id !== 'string' || !response[question.id]?.trim()) {
        return;
      }
      exactAnswers[question.id] = [response[question.id].trim()];
    }
    setBusyRequestId(requestId);
    try {
      await decideRequest(requestId, {
        kind: 'answers',
        answers: exactAnswers,
      });
    } finally {
      setBusyRequestId(null);
    }
  };
  const updateAnswer = (
    requestId: string,
    questionId: string,
    value: string
  ): void => {
    setAnswers((current) => ({
      ...current,
      [requestId]: { ...current[requestId], [questionId]: value },
    }));
  };

  return (
    <div
      className="flex min-w-0 items-center gap-2"
      data-testid="harness-selector"
    >
      <label className="sr-only" htmlFor="execution-provider">
        Execution provider
      </label>
      <select
        id="execution-provider"
        aria-label="Execution provider"
        value={executionProvider}
        disabled={disabled}
        onChange={(event) =>
          selectProvider(event.target.value === 'codex' ? 'codex' : 'nous')
        }
        className="h-8 rounded-md border border-(--nous-border-1) bg-(--nous-bg-1) px-2 text-xs text-(--nous-fg-1) focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
      >
        <option value="nous">NOUS</option>
        <option value="codex">Local Codex</option>
      </select>
      {executionProvider === 'codex' && (
        <>
          <label className="sr-only" htmlFor="harness-device">
            Paired computer
          </label>
          <select
            id="harness-device"
            aria-label="Paired computer"
            value={controller.deviceId ?? ''}
            disabled={disabled}
            onChange={(event) => selectDevice(event.target.value || null)}
            className="h-8 max-w-36 rounded-md border border-(--nous-border-1) bg-(--nous-bg-1) px-2 text-xs text-(--nous-fg-1) focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
          >
            <option value="">Choose computer</option>
            {devices.map((device) => (
              <option key={device.id} value={device.id}>
                {device.label}
              </option>
            ))}
          </select>
          <label className="sr-only" htmlFor="harness-workspace">
            Project workspace
          </label>
          <select
            id="harness-workspace"
            aria-label="Project workspace"
            value={controller.workspaceId ?? ''}
            onChange={(event) => selectWorkspace(event.target.value || null)}
            disabled={disabled || !controller.deviceId}
            className="h-8 max-w-40 rounded-md border border-(--nous-border-1) bg-(--nous-bg-1) px-2 text-xs text-(--nous-fg-1) focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 disabled:opacity-50"
          >
            <option value="">Choose workspace</option>
            {workspaces.map((workspace) => (
              <option
                key={workspace.workspace_id}
                value={workspace.workspace_id}
              >
                {workspace.label}
              </option>
            ))}
          </select>
        </>
      )}
      <span
        role="status"
        aria-live="polite"
        className="hidden md:inline text-xs text-(--nous-fg-2)"
      >
        {statusLabel}
      </span>
      {disabledReason && executionProvider === 'codex' && (
        <span className="sr-only" role="note">
          {disabledReason}
        </span>
      )}
      {pendingRequests.map((request) => (
        <div
          key={request.id}
          className="absolute right-4 top-16 z-50 max-w-sm rounded-lg border border-(--nous-border-1) bg-(--nous-bg-1) p-3 shadow-lg"
          role="alertdialog"
          aria-label="Codex permission request"
        >
          <p className="text-sm font-medium">
            {Array.isArray(request.target.questions)
              ? 'Codex needs your input'
              : 'Allow Codex to continue?'}
          </p>
          <p className="mt-1 break-words text-xs text-(--nous-fg-2)">
            {requestLabel(request)}
          </p>
          {Array.isArray(request.target.questions) ? (
            <div className="mt-3 space-y-2">
              {(request.target.questions as Array<Record<string, unknown>>).map(
                (question) =>
                  typeof question.id === 'string' &&
                  typeof question.question === 'string' ? (
                    <label key={question.id} className="block text-xs">
                      <span>{question.question}</span>
                      <input
                        type="text"
                        aria-label={question.question}
                        value={answers[request.id]?.[question.id] ?? ''}
                        onChange={(event) =>
                          updateAnswer(
                            request.id,
                            question.id as string,
                            event.target.value
                          )
                        }
                        className="mt-1 block w-full rounded border border-(--nous-border-1) bg-(--nous-bg-1) px-2 py-1 focus-visible:outline focus-visible:outline-2"
                      />
                    </label>
                  ) : null
              )}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  disabled={busyRequestId === request.id}
                  onClick={() => void decide(request.id, false)}
                  className="rounded px-3 py-1 text-xs focus-visible:outline focus-visible:outline-2"
                >
                  Cancel request
                </button>
                <button
                  type="button"
                  disabled={busyRequestId === request.id}
                  onClick={() =>
                    void answer(
                      request.id,
                      request.target.questions as Array<Record<string, unknown>>
                    )
                  }
                  className="rounded bg-(--nous-sol) px-3 py-1 text-xs text-white focus-visible:outline focus-visible:outline-2"
                >
                  Send answers
                </button>
              </div>
            </div>
          ) : (
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                disabled={busyRequestId === request.id}
                onClick={() => void decide(request.id, false)}
                className="rounded px-3 py-1 text-xs focus-visible:outline focus-visible:outline-2"
              >
                Deny
              </button>
              <button
                type="button"
                disabled={busyRequestId === request.id}
                onClick={() => void decide(request.id, true)}
                className="rounded bg-(--nous-sol) px-3 py-1 text-xs text-white focus-visible:outline focus-visible:outline-2"
              >
                Allow once
              </button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
