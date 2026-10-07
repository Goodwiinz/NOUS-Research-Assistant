import { ReauthenticationRequired, ToolRequestRejected } from "../mcp/client.ts";
import type { LocalTool } from "../mcp/server.ts";
import type { ActionHttpClient, ActionStatus } from "./client.ts";

const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const text = (value: unknown): boolean => typeof value === "string" && value.trim() !== "";
const list = (value: unknown, max: number, item: (value: unknown) => boolean): boolean =>
  Array.isArray(value) && value.length >= 1 && value.length <= max && value.every(item);

// NOUS's bounds (backend/src/services/agent/tool_actions.py). NOUS validates
// every argument again; the checks here only refuse a malformed call early.
const MAX_DOCUMENT_IDS = 20;
const MAX_PAPER_IDS = 10;

/** The shape each field must have before a request leaves the bridge. */
const FIELDS = {
  title: { valid: text, expected: "non-empty text" },
  content: { valid: text, expected: "non-empty text" },
  tags: {
    valid: (value: unknown) => Array.isArray(value) && value.every((tag) => typeof tag === "string"),
    expected: "a list of strings",
  },
  document_ids: {
    valid: (value: unknown) => list(value, MAX_DOCUMENT_IDS, uuid),
    expected: `a list of 1-${MAX_DOCUMENT_IDS} document UUIDs`,
  },
  document_id: { valid: uuid, expected: "a document UUID" },
  // Folders are chosen by id only: NOUS never resolves a folder name here.
  project_id: { valid: uuid, expected: "a project UUID" },
  from_project_id: { valid: uuid, expected: "a project UUID" },
  to_project_id: { valid: uuid, expected: "a project UUID" },
  name: { valid: text, expected: "non-empty text" },
  description: { valid: (value: unknown) => typeof value === "string", expected: "text" },
  paper_ids: {
    valid: (value: unknown) => list(value, MAX_PAPER_IDS, text),
    expected: `a list of 1-${MAX_PAPER_IDS} arXiv ids`,
  },
} satisfies Record<string, { valid: (value: unknown) => boolean; expected: string }>;
type Field = keyof typeof FIELDS;
type ActionFields = {
  required: readonly Field[];
  optional?: readonly Field[];
  // Optional fields whose validator in NOUS reads null as left out: a null
  // there is dropped. Any other null is refused, as NOUS refuses it.
  nullable?: readonly Field[];
  // At least one of the optional fields must be given.
  atLeastOneOptional?: true;
};

/**
 * Every action NOUS accepts (`ALLOWED_ACTIONS`) and the only fields sent for
 * it, matching its validator there. Identity never travels: NOUS takes the
 * user, organization and binding from the grant. A `project_id`-style field
 * is a selector NOUS checks against the grant before storing anything.
 */
const ACTION_FIELDS: Record<string, ActionFields> = {
  create_project_note: { required: ["title", "content"], optional: ["tags"] },
  save_papers_to_folder: { required: ["document_ids", "project_id"] },
  remove_papers_from_folder: { required: ["document_ids", "project_id"] },
  move_papers_between_folders: { required: ["document_ids", "from_project_id", "to_project_id"] },
  create_folder: { required: ["name"], optional: ["description"], nullable: ["description"] },
  rename_folder: { required: ["project_id", "name"] },
  delete_folder: { required: ["project_id"] },
  update_document_metadata: { required: ["document_id"], optional: ["title", "tags"], atLeastOneOptional: true },
  // A workspace connection must name the folder; a project connection may only name its own.
  ingest_arxiv_papers: { required: ["paper_ids"], optional: ["project_id"], nullable: ["project_id"] },
};
const ACTIONS = Object.keys(ACTION_FIELDS);

