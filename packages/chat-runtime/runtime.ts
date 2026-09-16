"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import type { RuntimeMessage } from "./types";
import { convertMessage } from "./message";

/** Only the fields used by this bridge, compatible with both installed SDKs. */
interface AppendMessage {
  content: readonly { type: string; text?: unknown }[];
  runConfig?: { custom?: Record<string, unknown> };
  attachments?: readonly { id: string }[];
  metadata?: { custom?: Record<string, unknown> };
}
function draftInput(message: AppendMessage): [string, string[] | undefined] {
  const text = message.content
    .filter((p) => p.type === "text" && typeof p.text === "string")
    .map((p) => p.text)
    .join("\n");
  const custom = message.runConfig?.custom?.attachmentIds;
  const ids =
    Array.isArray(custom) &&
    custom.every((id): id is string => typeof id === "string")
      ? custom
      : message.attachments?.map((a) => a.id);
  const quote = message.metadata?.custom?.quote as
    { text?: unknown } | undefined;
  const content =
    typeof quote?.text === "string"
      ? `> ${quote.text.replace(/\n/g, "\n> ")}\n\n${text}`
      : text;
  return [content || (ids?.length ? "Use the attached documents." : ""), ids];
}
interface QueueController {
  adapter: { items: readonly unknown[]; steerItems?: readonly unknown[] };
  subscribe(callback: () => void): () => void;
  notifyBusy(): void;
  notifyIdle(): void;
}
function useLatestSend(
  callback: ChatRuntimeProps["onSend"],
): ChatRuntimeProps["onSend"] {
  const callbackRef = useRef(callback);
  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);
  return useCallback(
    (text: string, attachmentIds?: string[]) =>
      callbackRef.current(text, attachmentIds),
    [],
  );
}

export interface ChatRuntimeProps {
  messages: RuntimeMessage[];
  isRunning: boolean;
  /** When true, the composer refuses to send (e.g. a HITL confirmation
   * is pending). Passed through to the runtime's compose disabled state. */
  isSendDisabled?: boolean;
  /** Delegates to the existing useChatStreaming send. */
  onSend: (text: string, attachmentIds?: string[]) => void | Promise<void>;
  onCancel: () => void;
  /** Resolves an in-band HITL approval (P4). Wired to the ExternalStore
   * adapter's onRespondToToolApproval so the approval tool UI's
   * respondToApproval routes to the existing hardened confirm handler. */
  onApproval?: (approved: boolean, approvalId?: string) => void;
}

/** Shared message projection, queue and callback wiring. Each platform supplies
 * its SDK's queue factory and passes this adapter to useExternalStoreRuntime. */
export function useChatRuntimeAdapter<Queue extends QueueController>(
  {
    messages,
    isRunning,
    isSendDisabled,
    onSend,
    onCancel,
    onApproval,
  }: ChatRuntimeProps,
  createMessageQueue: (driver: {
    run: (message: AppendMessage) => void;
  }) => Queue,
) {
  const send = useLatestSend(onSend);

  // Keep one queue for this runtime identity. The driver deliberately has no
  // cancel callback: assistant-ui Steer therefore reorders a pending item and
  // lets the current single-flight stream settle before processing it.
  const [queue] = useState(() => {
    const controller = createMessageQueue({
      run: (message) => {
        const [text, attachmentIds] = draftInput(message);
        if (text.trim()) void send(text, attachmentIds);
      },
    });
    return controller;
  });

  // createMessageQueue mutates its adapter in place. Subscribe so the
  // external-store runtime receives a fresh render and projects queue items.
  useSyncExternalStore(
    queue.subscribe,
    () => queue.adapter.items,
    () => queue.adapter.items,
  );

  useSyncExternalStore(
    queue.subscribe,
    () => queue.adapter.steerItems,
    () => queue.adapter.steerItems,
  );

  const previousBusyRef = useRef<boolean | undefined>(undefined);
  useEffect(() => {
    const busy = isRunning || !!isSendDisabled;
    const previous = previousBusyRef.current;
    previousBusyRef.current = busy;
    if (previous === undefined) {
      if (busy) queue.notifyBusy();
      return;
    }
    if (busy && !previous) queue.notifyBusy();
    if (!busy && previous) queue.notifyIdle();
  }, [isRunning, isSendDisabled, queue]);

  const onNew = useCallback(
    async (message: AppendMessage) => {
      const [text, attachmentIds] = draftInput(message);
      if (text.trim()) await onSend(text, attachmentIds);
    },
    [onSend],
  );

  const handleCancel = useCallback(async () => {
    onCancel();
  }, [onCancel]);

  // Route the runtime's approval resolution to the existing confirm handler.
  // The renderer's respondToApproval → runtime → this adapter callback; opts
  // carries the resolved boolean, so no option-list resolution is needed here.
  const onRespondToToolApproval = useCallback(
    (opts: { approved: boolean; approvalId?: string }) => {
      onApproval?.(opts.approved, opts.approvalId);
    },
    [onApproval],
  );

  return {
    messages,
    isRunning,
    isSendDisabled,
    convertMessage,
    queue: queue.adapter as Queue["adapter"],
    onNew,
    onCancel: handleCancel,
    onRespondToToolApproval,
  };
}
