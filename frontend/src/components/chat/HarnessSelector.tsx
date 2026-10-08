'use client';

import { useState, type ReactElement } from 'react';
import { useHarnessConnection } from '@/hooks/chat/useHarnessConnection';
import type { NativeDecision } from '@/services/harnessService';

interface HarnessSelectorProps {
  controller: ReturnType<typeof useHarnessConnection>;
  disabled?: boolean;
}

function requestTitle(method: string): string {
  switch (method) {
    case 'item/commandExecution/requestApproval':
      return 'Codex requests command execution';
    case 'item/fileChange/requestApproval':
      return 'Codex requests file changes';
    case 'item/tool/requestUserInput':
      return 'Codex needs your input';
    default:
      return 'Codex requests permission';
  }
}

function requestTargetRows(
  method: string,
  target: Record<string, unknown>
): Array<[string, string]> {
  if (method === 'item/commandExecution/requestApproval') {
    return ['command', 'cwd', 'reason', 'kind', 'itemId'].flatMap((field) => {
      const value = target[field];
      return typeof value === 'string' && value.length > 0
        ? [[fieldLabel(field), value]]
        : [];
    });
  }
  if (method === 'item/fileChange/requestApproval') {
    const rows: Array<[string, string]> = [];
    for (const field of ['path', 'filePath', 'paths', 'filePaths', 'files']) {
      const value = target[field];
      const paths =
        typeof value === 'string'
          ? [value]
          : Array.isArray(value)
            ? value.filter(
                (item): item is string =>
                  typeof item === 'string' && item.trim().length > 0
              )
            : [];
      if (paths.length > 0) {
        rows.push(['Affected file paths', paths.join('\n')]);
      }
    }
    for (const field of ['changeSummary', 'summary', 'changes']) {
      const value = target[field];
      const summary =
        typeof value === 'string'
          ? value
          : Array.isArray(value)
            ? value
                .filter(
                  (item): item is string =>
                    typeof item === 'string' && item.trim().length > 0
                )
                .join('\n')
            : '';
      if (summary.trim().length > 0) {
        rows.push(['Change summary', summary]);
      }
    }
    const reason = target.reason;
    if (typeof reason === 'string' && reason.trim().length > 0) {
      rows.push(['Reason', reason]);
    }
    const itemId = target.itemId;
    if (typeof itemId === 'string' && itemId.length > 0) {
      rows.push(['Item reference', itemId]);
    }
    return rows;
  }
  return [];
}

function fieldLabel(field: string): string {
  if (field === 'cwd') return 'Working directory';
  if (field === 'itemId') return 'Item reference';
  return field;
}

function hasReviewableTarget(
  method: string,
  target: Record<string, unknown>
): boolean {
  if (method === 'item/fileChange/requestApproval') {
    // The currently persisted native schema carries only reason/item identity
    // for file changes, so a request without explicit paths or a concrete
    // change summary must remain deny-only.
    const hasPath = ['path', 'filePath', 'paths', 'filePaths', 'files'].some(
      (field) => {
        const value = target[field];
        return typeof value === 'string'
          ? value.trim().length > 0
          : Array.isArray(value) &&
              value.some(
                (item) => typeof item === 'string' && item.trim().length > 0
              );
      }
    );
    const hasSummary = ['changeSummary', 'summary', 'changes'].some((field) => {
      const value = target[field];
      return typeof value === 'string'
        ? value.trim().length > 0
        : Array.isArray(value) &&
            value.some(
              (item) => typeof item === 'string' && item.trim().length > 0
            );
    });
    return hasPath || hasSummary;
  }
  if (method === 'item/commandExecution/requestApproval') {
    const value = target.command;
    return typeof value === 'string' && value.trim().length > 0;
  }
  return false;
}

export function HarnessSelector({
  controller,
  disabled = false,
}: HarnessSelectorProps): ReactElement {
  const [busyRequestId, setBusyRequestId] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
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
    setRequestError(null);
    setBusyRequestId(requestId);
    try {
      const decision: NativeDecision = { kind: 'decision', allow };
      await decideRequest(requestId, decision);
    } catch {
      setRequestError(
        'Could not process this Codex request. Refresh the conversation before trying again.'
      );
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
    setRequestError(null);
    setBusyRequestId(requestId);
    try {
      await decideRequest(requestId, {
        kind: 'answers',
        answers: exactAnswers,
      });
    } catch {
      setRequestError(
        'Could not send these answers. Refresh the conversation before trying again.'
      );
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
            // Opening the picker refetches the list, so a computer connected
            // since (a new device id) shows up without reloading the page.
            onMouseDown={controller.refreshDevices}
            onFocus={controller.refreshDevices}
            onChange={(event) => selectDevice(event.target.value || null)}
            className="h-8 max-w-36 rounded-md border border-(--nous-border-1) bg-(--nous-bg-1) px-2 text-xs text-(--nous-fg-1) focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
          >
            <option value="">Choose computer</option>
            {devices.map((device) => {
              const elsewhere = controller.isBoundToAnotherChat(device.id);
              return (
                <option key={device.id} value={device.id} disabled={elsewhere}>
                  {elsewhere
                    ? `${device.label} (bound to another chat)`
                    : device.label}
                </option>
              );
            })}
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
          <p className="text-sm font-medium">{requestTitle(request.method)}</p>
          {requestError && (
            <p className="mt-2 text-xs text-red-600" role="alert">
              {requestError}
            </p>
          )}
          {requestTargetRows(request.method, request.target).length > 0 && (
            <dl className="mt-2 space-y-1 text-xs">
              {requestTargetRows(request.method, request.target).map(
                ([label, value]) => (
                  <div key={label}>
                    <dt className="font-medium text-(--nous-fg-2)">{label}</dt>
                    <dd className="whitespace-pre-wrap break-words">{value}</dd>
                  </div>
                )
              )}
            </dl>
          )}
          {request.method === 'item/fileChange/requestApproval' &&
            !hasReviewableTarget(request.method, request.target) && (
              <p className="mt-2 text-xs text-(--nous-fg-2)">
                File paths or a file-specific change summary are unavailable, so
                this request cannot be approved here.
              </p>
            )}
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
                disabled={
                  busyRequestId === request.id ||
                  !hasReviewableTarget(request.method, request.target)
                }
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