/** e.g. `update_document_metadata(document_id, title and/or tags)`, for the model. */
function signature(action: string): string {
  const { required, optional = [], atLeastOneOptional } = ACTION_FIELDS[action]!;
  const extra = atLeastOneOptional ? [optional.join(" and/or ")] : optional.map((field) => `${field}?`);
  return `${action}(${[...required, ...extra].join(", ")})`;
}

/** The action's own fields from the model's arguments, or why they are refused. Every other key is dropped. */
function actionArguments(action: string, args: Record<string, unknown>): Record<string, unknown> | string {
  const { required, optional = [], nullable = [], atLeastOneOptional } = ACTION_FIELDS[action]!;
  const picked: Record<string, unknown> = {};
  for (const field of [...required, ...optional]) {
    const value = args[field];
    const absent = value === undefined || (value === null && nullable.includes(field));
    if (absent && !required.includes(field)) continue;
    if (!FIELDS[field].valid(value)) return `${field} must be ${FIELDS[field].expected}`;
    picked[field] = value;
  }
  if (atLeastOneOptional && !optional.some((field) => Object.hasOwn(picked, field)))
    return `${optional.join(" or ")} is required`;
  return picked;
}

const UUID_STRING = { type: "string", format: "uuid" };
const INPUT_PROPERTIES = {
  action: {
    type: "string",
    enum: ACTIONS,
    description: `The action, with the fields it takes (? marks an optional one): ${ACTIONS.map(signature).join("; ")}`,
  },
  invocation_id: { ...UUID_STRING, description: "Client-generated UUID; identical retries return the same request" },
  title: {
    type: "string",
    maxLength: 255,
    description: "create_project_note: the note's title. update_document_metadata: the paper's new title",
  },
  content: { type: "string", maxLength: 200000, description: "create_project_note: the Markdown body" },
  tags: {
    type: "array",
    maxItems: 20,
    items: { type: "string", minLength: 1, maxLength: 64 },
    description: "create_project_note: the note's tags. update_document_metadata: replaces the paper's tags; [] clears them",
  },
  document_ids: {
    type: "array",
    minItems: 1,
    maxItems: MAX_DOCUMENT_IDS,
    uniqueItems: true,
    items: UUID_STRING,
    description: "NOUS document ids of the papers to save, remove or move",
  },
  document_id: { ...UUID_STRING, description: "update_document_metadata: the NOUS document to edit" },
  project_id: {
    ...UUID_STRING,
    description:
      "The folder (NOUS project) to act on; list_library gives the ids. A project connection can only name its own project. ingest_arxiv_papers: the folder that receives the papers, required on a workspace connection",
  },
  from_project_id: { ...UUID_STRING, description: "move_papers_between_folders: the folder the papers leave" },
  to_project_id: { ...UUID_STRING, description: "move_papers_between_folders: the folder the papers join" },
  name: {
    type: "string",
    minLength: 1,
    maxLength: 255,
    description: "create_folder: the new folder's name. rename_folder: the folder's new name",
  },
  description: { type: "string", maxLength: 2000, description: "create_folder: what the folder is for" },
  paper_ids: {
    type: "array",
    minItems: 1,
    maxItems: MAX_PAPER_IDS,
    uniqueItems: true,
    items: { type: "string", minLength: 1 },
    description: "ingest_arxiv_papers: arXiv ids such as 2401.12345 or hep-th/9901001",
  },
} satisfies Record<"action" | "invocation_id" | Field, Record<string, unknown>>;

/**
 * Plain-language outcome for the model. Only `succeeded` says the action
 * happened; every other state says what the user or NOUS still has to do.
 */
