import { useEffect, useMemo, useRef, useState } from "react";
import { writeFile } from "node:fs/promises";
import { Box, Text, useApp, useInput } from "ink";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  ThreadPrimitive,
  ThreadListPrimitive,
  ThreadListItemPrimitive,
  LoadingPrimitive,
  StatusBarPrimitive,
  AuiIf,
  DiffView,
  makeAssistantToolUI,
  useAuiState,
  type ToolCallMessagePartProps,
} from "@assistant-ui/react-ink";
import {
  useExternalStoreRuntime,
  ThreadListItemByIndexProvider,
} from "@assistant-ui/core/react";
import {
  createMessageQueue,
  ExportedMessageRepository,
  type ThreadMessage,
} from "@assistant-ui/core";
import { useChatRuntimeAdapter } from "@nous/chat-runtime";
import { convertMessage, HITL_APPROVAL_TOOL } from "@nous/chat-runtime/message";
import { loadConfig, saveConfig } from "../../frontend/cli/auth/store";

import {
  readDraft,
  writeDraft,
  clearDraft,
} from "../../frontend/cli/services/draft";
import { fetchProjects } from "../../frontend/cli/services/projects";
import {
  fetchDocuments,
  fetchProjectDocuments,
  fetchDocument,
} from "../../frontend/cli/services/documents";
import { getApiBase } from "../../frontend/cli/services/client";
import { removeThread } from "../../frontend/cli/services/threadStore";
import { loadHistory } from "../../frontend/cli/services/promptHistory";
import { parseSlashCommand } from "../../frontend/cli/hooks/useSlashCommands";
import { terminalText, validatePrompt } from "./adapter";
import { useTerminalSession } from "./session";
import {
  attachmentAdapter,
  listThreads,
  type Thread,
  fileFromPath,
  copyText,
  updateThread,
  deleteThread,
  type TerminalMessage,
  type BranchHistory,
} from "./services";
import {
  Attachment,
  QuotePreview,
  Button,
  EditComposer,
  Message,
  Queue,
  Suggestions,
  buttonLabel,
  ChoiceMenu,
  CommandComposer,
  FocusedTextInput,
} from "./ui";

const MODELS = [
  "",
  "model-router",
  "gpt-5-mini",
  "gpt-5.6-luna",
] as const satisfies readonly import("../../frontend/src/types/generated/api").components["schemas"]["AgentExecuteRequest"]["model"][];

const SUBMITTED_NOTICE =
  "Action already submitted; waiting for its result. It will not be retried automatically.";

const HELP = `Chat: Enter sends/queues · Ctrl+J or Shift+Enter adds a line · Tab/Shift+Tab selects controls
Ctrl+C stops, then exits · Esc returns to chat · PageUp/PageDown expands/reduces the live transcript
Ctrl+P/Ctrl+N recalls prompt history
/help · /settings · /threads [pick] · /thread <id> · /new · /refresh · /rename <title>
/archive [id] · /unarchive <id> · /delete [id] (requires confirmation)
/projects · /project <id|none> · /papers · /paper <id|none> · /model [model-router|gpt-5-mini|gpt-5.6-luna]
/attach <path> · /documents [search] · /document <id> · /detach <attachment-id>
/copy <message-number> · /quote <message-number> · /edit <user-message-number>
/retry [assistant-message-number] · /branch <message-number> <previous|next>
/like <message-number> · /dislike <message-number> · /search <text> · /export <path> · /diff <old-path> | <new-path>
/queue · /remove <queue-id> · /steer <queue-id> · /quit
Edits and retries create server branches. Original conversations stay available.`;

function ArchivedThreads({
  renderItem,
}: {
  renderItem: (id: string, archived: boolean) => import("react").ReactNode;
}) {
  const ids = useAuiState((s) => s.threads.archivedThreadIds);
  return (
    <>
      {ids.map((id, index) => (
        <ThreadListItemByIndexProvider key={id} index={index} archived>
          {renderItem(id, true)}
        </ThreadListItemByIndexProvider>
      ))}
    </>
  );
}

