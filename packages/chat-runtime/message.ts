import type { ActivityStep, RuntimeMessage, RuntimeAttachment } from "./types";

export const HITL_APPROVAL_TOOL = "__nous_approval__";

type ToolCallPart = {
  type: "tool-call";
  toolCallId: string;
  toolName: string;
  // ThreadMessageLike types args as ReadonlyJSONObject. At runtime we carry the
  // structured (backend-redacted, JSON-safe) tool args so declarative per-tool
  // renderers read real fields via their own TArgs generic; the type stays the
  // JSON-compatible empty shape. `argsText` is the one-line fallback summary.
  args: Record<string, never>;
  argsText: string;
  result?: unknown;
  isError?: true;
  // Real approval gate for the in-band HITL part (P4). `approved`
  // omitted = pending, so the renderer's respondToApproval is callable and
  // routes to the adapter's onRespondToToolApproval.
  approval?: { readonly id: string; readonly approved?: boolean };
};

// Tool parts must be referentially stable across per-token re-conversions of
// the in-flight message: streamingSteps only changes reference on
// tool_start/tool_end, so a WeakMap keyed on the steps array is exact.
const partsCache = new WeakMap<ActivityStep[], Map<string, ToolCallPart[]>>();

function getAttachmentName(attachment: RuntimeAttachment): string {
  return (
    attachment.display_name ??
    attachment.document_title ??
    attachment.document_id ??
    "Attachment"
  );
}

function getAttachmentType(
  attachment: RuntimeAttachment,
): "image" | "document" | "file" {
  if (attachment.mime_type?.startsWith("image/") || attachment.thumbnail_url) {
    return "image";
  }
  if (attachment.document_type || attachment.mime_type?.includes("pdf")) {
    return "document";
  }
  return "file";
}

function toRuntimeAttachments(attachments: RuntimeAttachment[] | undefined) {
  if (!attachments || attachments.length === 0) return undefined;

  return attachments.map((attachment) => {
    const name = getAttachmentName(attachment);
    const type = getAttachmentType(attachment);
    return {
      id: attachment.id,
      type,
      name,
      ...(attachment.mime_type ? { contentType: attachment.mime_type } : {}),
      status: { type: "complete" as const },
      content:
        type === "image" && attachment.thumbnail_url
          ? [
              {
                type: "image" as const,
                image: attachment.thumbnail_url,
                filename: name,
              },
            ]
          : [],
    };
  });
}

export function toToolCallParts(
  messageId: string,
  steps: ActivityStep[],
): ToolCallPart[] {
  const cached = partsCache.get(steps)?.get(messageId);
  if (cached) return cached;
  const parts = steps.map((step, i): ToolCallPart => {
    const settled = step.status !== "running";
    return {
      type: "tool-call",
      toolCallId: `${messageId}-tool-${step.id ?? i}`,
      toolName: step.tool,
      args: (step.args ?? {}) as Record<string, never>,
      argsText: step.argsSummary ?? "",
      ...(settled && step.result !== undefined
        ? { result: step.result }
        : settled && step.resultSummary !== undefined
          ? { result: step.resultSummary }
          : {}),
      ...(step.status === "error" ? { isError: true as const } : {}),
    };
  });
  const byMessage = partsCache.get(steps) ?? new Map<string, ToolCallPart[]>();
  byMessage.set(messageId, parts);
  partsCache.set(steps, byMessage);
  return parts;
}

export function convertMessage(message: RuntimeMessage) {
  const toolParts = message.toolExecutions?.length
    ? // Deterministic per-message fallback: a shared constant like 'local'
      // would collide across multiple id-less messages (duplicate toolCallIds).
      toToolCallParts(message.runtimeId, message.toolExecutions)
    : [];

  // In-band HITL approval gate (P4): emit an approval tool-call part
  // routed to the registered HitlApprovalToolUI. Its args carry the confirmed
  // tool name + args for display; the bridge store drives resolution.
  const approvalParts: ToolCallPart[] = message.pendingApproval
    ? [
        {
          type: "tool-call",
          toolCallId: message.pendingApproval.id,
          toolName: HITL_APPROVAL_TOOL,
          args: {
            tools: message.pendingApproval.tools,
          } as unknown as Record<string, never>,
          argsText: "",
          approval: { id: message.pendingApproval.id },
        },
      ]
    : [];
  return {
    id: message.runtimeId,
    role: message.role,
    // Null-safe: a message may not have a timestamp yet (e.g. an optimistic
    // local insert before the server round-trip). `new Date(0)` would be a
    // misleading 1970 date, so omit createdAt instead.
    ...(message.timestamp ? { createdAt: new Date(message.timestamp) } : {}),
    ...(message.role === "assistant"
      ? {
          status: message.error
            ? {
                type: "incomplete" as const,
                reason: "error" as const,
                error: message.error.message,
              }
            : message.metadata?.stopped
              ? { type: "incomplete" as const, reason: "cancelled" as const }
              : message.isStreaming
                ? { type: "running" as const }
                : { type: "complete" as const, reason: "stop" as const },
        }
      : {}),
    metadata: {
      ...(message.timing
        ? {
            timing: {
              streamStartTime: message.timestamp,
              totalChunks: 0,
              toolCallCount: message.toolExecutions?.length ?? 0,
              ...message.timing,
            },
          }
        : {}),
      ...(message.feedback?.rating
        ? {
            submittedFeedback: {
              type:
                message.feedback.rating >= 4
                  ? ("positive" as const)
                  : ("negative" as const),
            },
          }
        : {}),
      custom: {
        ...message.metadata,
        activity: message.activity,
        contexts: message.contexts,
      },
    },
    ...(message.attachments?.length
      ? { attachments: toRuntimeAttachments(message.attachments) }
      : {}),
    content: [
      ...(message.reasoning
        ? [{ type: "reasoning" as const, text: message.reasoning }]
        : []),
      ...toolParts,
      ...approvalParts,
      { type: "text" as const, text: message.content },
    ],
  };
}

/** Tool name + args preview from an interrupt's confirmation payload — flat
 * (tool_name/tool_args) or the first entry of a `tools` list. Mirrors the
 * page-level banner's extractToolCall (P4). */
export function extractConfirmationPreview(
  confirmation: Record<string, unknown> | undefined,
): { tools: Array<{ name: string; args: Record<string, unknown> }> } {
  if (!confirmation) return { tools: [{ name: "this action", args: {} }] };
  const rawTools = Array.isArray(confirmation.tools) ? confirmation.tools : [];
  const tools = rawTools.flatMap((raw) => {
    if (!raw || typeof raw !== "object") return [];
    const item = raw as Record<string, unknown>;
    if (typeof item.name !== "string" || !item.name.trim()) return [];
    const args =
      item.args && typeof item.args === "object" && !Array.isArray(item.args)
        ? (item.args as Record<string, unknown>)
        : {};
    return [{ name: item.name, args }];
  });
  if (tools.length > 0) return { tools };

  const flatName = confirmation.tool_name;
  const rawArgs = confirmation.tool_args ?? confirmation.args;
  if (typeof flatName === "string" && flatName.trim()) {
    return {
      tools: [
        {
          name: flatName,
          args:
            rawArgs && typeof rawArgs === "object" && !Array.isArray(rawArgs)
              ? (rawArgs as Record<string, unknown>)
              : {},
        },
      ],
    };
  }
  return { tools: [{ name: "this action", args: {} }] };
}
