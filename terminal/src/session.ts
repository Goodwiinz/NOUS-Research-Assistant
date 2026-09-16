import { randomUUID } from "node:crypto";
import { useCallback, useEffect, useRef, useState } from "react";
import { loadConfig, saveConfig } from "../../frontend/cli/auth/store";
import { appendHistory } from "../../frontend/cli/services/promptHistory";
import { streamReply, validatePrompt, type Approve } from "./adapter";
import {
  forkThread,
  withoutThread,
  forgetBranches,
  loadTranscript,
  loadBranches,
  saveBranches,
  request,
  type BranchHistory,
  type TerminalMessage,
  type Message,
} from "./services";

export function visibleMessages(history: BranchHistory): TerminalMessage[] {
  const byId = new Map(
    history.nodes.map((node) => [node.message.runtimeId, node]),
  );
  const path: TerminalMessage[] = [];
  const seen = new Set<string>();
  let id = history.headId;
  while (id && !seen.has(id)) {
    seen.add(id);
    const node = byId.get(id);
    if (!node) break;
    path.unshift(node.message);
    id = node.parentId;
  }
  return path;
}
function fromMessages(messages: TerminalMessage[]): BranchHistory {
  return {
    headId: messages.at(-1)?.runtimeId ?? null,
    nodes: messages.map((message, index) => ({
      message,
      parentId: messages[index - 1]?.runtimeId ?? null,
    })),
  };
}

/** Keep server data current while retaining stable UI IDs and alternate paths.
 * An undefined transcript selects cached history after a failed refresh. */
export function reconcileHistory(
  remote: TerminalMessage[] | undefined,
  saved: BranchHistory | undefined,
  threadId: string | null,
): BranchHistory {
  if (!saved) return fromMessages(remote ?? []);
  const leaf =
    [...saved.nodes].reverse().find((n) => n.message.threadId === threadId)
      ?.message.runtimeId ?? null;
  if (remote === undefined) return { ...saved, headId: leaf };
  const known = visibleMessages({ ...saved, headId: leaf });
  const nodes = new Map(saved.nodes.map((n) => [n.message.runtimeId, n]));
  let parentId: string | null = null;
  remote.forEach((message, index) => {
    const previous = known[index];
    const same =
      previous?.role === message.role && previous.content === message.content;
    const runtimeId = same ? previous.runtimeId : message.runtimeId;
    // An inherited prefix belongs to its original server branch.
    const projected =
      same && previous.threadId !== threadId
        ? previous
        : { ...message, runtimeId };
    nodes.set(runtimeId, { parentId, message: projected });
    parentId = runtimeId;
  });
  return { headId: parentId, nodes: [...nodes.values()] };
}