function MessageRow({
  index,
  report,
}: {
  index: number;
  report: (p: Promise<unknown>) => void;
}) {
  const editing = useAuiState((s) => s.message.composer.isEditing);
  return editing ? <EditComposer /> : <Message index={index} report={report} />;
}
function ApprovalPrompt({
  args,
  approval,
  respondToApproval,
}: ToolCallMessagePartProps<{
  tools: Array<{ name: string; args: Record<string, unknown> }>;
}>) {
  const [answer, setAnswer] = useState("");
  const [submitted, setSubmitted] = useState(false);
  if (approval?.approved !== undefined) return null;
  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor="yellow"
      paddingX={1}
    >
      <Text bold color="yellow">
        Approval required
      </Text>
      {args.tools.map((tool, index) => (
        <Box key={index} flexDirection="column">
          <Text bold>{terminalText(tool.name)}</Text>
          <Text>{terminalText(JSON.stringify(tool.args, null, 2))}</Text>
        </Box>
      ))}
      <Text>
        Type yes to approve or no to deny, then Enter. Ctrl+C cancels.
      </Text>
      <FocusedTextInput
        value={answer}
        onChange={setAnswer}
        submitOnEnter
        onSubmit={(value) => {
          if (submitted) return;
          const choice = value.trim().toLowerCase();
          if (choice !== "yes" && choice !== "no") return;
          setSubmitted(true);
          respondToApproval({ approved: choice === "yes" });
        }}
      />
    </Box>
  );
}

const InkApprovalToolUI = makeAssistantToolUI({
  toolName: HITL_APPROVAL_TOOL,
  render: ApprovalPrompt,
});

