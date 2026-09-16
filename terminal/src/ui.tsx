import {
  useEffect,
  useRef,
  useState,
  type ComponentProps,
  type ReactNode,
} from "react";
import { Box, Text, useFocus, useFocusManager, useInput } from "ink";
import {
  ActionBarPrimitive,
  AuiIf,
  AttachmentPrimitive,
  BranchPickerPrimitive,
  ChainOfThoughtPrimitive,
  ComposerPrimitive,
  DiffView,
  MessagePrimitive,
  MessagePartPrimitive,
  QueueItemPrimitive,
  SuggestionPrimitive,
  ThreadPrimitive,
  ToolCallPrimitive,
  TextInput,
  useAui,
  useAuiState,
  type ToolCallMessagePartProps,
} from "@assistant-ui/react-ink";
import { MarkdownText } from "@assistant-ui/react-ink-markdown";
import { terminalText } from "./adapter";
import { copyText } from "./services";

/** Recover after Escape or a previous editor unmounts; keep text in its owner. */
export function FocusedTextInput(props: ComponentProps<typeof TextInput>) {
  const { activeId } = useFocusManager();
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (activeId === undefined && props.autoFocus !== false)
      setVersion((value) => value + 1);
  }, [activeId, props.autoFocus]);
  return <TextInput key={version} {...props} />;
}

export function Button({
  children,
  onPress,
  disabled = false,
}: {
  children: ReactNode;
  onPress: () => void;
  disabled?: boolean;
}) {
  const { isFocused } = useFocus({ isActive: !disabled });
  useInput(
    (_input, key) => {
      if (key.return) onPress();
    },
    { isActive: isFocused && !disabled },
  );
  return (
    <Text inverse={isFocused} dimColor={disabled}>
      [{children}]
    </Text>
  );
}
export const buttonLabel =
  (label: string) =>
  ({ isFocused, disabled }: { isFocused: boolean; disabled: boolean }) => (
    <Text inverse={isFocused} dimColor={disabled}>
      [{label}]
    </Text>
  );
export function TextPart({ text }: { text: string }) {
  return <MarkdownText text={terminalText(text)} />;
}

