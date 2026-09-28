import type { AdapterEvent } from "./contracts.ts";
import type { ProducerBody } from "./connection.ts";

/**
 * Map the three pinned, one-shot Codex callback schemas into a bridge event.
 * Unknown request kinds never cross the trust boundary; codex.ts also denies
 * unsupported callbacks locally before yielding them.
 */
export function nativeRequestBody(event: AdapterEvent): ProducerBody {
  if (event.kind !== "request") throw new Error("native request required");
  const p = event.params;
  const only = (keys: string[]) => Object.keys(p).every((key) => keys.includes(key));
  if (
    p.threadId !== event.sessionId ||
    p.turnId !== event.turnId ||
    p.itemId !== event.itemId
  )
    throw new Error("native request identity mismatch");
  if (event.method === "item/commandExecution/requestApproval") {
    if (
      !only([
        "threadId", "turnId", "itemId", "approvalId", "command", "cwd", "reason",
        "startedAtMs", "environmentId", "kind", "commandActions", "grantRoot",
        "proposedExecpolicyAmendment", "proposedNetworkPolicyAmendments", "networkApprovalContext",
      ]) ||
      typeof p.command !== "string" ||
      !p.command.length ||
      p.grantRoot != null ||
      p.proposedExecpolicyAmendment != null ||
      p.proposedNetworkPolicyAmendments != null ||
      p.networkApprovalContext != null
    )
      throw new Error("persistent command permission is unsupported");
  } else if (event.method === "item/fileChange/requestApproval") {
    if (
      !only(["threadId", "turnId", "itemId", "reason", "startedAtMs", "grantRoot"]) ||
      p.grantRoot != null
    )
      throw new Error("file approval root changes are unsupported");
  } else if (event.method === "item/tool/requestUserInput") {
    if (
      !only(["threadId", "turnId", "itemId", "questions", "isBlocking", "autoResolutionMs"]) ||
      typeof p.isBlocking !== "boolean" ||
      !Array.isArray(p.questions) ||
      p.questions.length === 0 ||
      p.questions.length > 16 ||
      p.questions.some(
        (q) =>
          !q ||
          typeof q !== "object" ||
          typeof q.id !== "string" ||
          !q.id ||
          typeof q.header !== "string" ||
          !q.header ||
          typeof q.question !== "string" ||
          !q.question ||
          q.isSecret === true ||
          (q.options !== null &&
            q.options !== undefined &&
            (!Array.isArray(q.options) ||
              (q.options as unknown[]).some(
                (option: unknown) =>
                  !option ||
                  typeof option !== "object" ||
                  !("label" in option) ||
                  !("description" in option) ||
                  typeof option.label !== "string" ||
                  typeof option.description !== "string",
              ))),
      ) ||
      new Set(p.questions.map((q) => (q as { id: string }).id)).size !==
        p.questions.length
    )
      throw new Error("invalid native input questions");
  } else {
    throw new Error("unsupported native request kind");
  }
  return {
    kind: "request",
    sessionId: event.sessionId,
    turnId: event.turnId,
    itemId: event.itemId,
    requestId: event.requestId,
    ...(event.approvalId ? { approvalId: event.approvalId } : {}),
    method: event.method,
    params: structuredClone(p),
  };
}