/** One application-owned branch graph. The backend owns each branch's transcript. */
export function useTerminalSession(
  initialMessages: TerminalMessage[],
  initialHistory?: BranchHistory,
  initialNotice = "",
) {
  const [history, setHistory] = useState(
    initialHistory ?? fromMessages(initialMessages),
  );
  const state = useRef(history);
  const [isRunning, setIsRunning] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [notice, setNotice] = useState(initialNotice);
  const active = useRef<AbortController | null>(null);
  const navigation = useRef(0);
  const decision = useRef<{
    id: string;
    respond: (approved: boolean) => void;
  } | null>(null);
  useEffect(
    () => () => {
      active.current?.abort();
      navigation.current++;
    },
    [],
  );
  const publish = useCallback((next: BranchHistory) => {
    state.current = next;
    setHistory(next);
  }, []);
  const persistence = useRef(Promise.resolve());
  const remember = async () => {
    try {
      const snapshot = state.current;
      persistence.current = persistence.current
        .catch(() => {})
        .then(() => saveBranches(snapshot));
      await persistence.current;
    } catch {
      setNotice(
        "Could not save local branch navigation. Server threads remain available.",
      );
    }
  };
  const selectThread = (id: string | null) => {
    const config = loadConfig();
    if (config) saveConfig({ ...config, thread_id: id });
  };
  const busy = () => !!active.current || isLoading;
  const switchThread = async (id: string | null, signal?: AbortSignal) => {
    if (active.current)
      throw new Error("Stop the current response before switching threads.");
    const generation = ++navigation.current;
    setIsLoading(true);
    try {
      const remote = id ? await loadTranscript(id, signal) : [];
      const saved = id ? await loadBranches(id) : undefined;
      signal?.throwIfAborted();
      if (generation !== navigation.current) return;
      const next = reconcileHistory(remote, saved, id);
      selectThread(id);
      publish({ ...next });
      setNotice("");
    } finally {
      if (generation === navigation.current) setIsLoading(false);
    }
  };

  const send = async (
    prompt: string,
    attachmentIds: string[] = [],
    parentOverride?: string | null,
  ) => {
    if (!prompt.trim() || active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setIsRunning(true);
    try {
      appendHistory(prompt);
    } catch {
      setNotice("Could not save prompt history.");
    }
    const userId = randomUUID();
    const runtimeId = randomUUID();
    const threadId = loadConfig()?.thread_id ?? undefined;
    let resolvedId = threadId;
    const parentId =
      parentOverride === undefined ? state.current.headId : parentOverride;
    const prior = visibleMessages({ ...state.current, headId: parentId });
    let reply: TerminalMessage = {
      runtimeId,
      threadId,
      role: "assistant",
      content: "",
      timestamp: Date.now(),
      isStreaming: true,
    };
    const updateReply = () =>
      publish({
        ...state.current,
        headId: runtimeId,
        nodes: state.current.nodes.map((n) =>
          n.message.runtimeId === runtimeId
            ? { ...n, message: reply }
            : n.message.runtimeId === userId &&
                n.message.threadId !== resolvedId
              ? { ...n, message: { ...n.message, threadId: resolvedId } }
              : n,
        ),
      });
    publish({
      headId: runtimeId,
      nodes: [
        ...state.current.nodes,
        {
          parentId,
          message: {
            runtimeId: userId,
            threadId,
            role: "user",
            content: prompt,
            timestamp: Date.now(),
            attachments: attachmentIds.map((id) => ({ id, document_id: id })),
          },
        },
        { parentId: userId, message: reply },
      ],
    });
    const approve: Approve = (_details, signal) =>
      new Promise((resolve, reject) => {
        signal.throwIfAborted();
        const cancel = () => {
          decision.current = null;
          reject(signal.reason);
        };
        signal.addEventListener("abort", cancel, { once: true });
        decision.current = {
          id: reply.pendingApproval!.id,
          respond: (approved) => {
            decision.current = null;
            signal.removeEventListener("abort", cancel);
            reply = { ...reply, pendingApproval: undefined };
            updateReply();
            resolve(approved);
          },
        };
      });
    try {
      for await (const update of streamReply(
        prompt,
        runtimeId,
        controller.signal,
        approve,
        {
          onThreadId: (id) => {
            resolvedId = id;
          },
          clientMessageId: userId,
          attachmentIds,
          history: prior.map(({ role, content }) => ({ role, content })),
        },
      )) {
        reply = { ...update, threadId: resolvedId };
        updateReply();
      }
    } catch (error) {
      reply = {
        ...reply,
        pendingApproval: undefined,
        isStreaming: false,
        ...(controller.signal.aborted
          ? { metadata: { ...reply.metadata, stopped: true } }
          : {
              error: {
                message: error instanceof Error ? error.message : String(error),
              },
            }),
      };
      updateReply();
    } finally {
      await remember();
      active.current = null;
      decision.current = null;
      setIsRunning(false);
    }
  };

  const branch = async (userId: string, text: string) => {
    if (busy())
      throw new Error(
        "Wait for the current operation before editing or retrying.",
      );
    const messages = visibleMessages(state.current);
    const index = messages.findIndex(
      (m) => m.runtimeId === userId && m.role === "user",
    );
    if (index < 0) throw new Error("Select a user message to edit or retry.");
    validatePrompt(
      text,
      messages[index].attachments?.map((a) => a.document_id),
    );
    const threadId = loadConfig()?.thread_id;
    if (!threadId)
      throw new Error("The server has not assigned this thread yet.");
    const parentId = messages[index - 1]?.runtimeId ?? null;
    // Lock before the first await; a second click cannot create a duplicate fork.
    const controller = new AbortController();
    active.current = controller;
    setIsLoading(true);
    try {
      const newId = await forkThread(
        threadId,
        messages.slice(0, index),
        controller.signal,
      );
      controller.signal.throwIfAborted();
      selectThread(newId);
      active.current = null;
      setIsLoading(false);
      await send(
        text,
        messages[index].attachments?.map((a) => a.document_id),
        parentId,
      );
    } finally {
      if (active.current === controller) active.current = null;
      setIsLoading(false);
    }
  };

  const forgetThread = async (id: string) => {
    await persistence.current;
    await forgetBranches(id, state.current);
    publish(withoutThread(state.current, id));
    await remember();
  };

  const switchBranch = (ids: readonly string[]) => {
    if (busy()) return;
    const headId = ids.at(-1) ?? null;
    const message = state.current.nodes.find(
      (n) => n.message.runtimeId === headId,
    )?.message;
    if (!message?.threadId) return;
    selectThread(message.threadId);
    publish({ ...state.current, headId });
    void remember();
  };

  const feedback = async (runtimeId: string, type: "positive" | "negative") => {
    try {
      const messages = visibleMessages(state.current);
      const index = messages.findIndex((m) => m.runtimeId === runtimeId);
      const target = messages[index];
      if (target?.role !== "assistant")
        throw new Error("Feedback is only available for assistant messages.");
      if (!target.threadId)
        throw new Error("Wait for the server to save this message.");
      let serverId = target.serverId;
      if (!serverId) {
        const remote = await loadTranscript(target.threadId);
        const user = messages
          .slice(0, index)
          .reverse()
          .find((m) => m.role === "user");
        const userIndex = remote.findIndex(
          (m) => m.runtimeId === user?.runtimeId,
        );
        const candidate = userIndex < 0 ? undefined : remote[userIndex + 1];
        if (
          candidate?.role === "assistant" &&
          candidate.content === target.content
        )
          serverId = candidate.serverId;
      }
      if (!serverId) {
        throw new Error(
          "Message persistence is pending. Refresh the thread before submitting feedback.",
        );
      }
      await request<Message>(
        `/messages/${encodeURIComponent(serverId)}`,
        "PATCH",
        { feedback_rating: type === "positive" ? 5 : 1 },
      );
      publish({
        ...state.current,
        nodes: state.current.nodes.map((n) =>
          n.message.runtimeId === runtimeId
            ? {
                ...n,
                message: {
                  ...n.message,
                  serverId,
                  feedback: {
                    rating: type === "positive" ? 5 : 1,
                    comment: null,
                  },
                },
              }
            : n,
        ),
      });
      void remember();
    } catch (error) {
      publish({ ...state.current });
      throw error;
    }
  };

  const messages = visibleMessages(history);
  return {
    messages,
    history,
    isRunning,
    isLoading,
    notice,
    setNotice,
    isSendDisabled: isLoading || messages.some((m) => !!m.pendingApproval),
    onSend: (text: string, ids?: string[]) => send(text, ids),
    branch,
    switchBranch,
    forgetThread,
    switchThread,
    feedback,
    onCancel: useCallback(() => {
      active.current?.abort();
      navigation.current++;
      if (!active.current) setIsLoading(false);
    }, []),
    onApproval: useCallback((approved: boolean, approvalId?: string) => {
      if (decision.current?.id !== approvalId) return;
      decision.current?.respond(approved);
    }, []),
  };
}