function ToolPart(props: ToolCallMessagePartProps) {
  return (
    <ToolCallPrimitive.Fallback
      {...props}
      renderHeader={({ toolName, status, expanded }) => (
        <Text color="yellow">
          {expanded ? "▾" : "▸"} {terminalText(toolName)} ·{" "}
          {terminalText(status)}
        </Text>
      )}
      renderArgs={({ argsText }) => (
        <Text dimColor>{terminalText(argsText)}</Text>
      )}
      renderResult={({ result, isError }) => {
        const text = terminalText(
          typeof result === "string" ? result : (JSON.stringify(result) ?? ""),
        );
        return text.startsWith("diff --git") ||
          (text.startsWith("--- ") && text.includes("\n@@")) ? (
          <DiffView patch={text} maxLines={50} showLineNumbers />
        ) : (
          <Text color={isError ? "red" : undefined}>{text}</Text>
        );
      }}
    />
  );
}
function Reasoning() {
  const text = useAuiState((s) =>
    s.part.type === "reasoning" ? s.part.text : "",
  );
  return <Text dimColor>{terminalText(text)}</Text>;
}
function ThoughtGroup() {
  const collapsed = useAuiState((s) => s.chainOfThought.collapsed);
  const pending = useAuiState((s) =>
    s.message.content.some(
      (p) =>
        p.type === "tool-call" &&
        p.approval &&
        p.approval.approved === undefined,
    ),
  );
  return (
    <ChainOfThoughtPrimitive.Root flexDirection="column">
      <ChainOfThoughtPrimitive.AccordionTrigger>
        {buttonLabel(
          collapsed ? "Show tools / reasoning" : "Hide tools / reasoning",
        )}
      </ChainOfThoughtPrimitive.AccordionTrigger>
      {(!collapsed || pending) && (
        <ChainOfThoughtPrimitive.Parts
          components={{ Reasoning, tools: { Fallback: ToolPart } }}
        />
      )}
    </ChainOfThoughtPrimitive.Root>
  );
}
export function QuotePreview() {
  const text = useAuiState((s) => s.composer.quote?.text ?? "");
  return (
    <ComposerPrimitive.QuoteText>
      {terminalText(text)}
    </ComposerPrimitive.QuoteText>
  );
}
export function Attachment({ removable = true }: { removable?: boolean }) {
  const name = useAuiState((s) => s.attachment.name);
  return (
    <AttachmentPrimitive.Root gap={1}>
      <AttachmentPrimitive.Thumb>
        {terminalText(name.split(".").at(-1) || "file")}
      </AttachmentPrimitive.Thumb>
      <Text>{terminalText(name)}</Text>
      <AttachmentPrimitive.Status showComplete />
      {removable && (
        <AttachmentPrimitive.Remove>
          {buttonLabel("Detach")}
        </AttachmentPrimitive.Remove>
      )}
    </AttachmentPrimitive.Root>
  );
}
export function Message({
  index,
  report,
}: {
  index: number;
  report: (promise: Promise<unknown>) => void;
}) {
  const message = useAuiState((s) => s.message);
  const aui = useAui();
  const custom = message.metadata.custom;
  return (
    <MessagePrimitive.Root flexDirection="column" marginBottom={1}>
      <Text bold color={message.role === "user" ? "green" : "cyan"}>
        {index + 1}. {message.role === "user" ? "You" : "NOUS"}
      </Text>
      <MessagePrimitive.Parts
        components={{
          Text: TextPart,
          ChainOfThought: ThoughtGroup,
          Image: () => <MessagePartPrimitive.Image />,
          File: () => <MessagePartPrimitive.File />,
          Source: () => <MessagePartPrimitive.Source />,
          data: { Fallback: () => <MessagePartPrimitive.Data /> },
        }}
      />
      <MessagePrimitive.Attachments>
        {() => <Attachment removable={false} />}
      </MessagePrimitive.Attachments>
      {typeof custom.activity === "string" && (
        <Text dimColor>{terminalText(custom.activity)}</Text>
      )}
      {Array.isArray(custom.contexts) && custom.contexts.length > 0 && (
        <Box flexDirection="column">
          <Text dimColor>Sources</Text>
          {custom.contexts.map((source, i) => (
            <Text key={i} dimColor>
              [{i + 1}]{" "}
              {terminalText(
                String(source?.title ?? source?.document_title ?? "Untitled"),
              )}{" "}
              {terminalText(String(source?.document_id ?? ""))}
            </Text>
          ))}
        </Box>
      )}
      {message.status?.type === "incomplete" && (
        <Text color="red">
          {terminalText(String(message.status.error ?? message.status.reason))}
        </Text>
      )}
      <AuiIf
        condition={(s) =>
          !s.thread.isRunning &&
          !s.thread.messages.some((m) => m.composer.isEditing)
        }
      >
        <Box gap={1} flexWrap="wrap">
          <ActionBarPrimitive.Copy
            copyToClipboard={(text) => {
              const p = copyText(text);
              report(p);
              return p.catch(() => {});
            }}
          >
            {({ isCopied }) => <Text>[{isCopied ? "Copied" : "Copy"}]</Text>}
          </ActionBarPrimitive.Copy>
          <Button
            onPress={() =>
              aui.composer().setQuote({
                messageId: message.id,
                text: message.content
                  .filter((p) => p.type === "text")
                  .map((p) => p.text)
                  .join("\n"),
              })
            }
          >
            Quote
          </Button>
          {message.role === "user" ? (
            <ActionBarPrimitive.Edit>
              {buttonLabel("Edit")}
            </ActionBarPrimitive.Edit>
          ) : (
            <>
              <ActionBarPrimitive.Reload>
                {buttonLabel("Retry / branch")}
              </ActionBarPrimitive.Reload>
              <ActionBarPrimitive.FeedbackPositive>
                {({ isSubmitted }) => (
                  <Text>[{isSubmitted ? "Liked ✓" : "Like"}]</Text>
                )}
              </ActionBarPrimitive.FeedbackPositive>
              <ActionBarPrimitive.FeedbackNegative>
                {({ isSubmitted }) => (
                  <Text>[{isSubmitted ? "Disliked ✓" : "Dislike"}]</Text>
                )}
              </ActionBarPrimitive.FeedbackNegative>
            </>
          )}
          <BranchPickerPrimitive.Previous>
            {buttonLabel("Previous branch")}
          </BranchPickerPrimitive.Previous>
          <Text>
            <BranchPickerPrimitive.Number />/<BranchPickerPrimitive.Count />
          </Text>
          <BranchPickerPrimitive.Next>
            {buttonLabel("Next branch")}
          </BranchPickerPrimitive.Next>
        </Box>
      </AuiIf>
    </MessagePrimitive.Root>
  );
}
export function EditComposer() {
  return (
    <ComposerPrimitive.Root
      flexDirection="column"
      borderStyle="round"
      borderColor="yellow"
    >
      <Text>Edit message — Enter saves as a new branch; Tab to Cancel</Text>
      <ComposerPrimitive.Input multiLine submitOnEnter autoFocus />
      <Box gap={1}>
        <ComposerPrimitive.Send>
          {buttonLabel("Save branch")}
        </ComposerPrimitive.Send>
        <ComposerPrimitive.Cancel>
          {buttonLabel("Cancel edit")}
        </ComposerPrimitive.Cancel>
      </Box>
    </ComposerPrimitive.Root>
  );
}
export function Queue({ onSteer }: { onSteer: (id: string) => void }) {
  return (
    <ComposerPrimitive.Queue>
      {({ queueItem }) => (
        <Box gap={1}>
          <Text dimColor>
            Queued:{" "}
            <QueueItemPrimitive.Text>
              {terminalText(
                queueItem.parts
                  .filter((p) => p.type === "text")
                  .map((p) => p.text)
                  .join("\n"),
              )}
            </QueueItemPrimitive.Text>
          </Text>
          <QueueItemPrimitive.Steer
            {...{ onPress: () => onSteer(queueItem.id) }}
          >
            {buttonLabel("Next")}
          </QueueItemPrimitive.Steer>
          <QueueItemPrimitive.Remove>
            {buttonLabel("Remove")}
          </QueueItemPrimitive.Remove>
        </Box>
      )}
    </ComposerPrimitive.Queue>
  );
}
export function Suggestions() {
  return (
    <AuiIf condition={(s) => s.thread.isEmpty}>
      <Text dimColor>Choose a starting point, or write your own prompt.</Text>
      <ThreadPrimitive.Suggestions>
        {() => (
          <SuggestionPrimitive.Trigger>
            {({ isFocused }) => (
              <Text inverse={isFocused}>
                <SuggestionPrimitive.Title /> —{" "}
                <SuggestionPrimitive.Description />
              </Text>
            )}
          </SuggestionPrimitive.Trigger>
        )}
      </ThreadPrimitive.Suggestions>
    </AuiIf>
  );
}