export function App({
  initialMessages = [],
  initialHistory,
}: {
  initialMessages?: TerminalMessage[];
  initialHistory?: BranchHistory;
}) {
  const { exit } = useApp();
  const session = useTerminalSession(initialMessages, initialHistory);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [panel, setPanel] = useState<"chat" | "threads" | "help" | "info">(
    "chat",
  );
  const [info, setInfo] = useState("");
  const operation = useRef<{
    controller: AbortController;
    writeStarted: boolean;
  } | null>(null);
  const [commandBusy, setCommandBusy] = useState(false);
  const [dialog, setDialog] = useState<{
    title: string;
    submit: (value: string) => Promise<void>;
  } | null>(null);
  const [selection, setSelection] = useState<{
    title: string;
    options: { value: string; label: string }[];
    onSelect: (value: string) => Promise<void>;
  } | null>(null);
  const [dialogText, setDialogText] = useState("");
  const [windowSize, setWindowSize] = useState(12);
  const [diff, setDiff] = useState<{
    oldFile: { content: string; name: string };
    newFile: { content: string; name: string };
  }>();
  const historyIndex = useRef(-1);
  const contextVersion = useRef(0);
  const config = loadConfig();
  const report = (promise: Promise<unknown>) => {
    void promise.catch((error) =>
      session.setNotice(
        terminalText(error instanceof Error ? error.message : String(error)),
      ),
    );
  };
  const runOperation = async (fn: () => Promise<unknown>) => {
    if (operation.current) return;
    const current = { controller: new AbortController(), writeStarted: false };
    operation.current = current;
    setCommandBusy(true);
    try {
      await fn();
    } catch (error) {
      if (!current.controller.signal.aborted) session.setNotice(String(error));
    } finally {
      if (operation.current === current) {
        session.setNotice((notice) =>
          notice === SUBMITTED_NOTICE ? "" : notice,
        );
        operation.current = null;
        setCommandBusy(false);
      }
    }
  };
  const beginWrite = () => {
    const current = operation.current;
    current?.controller.signal.throwIfAborted();
    if (current) current.writeStarted = true;
  };
  const cancelOperation = () => {
    const current = operation.current;
    if (!current) return true;
    if (current.writeStarted) {
      if (session.isLoading) session.onCancel();
      session.setNotice(SUBMITTED_NOTICE);
      return false;
    }
    current.controller.abort();
    operation.current = null;
    setCommandBusy(false);
    if (session.isLoading) session.onCancel();
    return true;
  };
  const loadThreads = async () => {
    const signal = operation.current?.controller.signal;
    const entries = await listThreads(signal);
    signal?.throwIfAborted();
    setThreads(entries);
  };
  const resetComposer = async () => {
    const composer = runtime.thread.composer;
    for (const item of composer.getState().queue)
      composer.removeQueueItem(item.id);
    await composer.reset();
  };
  const switchThread = async (id: string | null) => {
    if (session.isRunning || session.isLoading)
      throw new Error("Stop the current operation before switching threads.");
    const signal = operation.current?.controller.signal;
    contextVersion.current++;
    await resetComposer();
    signal?.throwIfAborted();
    await session.switchThread(id, signal);
    signal?.throwIfAborted();
    setPanel("chat");
    setWindowSize(12);
  };
  const askDelete = (id: string) => {
    if (session.isRunning || session.isLoading) {
      session.setNotice("Stop the current operation before deleting a thread.");
      return;
    }
    setDialogText("");
    setDialog({
      title: `Delete thread ${id}? Type delete to confirm.`,
      submit: async (value) => {
        if (value !== "delete")
          throw new Error("Type delete exactly, or press Esc to cancel.");
        beginWrite();
        await deleteThread(id);
        await session.forgetThread(id);
        if (loadConfig()?.thread_id === id) await switchThread(null);
        await loadThreads();
        setDialog(null);
      },
    });
  };
  const repository = useMemo(
    () =>
      ExportedMessageRepository.fromBranchableArray(
        session.history.nodes.map((n) => ({
          parentId: n.parentId,
          message: convertMessage(n.message),
        })),
        { headId: session.history.headId },
      ),
    [session.history],
  );
  const shared = useChatRuntimeAdapter(
    { ...session, isSendDisabled: session.isSendDisabled || commandBusy },
    createMessageQueue,
  );
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    ...shared,
    messages: undefined,
    convertMessage: undefined,
    messageRepository: repository,
    isLoading: session.isLoading,
    setMessages: (messages) => session.switchBranch(messages.map((m) => m.id)),
    onEdit: async (message) => {
      try {
        await session.branch(
          message.sourceId!,
          message.content
            .filter((p) => p.type === "text")
            .map((p) => p.text)
            .join("\n"),
        );
      } catch (e) {
        session.setNotice(String(e));
      }
    },
    onReload: async (parentId) => {
      const user = session.messages.find((m) => m.runtimeId === parentId);
      if (user) {
        try {
          await session.branch(user.runtimeId, user.content);
        } catch (e) {
          session.setNotice(String(e));
        }
      }
    },
    suggestions: [
      {
        title: "Research",
        label: "Find sources",
        prompt: "Find and compare recent research on ",
      },
      {
        title: "Summarize",
        label: "Use selected context",
        prompt: "Summarize the key findings in my selected documents.",
      },
      {
        title: "Write",
        label: "Draft with evidence",
        prompt: "Help me draft an outline using my project sources.",
      },
    ],
    adapters: {
      attachments: attachmentAdapter,
      feedback: {
        submit: ({ message, type }) =>
          report(session.feedback(message.id, type)),
      },
      threadList: {
        threadId: config?.thread_id ?? undefined,
        threads: threads
          .filter((t) => t.status !== "archived")
          .map((t) => ({
            id: t.id,
            title: terminalText(t.title || "Untitled"),
            status: "regular",
          })),
        archivedThreads: threads
          .filter((t) => t.status === "archived")
          .map((t) => ({
            id: t.id,
            title: terminalText(t.title || "Untitled"),
            status: "archived",
          })),
        onSwitchToNewThread: () => {
          report(switchThread(null));
        },
        onSwitchToThread: (id) => {
          report(switchThread(id));
        },
        onRename: async (id, title) => {
          await updateThread(id, { title });
          await loadThreads();
        },
        onArchive: async (id) => {
          try {
            if (session.isRunning || session.isLoading)
              throw new Error("Stop the current operation first.");
            await updateThread(id, { status: "archived" });
            await loadThreads();
          } catch (e) {
            session.setNotice(String(e));
          }
        },
        onUnarchive: async (id) => {
          try {
            if (session.isRunning || session.isLoading)
              throw new Error("Stop the current operation first.");
            await updateThread(id, { status: "active" });
            await loadThreads();
          } catch (e) {
            session.setNotice(String(e));
          }
        },
        onDelete: (id) => {
          askDelete(id);
        },
      },
    },
  });

  // Reuse the original CLI's draft file; the composer remains the only live owner.
  useEffect(() => {
    const composer = runtime.thread.composer;
    const restored = readDraft();
    if (restored.trim().length >= 2 && !restored.trim().startsWith("/"))
      composer.setText(restored);
    let previous = composer.getState().text;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const persist = () => {
      const text = composer.getState().text;
      try {
        writeDraft(text.trim().startsWith("/") ? "" : text);
      } catch {
        session.setNotice("Could not save the input draft.");
      }
    };
    const unsubscribe = composer.subscribe(() => {
      const text = composer.getState().text;
      if (text === previous) return;
      previous = text;
      clearTimeout(timer);
      timer = setTimeout(persist, 300);
    });
    return () => {
      unsubscribe();
      clearTimeout(timer);
      persist();
    };
  }, [runtime]);

  const attach = async (path: string) => {
    if (runtime.thread.composer.getState().attachments.length >= 10)
      throw new Error("Attach at most 10 documents.");
    const version = contextVersion.current;
    const signal = operation.current?.controller.signal;
    const file = await fileFromPath(path);
    signal?.throwIfAborted();
    if (version !== contextVersion.current)
      throw new Error(
        "Thread changed before upload. Attach again in the selected thread.",
      );
    beginWrite();
    await runtime.thread.composer.addAttachment(file);
  };
  const askAttach = () => {
    setDialogText("");
    setDialog({
      title: "File path to upload (up to 50 MiB)",
      submit: async (path) => {
        await attach(path);
        setDialog(null);
      },
    });
  };
  const command = async (input: string): Promise<boolean> => {
    const parsed = parseSlashCommand(input);
    if (!parsed) return false;
    const { command: name, args } = parsed;
    const signal = operation.current?.controller.signal;
    // Some clients cannot abort (local files, cached responses); reject late results too.
    const read = async <T,>(pending: Promise<T>): Promise<T> => {
      const value = await pending;
      signal?.throwIfAborted();
      return value;
    };
    const rest = input.trim().replace(/^\/\S+\s*/, "");
    const cfg = loadConfig();
    const requireIdle = () => {
      if (session.isRunning || session.isLoading)
        throw new Error("Stop the current operation first.");
    };
    const messageAt = (arg: string | undefined) => {
      const message = session.messages[Number(arg) - 1];
      if (!message) throw new Error("Use a displayed message number.");
      return message;
    };
    const show = (text: string) => {
      setDiff(undefined);
      setInfo(text);
      setPanel("info");
    };
    switch (name) {
      case "help":
        setPanel("help");
        break;
      case "quit":
      case "exit":
        session.onCancel();
        exit();
        break;
      case "new":
        requireIdle();
        await switchThread(null);
        break;
      case "threads":
        if (args[0] === "pick") {
          requireIdle();
          const entries = await read(listThreads(signal));
          setSelection({
            title: "Pick a thread",
            options: entries.map((t) => ({
              value: t.id,
              label: t.title || t.id,
            })),
            onSelect: switchThread,
          });
          break;
        }
        await loadThreads();
        setPanel("threads");
        break;
      case "thread":
        requireIdle();
        if (!args[0]) {
          show(`Thread: ${cfg?.thread_id || "none"}`);
          break;
        }
        await switchThread(args[0]);
        break;
      case "clear":
        setPanel("chat");
        setWindowSize(4);
        break;
      case "history": {
        const count = args[0] ? Number(args[0]) : 10;
        if (!Number.isInteger(count) || count < 1)
          throw new Error("/history <positive message count>");
        show(
          session.messages
            .slice(-count)
            .map((m) => `${m.role}: ${m.content}`)
            .join("\n\n") || "No messages.",
        );
        break;
      }
      case "forget": {
        requireIdle();
        const id = args[0] || cfg?.thread_id;
        if (!id) throw new Error("Select a thread or provide an ID.");
        removeThread(id);
        if (id === cfg?.thread_id) await switchThread(null);
        session.setNotice("Forgot local selection. Server thread unchanged.");
        break;
      }
      case "context":
        if (args[0] === "clear") await command("/project none");
        else if (["project", "paper"].includes(args[0]) && args[1])
          await command(`/${args[0]} ${args[1]}`);
        else throw new Error("/context project <id> | paper <id> | clear");
        break;
      case "refresh":
        requireIdle();
        await switchThread(cfg?.thread_id ?? null);
        break;
      case "rename":
        requireIdle();
        if (!cfg?.thread_id || !rest)
          throw new Error("Select a thread and provide a title.");
        beginWrite();
        await updateThread(cfg.thread_id, { title: rest });
        await loadThreads();
        break;
      case "archive":
      case "unarchive": {
        requireIdle();
        const id = args[0] || cfg?.thread_id;
        if (!id) throw new Error("Select a saved thread.");
        beginWrite();
        await updateThread(id, {
          status: name === "archive" ? "archived" : "active",
        });
        await loadThreads();
        break;
      }
      case "delete":
        requireIdle();
        if (!args[0] && !cfg?.thread_id)
          throw new Error("Select a saved thread.");
        askDelete(args[0] || cfg!.thread_id!);
        break;
      case "projects":
        requireIdle();
        setSelection({
          title: "Pick a project",
          options: [
            { value: "none", label: "Clear project context" },
            ...(await read(fetchProjects({ signal }))).map((p) => ({
              value: p.id,
              label: p.name,
            })),
          ],
          onSelect: async (id) => {
            await command(`/project ${id}`);
          },
        });
        break;
      case "settings":
        requireIdle();
        if (args[0] === "set") {
          if (args[1] === "model") {
            await command(`/model ${args[2] || "default"}`);
            break;
          }
          if (args[1] !== "api_url" || !args[2] || !cfg)
            throw new Error("/settings set <model|api_url> <value>");
          if (process.env.NOUS_API_URL)
            throw new Error(
              "NOUS_API_URL overrides the saved URL. Change that environment variable before launching.",
            );
          const url = new URL(args[2]);
          if (
            !["https:", "http:"].includes(url.protocol) ||
            url.username ||
            url.password ||
            url.search ||
            url.hash ||
            !url.pathname.replace(/\/$/, "").endsWith("/api/v1")
          )
            throw new Error(
              "Use an HTTP(S) backend URL ending in /api/v1, without credentials, query or fragment.",
            );
          await switchThread(null);
          saveConfig({
            ...loadConfig()!,
            api_url: url.href.replace(/\/$/, ""),
            token: "",
            expires_at: "1970-01-01",
            project_id: null,
            project_name: null,
            paper_id: null,
            paper_title: null,
          });
          exit(
            "Backend changed. Run ./nous login for this backend before chatting.",
          );
          break;
        }
        setSelection({
          title: `Settings · ${cfg?.user_email || ""} · ${getApiBase()}${process.env.NOUS_API_URL ? " (environment override)" : ""}`,
          options: [
            {
              value: "model",
              label: `Model: ${cfg?.model || "server default"}`,
            },
            { value: "api_url", label: "Backend URL" },
          ],
          onSelect: async (key) => {
            if (key === "model") await command("/model");
            else {
              setDialogText(getApiBase());
              setDialog({
                title: "Backend URL (ends in /api/v1)",
                submit: async (url) => {
                  await command(`/settings set api_url ${url}`);
                  setDialog(null);
                },
              });
            }
          },
        });
        break;
      case "papers":
        requireIdle();
        setSelection({
          title: "Pick a paper",
          options: [
            { value: "none", label: "Clear paper context" },
            ...(cfg?.project_id
              ? await read(fetchProjectDocuments(cfg.project_id, { signal }))
              : await read(fetchDocuments({ signal }))
            ).map((d) => ({ value: d.id, label: d.title })),
          ],
          onSelect: async (id) => {
            await command(`/paper ${id}`);
          },
        });
        break;
      case "project": {
        requireIdle();
        if (!cfg || !args[0]) throw new Error("/project <id|none>");
        const project =
          args[0] === "none"
            ? undefined
            : (await read(fetchProjects({ signal }))).find(
                (p) => p.id === args[0],
              );
        if (args[0] !== "none" && !project)
          throw new Error("Project not found. Use /projects.");
        saveConfig({
          ...cfg,
          project_id: project?.id ?? null,
          project_name: project?.name ?? null,
          paper_id: null,
          paper_title: null,
        });
        await switchThread(null);
        break;
      }
      case "documents":
        show(
          (await read(fetchDocuments({ search: rest || undefined, signal })))
            .map((d) => `${d.id}  ${d.title}  (${d.processing_status})`)
            .join("\n") || "No documents.",
        );
        break;
      case "paper": {
        requireIdle();
        if (!cfg || !args[0]) throw new Error("/paper <id|none>");
        const doc =
          args[0] === "none"
            ? null
            : await read(fetchDocument(args[0], { signal }));
        if (args[0] !== "none" && !doc) throw new Error("Document not found.");
        saveConfig({
          ...cfg,
          paper_id: doc?.id ?? null,
          paper_title: doc?.title ?? null,
        });
        await switchThread(null);
        break;
      }
      case "model":
        requireIdle();
        if (!args[0]) {
          setSelection({
            title: "Select model",
            options: MODELS.map((value) => ({
              value: value || "default",
              label: value || "Server default",
            })),
            onSelect: async (value) => {
              await command(`/model ${value}`);
            },
          });
          break;
        }
        if (
          !MODELS.some(
            (value) => value === (args[0] === "default" ? "" : args[0]),
          )
        )
          throw new Error("Use /model to list supported models.");
        if (cfg)
          saveConfig({ ...cfg, model: args[0] === "default" ? "" : args[0] });
        session.setNotice(`Model: ${args[0]}`);
        break;
      case "attach":
        if (!rest) askAttach();
        else await attach(rest);
        break;
      case "document": {
        const doc = await read(fetchDocument(args[0] ?? "", { signal }));
        if (!doc) throw new Error("Document not found.");
        if (runtime.thread.composer.getState().attachments.length >= 10)
          throw new Error("Attach at most 10 documents.");
        await runtime.thread.composer.addAttachment({
          id: doc.id,
          name: doc.title,
          type: "document",
          content: [],
        });
        break;
      }
      case "detach": {
        const index = runtime.thread.composer
          .getState()
          .attachments.findIndex((a) => a.id === args[0]);
        if (index < 0) throw new Error("Attachment not found.");
        await runtime.thread.composer.getAttachmentByIndex(index).remove();
        break;
      }
      case "copy":
        beginWrite();
        await copyText(messageAt(args[0]).content);
        session.setNotice("Copied.");
        break;
      case "quote": {
        const m = messageAt(args[0]);
        runtime.thread.composer.setQuote({
          messageId: m.runtimeId,
          text: m.content,
        });
        break;
      }
      case "edit": {
        requireIdle();
        const m = messageAt(args[0]);
        if (m.role !== "user") throw new Error("Edit a user message.");
        setWindowSize(session.messages.length);
        setPanel("chat");
        runtime.thread.getMessageById(m.runtimeId).composer.beginEdit();
        break;
      }
      case "retry": {
        requireIdle();
        const m = args[0] ? messageAt(args[0]) : session.messages.at(-1);
        if (!m || m.role !== "assistant")
          throw new Error("Retry an assistant message.");
        beginWrite();
        await runtime.thread.getMessageById(m.runtimeId).reload();
        break;
      }
      case "branch": {
        requireIdle();
        const m = messageAt(args[0]);
        if (args[1] !== "previous" && args[1] !== "next")
          throw new Error("/branch <message-number> <previous|next>");
        runtime.thread
          .getMessageById(m.runtimeId)
          .switchToBranch({ position: args[1] });
        break;
      }
      case "like":
      case "dislike": {
        const m = messageAt(args[0]);
        beginWrite();
        await session.feedback(
          m.runtimeId,
          name === "like" ? "positive" : "negative",
        );
        break;
      }
      case "search":
        show(
          session.messages
            .flatMap((m, i) =>
              m.content.toLowerCase().includes(rest.toLowerCase())
                ? [`${i + 1}. ${m.role}: ${m.content}`]
                : [],
            )
            .join("\n\n") || "No matches.",
        );
        break;
      case "export":
        if (!rest) throw new Error("/export <path>");
        beginWrite();
        await writeFile(
          rest,
          session.messages
            .map((m) => `## ${m.role}\n\n${m.content}`)
            .join("\n\n"),
          { flag: "wx", mode: 0o600 },
        );
        session.setNotice(`Exported to ${rest}`);
        break;
      case "diff": {
        const paths = rest.split("|").map((p) => p.trim());
        if (paths.length !== 2)
          throw new Error("/diff <old-path> | <new-path>");
        const [oldFile, newFile] = await read(
          Promise.all(paths.map(fileFromPath)),
        );
        const [oldText, newText] = await read(
          Promise.all([oldFile.text(), newFile.text()]),
        );
        setDiff({
          oldFile: { name: oldFile.name, content: terminalText(oldText) },
          newFile: { name: newFile.name, content: terminalText(newText) },
        });
        setPanel("info");
        break;
      }
      case "queue":
        show(
          runtime.thread.composer
            .getState()
            .queue.map((q) => `${q.id}  ${q.prompt}`)
            .join("\n") || "Queue is empty.",
        );
        break;
      case "remove":
        runtime.thread.composer.removeQueueItem(args[0] ?? "");
        break;
      case "steer":
        runtime.thread.composer.moveQueueItem(args[0] ?? "", {
          insertBefore: runtime.thread.composer.getState().queue[0]?.id ?? null,
        });
        break;
      default:
        throw new Error(`Unknown command /${name}. Use /help.`);
    }
    return true;
  };
  const submit = (text: string) => {
    if (text.trim().startsWith("/")) {
      runtime.thread.composer.setText("");
      void runOperation(() => command(text));
    } else {
      const draft = runtime.thread.composer.getState();
      const prompt = draft.quote
        ? `> ${draft.quote.text.replace(/\n/g, "\n> ")}\n\n${text}`
        : text;
      try {
        validatePrompt(
          prompt ||
            (draft.attachments.length ? "Use the attached documents." : ""),
          draft.attachments.map((a) => a.id),
        );
      } catch (error) {
        session.setNotice(String(error));
        return;
      }
      setPanel("chat");
      runtime.thread.composer.send({ steer: false });
      try {
        clearDraft();
      } catch {
        session.setNotice("Could not clear the saved draft.");
      }
    }
  };
  useInput((input, key) => {
    const editing = runtime.thread
      .getState()
      .messages.map(
        (message) => runtime.thread.getMessageById(message.id).composer,
      )
      .find((composer) => composer.getState().isEditing);
    if (editing && (key.escape || (key.ctrl && input === "c"))) {
      editing.cancel();
      return;
    }
    if (input === "c" && key.ctrl) {
      if (operation.current) {
        if (cancelOperation()) {
          setSelection(null);
          setDialog(null);
        }
        return;
      }
      if (selection || dialog) {
        setSelection(null);
        setDialog(null);
        return;
      }
      if (session.isRunning) runtime.thread.cancelRun();
      else if (session.isLoading) session.onCancel();
      else exit();
    }
    if (key.escape) {
      if (!cancelOperation()) return;
      setSelection(null);
      setDialog(null);
      setPanel("chat");
      setDiff(undefined);
    }
    if (key.pageUp)
      setWindowSize((n) => Math.min(session.messages.length, n + 10));
    if (key.pageDown) setWindowSize((n) => Math.max(4, n - 10));
    if (
      key.ctrl &&
      (input === "p" || input === "n") &&
      !dialog &&
      !selection &&
      !editing
    ) {
      const prompts = loadHistory();
      historyIndex.current = Math.max(
        -1,
        Math.min(
          prompts.length - 1,
          historyIndex.current + (input === "p" ? 1 : -1),
        ),
      );
      runtime.thread.composer.setText(
        historyIndex.current < 0
          ? ""
          : prompts[prompts.length - 1 - historyIndex.current],
      );
    }
  });
  const threadRow = (id: string, archived = false) => (
    <ThreadListItemPrimitive.Root key={id} gap={1}>
      <ThreadListItemPrimitive.Trigger>
        {({ isFocused }) => (
          <Text inverse={isFocused}>
            <ThreadListItemPrimitive.Title fallback="Untitled" />
          </Text>
        )}
      </ThreadListItemPrimitive.Trigger>
      <Text dimColor>{id}</Text>
      {archived ? (
        <ThreadListItemPrimitive.Unarchive>
          {buttonLabel("Restore")}
        </ThreadListItemPrimitive.Unarchive>
      ) : (
        <ThreadListItemPrimitive.Archive>
          {buttonLabel("Archive")}
        </ThreadListItemPrimitive.Archive>
      )}
      <ThreadListItemPrimitive.Delete>
        {buttonLabel("Delete")}
      </ThreadListItemPrimitive.Delete>
    </ThreadListItemPrimitive.Root>
  );
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <InkApprovalToolUI />
      <Box flexDirection="column" paddingX={1}>
        <Text bold color="cyan">
          NOUS ·{" "}
          {terminalText(
            [
              config?.project_name,
              config?.paper_title,
              config?.thread_id || "New chat",
            ]
              .filter(Boolean)
              .join(" · "),
          )}
        </Text>
        <Text dimColor>
          Enter sends or queues · Ctrl+J newline · Tab controls · /help · Ctrl+C
          stop/exit
        </Text>
        {commandBusy && <Text color="yellow">Working on command…</Text>}
        {session.notice && (
          <Text color="yellow">{terminalText(session.notice)}</Text>
        )}
        {selection ? (
          <ChoiceMenu
            key={selection.title}
            title={selection.title}
            options={selection.options}
            onSelect={(value) => {
              const selected = selection;
              setSelection(null);
              void runOperation(() => selected.onSelect(value));
            }}
          />
        ) : dialog ? (
          <Box flexDirection="column" borderStyle="round">
            <Text>{terminalText(dialog.title)}</Text>
            <FocusedTextInput
              value={dialogText}
              onChange={setDialogText}
              submitOnEnter
              onSubmit={(value) => {
                void runOperation(() => dialog.submit(value));
              }}
            />
          </Box>
        ) : (
          <>
            {panel === "threads" && (
              <ThreadListPrimitive.Root flexDirection="column">
                <ThreadListPrimitive.New>
                  {buttonLabel("New chat")}
                </ThreadListPrimitive.New>
                <ThreadListPrimitive.Items
                  renderItem={({ threadId }) => threadRow(threadId)}
                />
                <Text dimColor>Archived</Text>
                <ArchivedThreads renderItem={threadRow} />
                <Text dimColor>
                  Server threads across your workspaces, including branches.
                </Text>
              </ThreadListPrimitive.Root>
            )}
            {panel === "help" && <Text>{HELP}</Text>}
            {panel === "info" &&
              (diff ? (
                <DiffView {...diff} showLineNumbers maxLines={100} />
              ) : (
                <Text>{terminalText(info)}</Text>
              ))}
            {panel === "chat" && (
              <ThreadPrimitive.Root>
                <Suggestions />
                <ThreadPrimitive.Messages
                  windowSize={windowSize}
                  windowOverscan={0}
                >
                  {({ message }) => (
                    <MessageRow
                      index={session.messages.findIndex(
                        (m) => m.runtimeId === message.id,
                      )}
                      report={report}
                    />
                  )}
                </ThreadPrimitive.Messages>
              </ThreadPrimitive.Root>
            )}
            <AuiIf condition={(s) => s.thread.isRunning}>
              <LoadingPrimitive.Root gap={1}>
                <LoadingPrimitive.Spinner variant="dots" />
                <LoadingPrimitive.Text>Working…</LoadingPrimitive.Text>
                <LoadingPrimitive.ElapsedTime />
              </LoadingPrimitive.Root>
            </AuiIf>
            <Queue
              onSteer={(id) => {
                const first = runtime.thread.composer.getState().queue[0]?.id;
                if (first && first !== id)
                  runtime.thread.composer.moveQueueItem(id, {
                    insertBefore: first,
                  });
              }}
            />
            {!session.isSendDisabled && (
              <AuiIf
                condition={(s) =>
                  !s.thread.messages.some((m) => m.composer.isEditing)
                }
              >
                <ComposerPrimitive.Root
                  flexDirection="column"
                  borderStyle="round"
                  borderColor="cyan"
                  paddingX={1}
                >
                  <ComposerPrimitive.Quote>
                    <Text dimColor>Quoting:</Text>
                    <QuotePreview />
                    <ComposerPrimitive.QuoteDismiss>
                      {buttonLabel("Clear quote")}
                    </ComposerPrimitive.QuoteDismiss>
                  </ComposerPrimitive.Quote>
                  <ComposerPrimitive.Attachments>
                    {() => <Attachment />}
                  </ComposerPrimitive.Attachments>
                  <CommandComposer
                    key={session.history.headId ?? "new"}
                    onSubmit={submit}
                  />
                  <Box gap={1}>
                    <Button
                      onPress={() =>
                        submit(runtime.thread.composer.getState().text)
                      }
                    >
                      Send
                    </Button>
                    <ComposerPrimitive.Cancel>
                      {buttonLabel("Stop")}
                    </ComposerPrimitive.Cancel>
                    <ComposerPrimitive.AddAttachment
                      {...{ onPress: askAttach }}
                    >
                      {buttonLabel("Attach")}
                    </ComposerPrimitive.AddAttachment>
                    <Button
                      onPress={() =>
                        void runOperation(async () => {
                          await loadThreads();
                          setPanel("threads");
                        })
                      }
                    >
                      Threads
                    </Button>
                    <Button onPress={() => setPanel("help")}>Help</Button>
                  </Box>
                </ComposerPrimitive.Root>
              </AuiIf>
            )}
          </>
        )}
        <StatusBarPrimitive.Root gap={1}>
          <StatusBarPrimitive.Status />
          <StatusBarPrimitive.ModelName
            name={terminalText(config?.model || "server default")}
          />
          <StatusBarPrimitive.MessageCount />
          <StatusBarPrimitive.TokenCount />
          <StatusBarPrimitive.Latency />
        </StatusBarPrimitive.Root>
      </Box>
    </AssistantRuntimeProvider>
  );
}