export function describe(status: ActionStatus): string {
  const id = status.invocation_id;
  switch (status.state) {
    case "awaiting_approval":
      return `Not done yet. The user must approve this action in NOUS: ${status.approval_url ?? "open NOUS and review the pending action"}. Check later with get_action_status (invocation_id ${id}); do not request it again.`;
    case "approved":
    case "executing":
      return `Approved; NOUS is carrying out the action. Check again with get_action_status (invocation_id ${id}).`;
    case "succeeded":
      return `Done. ${JSON.stringify(status.result?.content?.[0] ?? {})}`;
    case "failed":
      return `Not done: ${JSON.stringify(status.result?.content?.[0] ?? "the user denied it or NOUS refused it")}.`;
    case "outcome_unknown":
      return `Outcome unknown: NOUS could not confirm whether the action ran. Do not retry; ask the user to check NOUS (invocation_id ${id}).`;
  }
}

function result(status: ActionStatus): { text: string; structured: Record<string, unknown>; isError?: boolean } {
  return {
    text: describe(status),
    structured: status as unknown as Record<string, unknown>,
    ...(status.state === "failed" || status.state === "outcome_unknown" ? { isError: true } : {}),
  };
}

/**
 * `request_action`: ask NOUS for one note or library action. NOUS decides
 * whether it runs at once (a reversible library change under library:write)
 * or waits for the user's approval; the model can never approve.
 */
export function requestActionTool(client: ActionHttpClient): LocalTool {
  return {
    descriptor: {
      name: "request_action",
      description:
        "Ask NOUS to change the user's library or to add a note to the granted project. Library actions save papers to a folder (a NOUS project), remove them from one or move them between two, create, rename or delete a folder, edit a paper's title or tags, or ingest arXiv papers into a folder; list_library gives the folder ids. Pass the action and only its fields. A project connection acts on its own project only (no new folders, no moves); a workspace connection cannot add a note. With library:write the reversible changes run at once; a note, a folder deletion, an arXiv ingest, and any action without library:write wait until the user approves it in NOUS, and the reply gives the approval link. Reuse the same invocation_id to retry safely; NOUS returns the stored request.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        required: ["action", "invocation_id"],
        properties: INPUT_PROPERTIES,
      },
    },
    async call(args) {
      const { action, invocation_id: invocationId } = args;
      if (typeof action !== "string" || !Object.hasOwn(ACTION_FIELDS, action))
        return { text: `request_action supports action=${ACTIONS.join(" | ")}`, isError: true };
      if (!uuid(invocationId)) return { text: "request_action requires a UUID invocation_id", isError: true };
      const fields = actionArguments(action, args);
      if (typeof fields === "string") return { text: `request_action ${action}: ${fields}`, isError: true };
      try {
        return result(await client.request(action, invocationId, fields));
      } catch (error) {
        if (error instanceof ToolRequestRejected || error instanceof ReauthenticationRequired) throw error;
        const reason = error instanceof Error ? error.message : String(error);
        console.error(`request_action outcome unknown: ${reason}`);
        return {
          text: `request outcome unknown: ${reason}. Retry with the same invocation_id ${invocationId}; NOUS returns the stored request.`,
          isError: true,
        };
      }
    },
  };
}

/** `get_action_status`: read the stored outcome of a requested action. */
export function actionStatusTool(client: ActionHttpClient): LocalTool {
  return {
    descriptor: {
      name: "get_action_status",
      description:
        "Read whether a requested NOUS action is still waiting for approval, is running, is done, failed, or has an unknown outcome.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        required: ["invocation_id"],
        properties: { invocation_id: { type: "string", format: "uuid" } },
      },
    },
    async call(args) {
      if (!uuid(args.invocation_id)) return { text: "get_action_status requires a UUID invocation_id", isError: true };
      try {
        return result(await client.status(args.invocation_id));
      } catch (error) {
        // Actions are scoped to the connection's consent; after a reconnect an
        // earlier request is not visible, which does not mean it never existed.
        if (error instanceof ToolRequestRejected && /not found/i.test(error.message))
          return {
            text: `Action ${args.invocation_id} is not visible to this connection; it may belong to an earlier connection. Do not request it again; ask the user to check NOUS.`,
            isError: true,
          };
        throw error;
      }
    },
  };
}