export function ChoiceMenu({
  title,
  options,
  onSelect,
}: {
  title: string;
  options: { value: string; label: string }[];
  onSelect: (value: string) => void;
}) {
  const [index, setIndex] = useState(0);
  useInput((_input, key) => {
    if (key.upArrow) setIndex((i) => Math.max(0, i - 1));
    if (key.downArrow) setIndex((i) => Math.min(options.length - 1, i + 1));
    if (key.return && options[index]) onSelect(options[index].value);
  });
  const start = Math.max(0, index - 7);
  return (
    <Box flexDirection="column" borderStyle="round" paddingX={1}>
      <Text bold>{terminalText(title)}</Text>
      {options.slice(start, start + 10).map((option, offset) => (
        <Text key={option.value} inverse={start + offset === index}>
          {start + offset === index ? "› " : "  "}
          {terminalText(option.label)}
        </Text>
      ))}
      <Text dimColor>
        {options.length
          ? "↑/↓ choose · Enter select · Esc cancel"
          : "No entries · Esc return"}
      </Text>
    </Box>
  );
}

const COMMANDS = [
  ["/help", "Show commands and keyboard controls"],
  ["/new", "Start a new conversation"],
  ["/threads [pick]", "Browse saved conversations"],
  ["/thread [id]", "Show or switch the current thread"],
  ["/projects", "Choose a project"],
  ["/project <id|none>", "Set project context"],
  ["/papers", "Choose a paper"],
  ["/paper <id|none>", "Set paper context"],
  ["/settings", "Open settings"],
  ["/model [name]", "Choose a model"],
  ["/history [n]", "Show recent messages"],
  ["/context project|paper <id>", "Set context; use /context clear to reset"],
  ["/refresh", "Reload the current conversation"],
  ["/clear", "Reduce the interactive transcript"],
  ["/forget [id]", "Forget local thread selection"],
  ["/rename <title>", "Rename the current thread"],
  ["/archive [id]", "Archive a thread"],
  ["/unarchive <id>", "Restore a thread"],
  ["/delete [id]", "Delete a thread after confirmation"],
  ["/attach <path>", "Upload an attachment"],
  ["/documents [search]", "Find documents"],
  ["/document <id>", "Attach an existing document"],
  ["/detach <id>", "Remove an attachment"],
  ["/copy <number>", "Copy a message"],
  ["/quote <number>", "Quote a message"],
  ["/edit <number>", "Edit a user message"],
  ["/retry [number]", "Regenerate an assistant reply"],
  ["/branch <number> <previous|next>", "Switch message branches"],
  ["/like <number>", "Send positive feedback"],
  ["/dislike <number>", "Send negative feedback"],
  ["/search <text>", "Search the transcript"],
  ["/export <path>", "Export the current branch"],
  ["/diff <old-path> | <new-path>", "Compare two files"],
  ["/queue", "Show queued prompts"],
  ["/remove <queue-id>", "Remove a queued prompt"],
  ["/steer <queue-id>", "Move a queued prompt to run next"],
  ["/quit", "Exit NOUS"],
  ["/exit", "Exit NOUS"],
].map(([syntax, description]) => ({
  name: syntax.split(" ")[0],
  syntax,
  description,
}));

