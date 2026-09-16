import { stripVTControlCharacters } from "node:util";
import type { RuntimeMessage, ActivityStep } from "@nous/chat-runtime/types";
import { extractConfirmationPreview } from "@nous/chat-runtime/message";
import { loadConfig } from "../../frontend/cli/auth/store";
import { streamAgent, streamConfirm } from "../../frontend/cli/stream";

export type Approve = (
  details: Record<string, unknown>,
  signal: AbortSignal,
) => Promise<boolean>;

export const terminalText = (text: string): string =>
  stripVTControlCharacters(text).replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, "");

export function validatePrompt(prompt: string, attachmentIds: string[] = []) {
  if (!prompt.trim()) throw new Error("Enter a message.");
  if (prompt.length > 32000)
    throw new Error(
      "Messages must be at most 32,000 characters, including quotes.",
    );
  const paperId = loadConfig()?.paper_id;
  if (new Set([...attachmentIds, ...(paperId ? [paperId] : [])]).size > 10)
    throw new Error(
      "Attach at most 10 documents, including the selected paper.",
    );
}

/** Node transport only: application-owned messages feed the shared runtime bridge. */
export async function* streamReply(
  prompt: string,
  runtimeId: string,
  abortSignal: AbortSignal,
  approve: Approve,
  options: import("../../frontend/cli/stream").StreamOptions = {},
): AsyncGenerator<RuntimeMessage> {
  validatePrompt(prompt, options.attachmentIds);
  abortSignal.throwIfAborted();
  const config = loadConfig();
  const pageContext: Record<string, unknown> = config?.project_id
    ? {
        type: "project",
        project_id: config.project_id,
        project_name: config.project_name,
      }
    : { type: "chat" };
  if (config?.paper_id) {
    pageContext.paper_id = config.paper_id;
    pageContext.paper_title = config.paper_title;
  }

  let stream = streamAgent(prompt, pageContext, {
    ...options,
    signal: abortSignal,
  });
  let text = "";
  let tools: ActivityStep[] = [];
  let pendingApproval: RuntimeMessage["pendingApproval"];
  let approvalSequence = 0;
  const timestamp = Date.now();
  let activity = "";
  let reasoning = "";
  let tokenCount = 0;
  const started = Date.now();
  const contexts: Record<string, unknown>[] = [];
  const snapshot = (): RuntimeMessage => ({
    runtimeId,
    timestamp,
    role: "assistant",
    content: text,
    toolExecutions: tools,
    pendingApproval,
    isStreaming: true,
    activity,
    reasoning,
    timing: tokenCount
      ? {
          tokenCount,
          tokensPerSecond:
            tokenCount / Math.max((Date.now() - started) / 1000, 0.001),
        }
      : undefined,
    contexts: [...contexts],
  });

  while (true) {
    let confirmation:
      { threadId: string; details: Record<string, unknown> } | undefined;
    for await (const event of stream) {
      abortSignal.throwIfAborted();
      switch (event.type) {
        case "token":
          text += event.content;
          break;
        case "tool_start":
          tools = [
            ...tools,
            {
              id: `tool-${tools.length}`,
              tool: event.tool,
              label: event.tool,
              status: "running",
              argsSummary: event.args,
            },
          ];
          break;
        case "tool_end": {
          // ponytail: SSE identifies tools by name; use call IDs when the API exposes them.
          const index = tools.findIndex(
            (tool) => tool.tool === event.tool && tool.status === "running",
          );
          const finished: ActivityStep = {
            ...(tools[index] ?? {
              id: `tool-${tools.length}`,
              tool: event.tool,
              label: event.tool,
            }),
            result: event.result,
            status: event.isError ? "error" : "done",
          };
          tools =
            index < 0
              ? [...tools, finished]
              : tools.map((tool, i) => (i === index ? finished : tool));
          break;
        }
        case "plan":
          reasoning = event.reasoning;
          activity = event.steps.join(" → ");
          break;
        case "reflection":
          if (event.revising) text = "";
          activity = event.revising
            ? "Revising response…"
            : event.issues.join("; ");
          break;
        case "rag_context":
          contexts.push(...event.contexts);
          break;
        case "usage":
          tokenCount = event.outputTokens;
          activity = `${event.inputTokens} input / ${event.outputTokens} output tokens`;
          break;
        case "confirmation":
          if (!event.threadId)
            throw new Error("Approval request has no thread ID.");
          confirmation = event;
          pendingApproval = {
            id: `${runtimeId}-approval-${++approvalSequence}`,
            tools: extractConfirmationPreview(event.details).tools,
          };
          break;
        case "error":
          throw new Error(terminalText(event.message));
        case "done":
          yield {
            ...snapshot(),
            isStreaming: false,
          };
          return;
      }
      yield snapshot();
      if (confirmation) break;
    }
    abortSignal.throwIfAborted();
    if (!confirmation)
      throw new Error(
        "Connection closed before completion. The request was not retried.",
      );
    const approved = await approve(confirmation.details, abortSignal);
    abortSignal.throwIfAborted();
    pendingApproval = undefined;
    yield snapshot();
    stream = streamConfirm(confirmation.threadId, approved, {
      signal: abortSignal,
      onThreadId: options.onThreadId,
    });
  }
}