/** Keep the SDK line editor; only command discovery and completion are app-owned. */
export function CommandComposer({
  onSubmit,
}: {
  onSubmit: (text: string) => void;
}) {
  const aui = useAui();
  const text = useAuiState((s) => s.composer.text);
  const { activeId } = useFocusManager();
  const inputFocus = useRef<string | undefined>(undefined);
  const [navigation, setNavigation] = useState({ query: "", index: 0 });
  const [dismissed, setDismissed] = useState<string | null>(null);
  const query = text.trimStart();
  const matches = /^\/\S*$/.test(query)
    ? COMMANDS.filter((command) => command.name.startsWith(query)).sort(
        (a, b) => Number(b.name === query) - Number(a.name === query),
      )
    : [];
  const visible =
    matches.length > 0 &&
    dismissed !== text &&
    activeId !== undefined &&
    activeId === inputFocus.current;
  const index = Math.min(
    navigation.query === text ? navigation.index : 0,
    Math.max(0, matches.length - 1),
  );
  useInput((_input, key) => {
    if (visible && key.upArrow)
      setNavigation({ query: text, index: Math.max(0, index - 1) });
    if (visible && key.downArrow)
      setNavigation({
        query: text,
        index: Math.min(matches.length - 1, index + 1),
      });
    if (key.escape) {
      setDismissed(text);
    }
  });
  const start = Math.max(0, index - 5);
  return (
    <Box flexDirection="column">
      {visible && (
        <Box flexDirection="column" marginBottom={1}>
          <Text bold color="cyan">
            Commands ({matches.length})
          </Text>
          {matches.slice(start, start + 6).map((command, offset) => (
            <Text key={command.name} inverse={start + offset === index}>
              {start + offset === index ? "› " : "  "}
              {command.syntax} — {command.description}
            </Text>
          ))}
          <Text dimColor>↑/↓ select · Enter complete · Esc dismiss</Text>
        </Box>
      )}
      <FocusedTextInput
        value={text}
        multiLine
        submitOnEnter
        placeholder="Ask NOUS…"
        onChange={(value) => {
          inputFocus.current = activeId;
          setDismissed(null);
          aui.composer.setText(value);
        }}
        onSubmit={(value) => {
          if (
            visible &&
            matches[index] &&
            matches[index].name !== value.trim()
          ) {
            aui.composer.setText(`${matches[index].name} `);
            return;
          }
          onSubmit(value);
        }}
      />
    </Box>
  );
}
