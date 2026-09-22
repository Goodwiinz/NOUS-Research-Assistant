'use client';

import {
  ComposerPrimitive,
  QueueItemPrimitive,
  useAui,
} from '@assistant-ui/react';
import { cn } from '@/lib/utils';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {
  SlashCommandMenu,
  SLASH_LISTBOX_ID,
  slashOptionId,
} from './SlashCommandMenu';
import { useSlashCommandMenu } from './useSlashCommandMenu';
import {
  isSlashTrigger,
  type SlashCommand,
  type SlashCommandId,
} from './slashCommands';
import {
  AlertCircle,
  ArrowRight,
  Image as ImageIcon,
  Loader2,
  Mic,
  Paperclip,
  Square,
  X,
} from 'lucide-react';
import React, {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';

interface ChatInputProps {
  value: string;
  onChange: (value: string) => void;
  /** Sends the composed turn. Receives the document ids of the attachment
   * chips still present, so removing a chip un-attaches its document. */
  /** Return false when the host handled the draft locally and the runtime
   * must not send it. */
  onSubmit: (attachmentIds: string[]) => boolean | void;
  isLoading: boolean;
  /** Hard-disables the composer without swapping Send for Stop — used while
   * the session is still initializing, when a submit would be dropped. */
  disabled?: boolean;
  enableRAG: boolean;
  onRAGToggle: (enabled: boolean) => void;
  inputRef?: React.RefObject<HTMLTextAreaElement>;
  /**
   * Hands the picked files to the host, which owns the upload. Resolving with
   * one outcome per file (in the same order) settles the composer's chips;
   * returning void keeps them optimistic.
   */
  onAttach?: (
    files: FileList
  ) => void | Promise<{ ok: boolean; documentId?: string }[] | void>;
  // Slash commands
  onCommand?: (id: SlashCommandId) => void;
}

type SpeechRecognitionEventLike = {
  results: ArrayLike<ArrayLike<{ transcript: string }>>;
};
type SpeechRecognitionInstance = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
  start: () => void;
  stop: () => void;
};
type SpeechRecognitionCtor = new () => SpeechRecognitionInstance;

/** Speech-recognition availability cannot change for the life of the page. */
function _subscribeNever(): () => void {
  return () => {};
}

function getSpeechRecognition(): SpeechRecognitionCtor | null {
  if (typeof window === 'undefined') return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function ChatInput({
  value,
  onChange,
  onSubmit,
  isLoading,
  disabled = false,
  enableRAG,
  onRAGToggle,
  inputRef,
  onAttach,
  onCommand,
}: ChatInputProps) {
  const aui = useAui();
  const internalRef = useRef<HTMLTextAreaElement>(null);
  const textareaRef = inputRef ?? internalRef;
  const [isFocused, setIsFocused] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [pendingCommand, setPendingCommand] = useState<SlashCommand | null>(
    null
  );
  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null);
  const draftSelectionRef = useRef<{ start: number; end: number } | null>(null);
  // Browser capability, read through useSyncExternalStore so the server
  // snapshot is explicitly `false`. It used to be useState(false) + a mount
  // effect, which is a setState-in-effect (an extra render) — and it could not
  // simply become a lazy initializer, because getSpeechRecognition() reads
  // `window` and would hydrate mismatched.
  const voiceSupported = useSyncExternalStore(
    _subscribeNever,
    () => getSpeechRecognition() !== null,
    () => false
  );
  const menu = useSlashCommandMenu(value);

  // Local attachment receipts — chips shown in the composer for files the user
  // attached this turn. The page still owns the actual upload via onAttach;
  // these are the visual record (with image thumbnails) the composer lacked.
  type Attachment = {
    id: string;
    name: string;
    isImage: boolean;
    url?: string;
    /**
     * A chip that shows the filename the instant it is picked implies the
     * file landed, so it tracks the upload the page is actually running.
     * Hosts whose `onAttach` reports nothing back stay on 'done'.
     */
    state: 'uploading' | 'done' | 'error';
    /** The uploaded document this chip stands for, once the host reports it.
     * Absent while uploading, on failure, and for hosts that report no
     * outcomes — those chips are a visual record only and attach nothing. */
    documentId?: string;
  };
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const attachmentStatus = attachments.reduce(
    (status, attachment) => {
      return {
        uploading: status.uploading || attachment.state === 'uploading',
        error: status.error || attachment.state === 'error',
      };
    },
    { uploading: false, error: false }
  );
  const hasUnsettledAttachments =
    attachmentStatus.uploading || attachmentStatus.error;
  const [submittedDraft, setSubmittedDraft] = useState<string | null>(null);
  const submittedDraftBecameBusyRef = useRef(false);
  // Monotonic counter behind each chip's id — see addFiles.
  const attachSeq = useRef(0);
  // Keep a live ref so the unmount cleanup revokes the current object URLs
  // without re-running on every attachment change.
  const attachmentsRef = useRef<Attachment[]>(attachments);
  // Written in an effect rather than during render (react-hooks/refs): the
  // only reader is the unmount cleanup below, so post-commit timing is fine.
  useEffect(() => {
    attachmentsRef.current = attachments;
  }, [attachments]);

  const addFiles = (files: FileList): void => {
    // Kick the upload off first: whether the host reports back decides
    // whether the chips start optimistic or start pending.
    const outcomes = onAttach?.(files);

    const next: Attachment[] = Array.from(files).map((file) => {
      const isImage = file.type.startsWith('image/');
      return {
        // Identity is per pick, not per file: the input is reset after every
        // selection so the same file can be attached twice, and outcomes are
        // routed by id. A metadata-derived id would make those two chips
        // indistinguishable, letting one batch settle the other's chips.
        id: `att-${(attachSeq.current += 1)}`,
        name: file.name,
        isImage,
        url: isImage ? URL.createObjectURL(file) : undefined,
        state: outcomes ? ('uploading' as const) : ('done' as const),
      };
    });
    setAttachments((prev) => [...prev, ...next]);

    if (!outcomes) return;

    // Outcomes come back in FileList order, so they zip onto this batch's ids
    // by index. A chip the user removed mid-upload is absent from `prev` and
    // is simply skipped.
    const indexById = new Map(next.map((att, i) => [att.id, i]));
    const settle = (
      results: { ok: boolean; documentId?: string }[] | void
    ): void =>
      setAttachments((prev) =>
        prev.map((att) => {
          const i = indexById.get(att.id);
          if (i === undefined) return att;
          const result = results?.[i];
          // A host that resolves without per-file outcomes gets the old
          // behaviour rather than a chip stuck on 'uploading'.
          if (!result) return { ...att, state: 'done' as const };
          return {
            ...att,
            state: result.ok ? ('done' as const) : ('error' as const),
            ...(result.documentId ? { documentId: result.documentId } : {}),
          };
        })
      );

    // A rejecting host must not strand the chips as uploading, and must not
    // surface as an unhandled rejection — the prop contract accepts a promise,
    // so normalising every failure into `{ ok: false }` is not the host's job.
    void Promise.resolve(outcomes).then(settle, () =>
      setAttachments((prev) =>
        prev.map((att) =>
          indexById.has(att.id) ? { ...att, state: 'error' as const } : att
        )
      )
    );
  };

  const removeAttachment = (id: string): void => {
    setAttachments((prev) => {
      const target = prev.find((a) => a.id === id);
      if (target?.url) URL.revokeObjectURL(target.url);
      return prev.filter((a) => a.id !== id);
    });
  };

  // Document ids for the chips still on screen at send time. Chips the user
  // removed are already out of state, so removing one un-attaches its
  // document; chips that failed to upload never carry an id.
  const attachedDocumentIds = (): string[] =>
    attachments.flatMap((a) =>
      a.state === 'done' && a.documentId ? [a.documentId] : []
    );

  // Clear composer chips + revoke their blob URLs once a message is sent —
  // otherwise stale chips linger into the next turn and every image object URL
  // leaks until unmount.
  const clearAttachments = (): void => {
    setAttachments((prev) => {
      prev.forEach((a) => a.url && URL.revokeObjectURL(a.url));
      return [];
    });
  };

  // Keep sent chips until the turn has actually settled. A canceled preflight
  // restores the draft text, so its chips become editable again; a completed
  // turn discards them only after loading ends.
  useEffect(() => {
    if (submittedDraft === null) return;
    if (isLoading) {
      submittedDraftBecameBusyRef.current = true;
      return;
    }
    if (!submittedDraftBecameBusyRef.current) return;

    const settle = window.setTimeout(() => {
      if (value !== submittedDraft) clearAttachments();
      submittedDraftBecameBusyRef.current = false;
      setSubmittedDraft(null);
    }, 0);
    return () => window.clearTimeout(settle);
  }, [isLoading, submittedDraft, value]);

  useEffect(() => {
    return () => {
      attachmentsRef.current.forEach(
        (a) => a.url && URL.revokeObjectURL(a.url)
      );
    };
  }, []);

  const toggleVoice = (): void => {
    const Ctor = getSpeechRecognition();
    if (!Ctor) return;
    if (isListening) {
      recognitionRef.current?.stop();
      return;
    }
    const recognition = new Ctor();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = 'en-US';
    recognition.onresult = (event) => {
      const transcript = event.results[0]?.[0]?.transcript ?? '';
      if (transcript) {
        onChange(value ? `${value} ${transcript}` : transcript);
      }
    };
    recognition.onend = () => {
      setIsListening(false);
      recognitionRef.current = null;
    };
    recognition.onerror = () => {
      setIsListening(false);
      recognitionRef.current = null;
    };
    recognitionRef.current = recognition;
    recognition.start();
    setIsListening(true);
  };

  useEffect(() => {
    return () => {
      recognitionRef.current?.stop();
    };
  }, []);

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height =
        Math.min(textareaRef.current.scrollHeight, 200) + 'px';
    }
  }, [value, textareaRef]);

  // The page owns the controlled draft for slash commands and quote actions.
  // Mirror programmatic changes into assistant-ui; direct typing is already
  // synchronized by ComposerPrimitive.Input.
  useEffect(() => {
    if (aui.composer().getState().text !== value) {
      aui.composer().setText(value);
    }
  }, [aui, value]);

  const restoreDraftSelection = (): void => {
    const textarea = textareaRef.current;
    const selection = draftSelectionRef.current;
    if (!textarea || !selection) return;
    textarea.focus();
    textarea.setSelectionRange(selection.start, selection.end);
  };

  const executeCommand = (
    command: SlashCommand,
    discardDraft = false
  ): void => {
    const typedSlash = isSlashTrigger(value);
    if (typedSlash || discardDraft) onChange('');
    menu.dismiss();
    onCommand?.(command.id);
    if (!typedSlash && !discardDraft) {
      restoreDraftSelection();
    }
    draftSelectionRef.current = null;
  };

  const commandReplacesDraft = (command: SlashCommand): boolean =>
    command.id === 'new' ||
    command.id === 'retry' ||
    command.id === 'clear' ||
    command.id === 'summarize' ||
    command.id === 'keypoints' ||
    command.id === 'gaps' ||
    command.id === 'timeline';

  const runCommand = (command: SlashCommand | undefined): void => {
    if (!command) return;
    if (menu.isManual && value.trim() && commandReplacesDraft(command)) {
      // Commands opened from the button can otherwise replace/send a real
      // draft. Make that choice explicit instead of losing text on selection.
      menu.dismiss();
      setPendingCommand(command);
      return;
    }
    executeCommand(command);
  };

  const openCommands = (): void => {
    const textarea = textareaRef.current;
    if (textarea) {
      draftSelectionRef.current = {
        start: textarea.selectionStart,
        end: textarea.selectionEnd,
      };
    }
    menu.open();
    restoreDraftSelection();
  };

  const handleKeyDown = (e: React.KeyboardEvent): void => {
    // IME composition (ja/zh/ko): confirm-Enter must commit text, not submit.
    // Same guard as AuiMessage.tsx.
    if (e.nativeEvent.isComposing) return;
    // When the slash menu is open, intercept navigation/run keys. Enter runs
    // the highlighted command and never submits. The menu can only be open
    // while the whole value is a "/word" token, which is never a sendable
    // message — so normal send-on-Enter is unaffected.
    if (menu.isOpen) {
      if (menu.filtered.length > 0) {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          menu.move(1);
          return;
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          menu.move(-1);
          return;
        }
        if (e.key === 'Tab') {
          e.preventDefault();
          menu.move(e.shiftKey ? -1 : 1);
          return;
        }
        if (e.key === 'Enter') {
          e.preventDefault();
          runCommand(menu.filtered[menu.highlightedIndex]);
          return;
        }
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        const wasManual = menu.isManual;
        menu.dismiss();
        if (wasManual) restoreDraftSelection();
        draftSelectionRef.current = null;
        return;
      }
    }
  };

  const prepareSubmission = (e: React.SyntheticEvent): boolean => {
    if (isDisabled || !value.trim() || isOverLimit || hasUnsettledAttachments) {
      e.preventDefault();
      console.warn('[Chat] Composer submit swallowed', {
        isLoading,
        disabled,
        empty: !value.trim(),
        isOverLimit,
        hasUnsettledAttachments,
      });
      return false;
    }

    const attachmentIds = attachedDocumentIds();
    if (onSubmit(attachmentIds) === false) {
      e.preventDefault();
      aui.composer().setText('');
      clearAttachments();
      return false;
    }

    aui.composer().setRunConfig({ custom: { attachmentIds } });
    if (attachments.length > 0) setSubmittedDraft(value.trim());
    onChange('');
    return true;
  };

  const handleComposerSubmit = (e: React.FormEvent<HTMLFormElement>): void => {
    prepareSubmission(e);
  };

  const closeCommandConfirmation = (): void => {
    setPendingCommand(null);
  };

  const isDisabled = disabled;
  const charCount = value.length;
  const maxChars = 4000;
  const fillPct = Math.min(100, Math.round((charCount / maxChars) * 100));
  const isNearLimit = charCount > maxChars * 0.8;
  const isOverLimit = charCount > maxChars;

  const activeCommand =
    menu.isOpen && menu.filtered.length > 0
      ? menu.filtered[menu.highlightedIndex]
      : undefined;

  return (
    <div
      className="@container z-40 px-2 sm:px-4 pt-2 pb-[calc(0.5rem+env(safe-area-inset-bottom))] border-t"
      style={{
        background: 'var(--nous-bg-1)',
        borderColor: 'var(--nous-border-1)',
      }}
    >
      <div className="relative max-w-(--nous-chat-col) mx-auto">
        <SlashCommandMenu
          open={menu.isOpen}
          commands={menu.filtered}
          highlightedIndex={menu.highlightedIndex}
          onHighlight={menu.setHighlightedIndex}
          onRun={runCommand}
        />
        <ComposerPrimitive.Root
          onSubmit={handleComposerSubmit}
          className="rounded-[14px] overflow-hidden"
          style={{
            background: 'var(--nous-bg-2)',
            border: `1px solid ${
              isFocused ? 'var(--nous-sol-safe)' : 'var(--nous-border-1)'
            }`,
            boxShadow: isFocused
              ? '0 0 0 2px var(--nous-sol-safe)'
              : '0 1px 2px rgba(var(--nous-erebus-rgb), 0.04)',
            transition:
              'border-color 260ms var(--nous-ease-out), box-shadow 260ms var(--nous-ease-out)',
          }}
        >
          {/* Top strip — live status, sources switch, counter */}
          <div
            className="flex items-center justify-between gap-2 px-3 py-2 border-b"
            style={{
              background: 'var(--nous-bg-1)',
              borderColor: 'var(--nous-border-1)',
            }}
          >
            <div className="flex items-center gap-2 min-w-0">
              <button
                type="button"
                role="switch"
                onClick={() => onRAGToggle(!enableRAG)}
                disabled={isLoading}
                aria-checked={enableRAG}
                aria-label="Use my sources"
                title={
                  enableRAG
                    ? 'Grounds answers in your sources.'
                    : 'Answers without your sources.'
                }
                className="inline-flex items-center gap-2 shrink-0 rounded-md px-1 py-0.5 transition-colors disabled:opacity-50 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
              >
                <span
                  aria-hidden
                  className="relative block w-7 h-4 rounded-full transition-colors"
                  style={{
                    background: enableRAG
                      ? 'var(--nous-sol)'
                      : 'var(--nous-border-2)',
                  }}
                >
                  <span
                    className="absolute top-0.5 left-0.5 block w-3 h-3 rounded-full"
                    style={{
                      background: 'var(--nous-bg-1)',
                      transform: enableRAG
                        ? 'translateX(12px)'
                        : 'translateX(0)',
                      transition: 'transform 160ms var(--nous-ease-out)',
                    }}
                  />
                </span>
                <span
                  aria-hidden
                  className="text-[12px] whitespace-nowrap"
                  style={{
                    fontFamily: 'var(--nous-font-ui)',
                    color: enableRAG ? 'var(--nous-fg-1)' : 'var(--nous-fg-3)',
                  }}
                >
                  Use my sources
                </span>
              </button>
            </div>

            <div
              className="inline-flex items-center gap-2 font-nous-mono text-[12px] tabular-nums whitespace-nowrap shrink-0"
              style={{
                color: isOverLimit
                  ? 'var(--nous-mars)'
                  : isNearLimit
                    ? 'var(--nous-corona)'
                    : 'var(--nous-fg-3)',
                letterSpacing: '0.04em',
              }}
            >
              <span
                className={isNearLimit ? 'inline' : 'hidden sm:inline'}
                aria-live="polite"
                aria-atomic="true"
              >
                {charCount}/{maxChars}
              </span>
              <span
                className="block rounded-sm overflow-hidden"
                style={{
                  width: '36px',
                  height: '3px',
                  background: 'var(--nous-border-1)',
                }}
              >
                <span
                  className="block h-full w-full origin-left rounded-sm"
                  style={{
                    transform: `scaleX(${fillPct / 100})`,
                    background: isNearLimit
                      ? 'var(--nous-corona)'
                      : 'var(--nous-sol)',
                    transition: 'transform 200ms ease-out',
                  }}
                />
              </span>
            </div>
          </div>

          {/* Body — serif input */}
          <div
            className="px-3 pt-2.5 pb-2"
            style={{ background: 'var(--nous-bg-2)' }}
          >
            {attachments.length > 0 && submittedDraft === null && (
              <ul
                className="flex flex-wrap items-center gap-2 mb-3 list-none p-0 m-0"
                aria-label="Attached files"
              >
                {attachments.map((att) => (
                  <li
                    key={att.id}
                    className={cn(
                      'group inline-flex items-center gap-2 h-9 rounded-md pl-1.5 pr-1 font-nous-mono text-[11px]',
                      att.state === 'uploading' && 'opacity-70'
                    )}
                    style={{
                      background: 'var(--nous-bg-1)',
                      border: `1px solid ${
                        att.state === 'error'
                          ? 'hsl(var(--destructive))'
                          : 'var(--nous-border-1)'
                      }`,
                      color: 'var(--nous-fg-2)',
                    }}
                  >
                    {att.isImage && att.url ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={att.url}
                        alt={att.name}
                        className="w-6 h-6 rounded object-cover shrink-0"
                      />
                    ) : (
                      <span
                        className="grid place-items-center w-6 h-6 rounded shrink-0"
                        style={{
                          background: 'var(--nous-bg-2)',
                          color: 'var(--nous-fg-3)',
                        }}
                      >
                        <Paperclip className="w-3 h-3" strokeWidth={1.7} />
                      </span>
                    )}
                    <span
                      className="max-w-[140px] truncate"
                      title={
                        att.state === 'error'
                          ? `${att.name}, upload failed`
                          : att.name
                      }
                    >
                      {att.name}
                    </span>
                    {att.state !== 'done' && (
                      <span
                        className="shrink-0"
                        style={{
                          color:
                            att.state === 'error'
                              ? 'hsl(var(--destructive))'
                              : 'var(--nous-fg-3)',
                        }}
                      >
                        {att.state === 'uploading' ? (
                          <Loader2
                            className="w-3 h-3 animate-spin"
                            strokeWidth={2}
                            aria-label={`Uploading ${att.name}`}
                          />
                        ) : (
                          <AlertCircle
                            className="w-3 h-3"
                            strokeWidth={2}
                            aria-label={`Upload failed for ${att.name}`}
                          />
                        )}
                      </span>
                    )}
                    <button
                      type="button"
                      onClick={() => removeAttachment(att.id)}
                      aria-label={`Remove ${att.name}`}
                      title="Remove"
                      className="grid place-items-center w-6 h-6 rounded transition-colors hover:bg-(--nous-aurum)"
                      style={{ color: 'var(--nous-fg-3)' }}
                    >
                      <X className="w-3 h-3" strokeWidth={2} />
                    </button>
                  </li>
                ))}
              </ul>
            )}

            {hasUnsettledAttachments && submittedDraft === null && (
              <p
                role="status"
                aria-live="polite"
                className="mb-3 font-nous-mono text-[10px]"
                style={{ color: 'var(--nous-fg-3)' }}
              >
                {attachmentStatus.uploading
                  ? 'Wait for attachments to finish uploading before sending.'
                  : 'Remove failed attachments before sending.'}
              </p>
            )}

            <ComposerPrimitive.Queue>
              {({ queueItem }) => (
                <div
                  className="mb-2 flex min-h-11 items-center gap-2 rounded-md px-2 font-nous-mono text-[11px]"
                  style={{
                    background: 'var(--nous-bg-1)',
                    border: '1px solid var(--nous-border-1)',
                    color: 'var(--nous-fg-2)',
                  }}
                >
                  <span
                    className="shrink-0 text-[10px] font-semibold"
                    style={{ color: 'var(--nous-fg-3)' }}
                  >
                    Queued
                  </span>
                  <QueueItemPrimitive.Text className="min-w-0 flex-1 truncate" />
                  <QueueItemPrimitive.Steer
                    aria-label={`Run ${queueItem.prompt} next`}
                    title="Run next"
                    className="min-h-11 shrink-0 rounded-md px-2 transition-colors hover:bg-(--nous-aurum)"
                  >
                    Run next
                  </QueueItemPrimitive.Steer>
                  <QueueItemPrimitive.Remove
                    aria-label={`Remove ${queueItem.prompt} from queue`}
                    title="Remove from queue"
                    className="grid size-11 shrink-0 place-items-center rounded-md transition-colors hover:bg-(--nous-aurum)"
                  >
                    <X className="size-3" strokeWidth={2} />
                  </QueueItemPrimitive.Remove>
                </div>
              )}
            </ComposerPrimitive.Queue>

            <ComposerPrimitive.Input asChild>
              <textarea
                ref={textareaRef}
                value={value}
                onChange={(e) => onChange(e.target.value)}
                onKeyDown={handleKeyDown}
                onFocus={() => setIsFocused(true)}
                onBlur={() => setIsFocused(false)}
                placeholder="Ask anything, or paste a passage to discuss…"
                aria-label="Message"
                rows={1}
                disabled={isDisabled}
                aria-haspopup={menu.isOpen ? 'listbox' : undefined}
                aria-controls={menu.isOpen ? SLASH_LISTBOX_ID : undefined}
                aria-activedescendant={
                  activeCommand ? slashOptionId(activeCommand.id) : undefined
                }
                aria-autocomplete="list"
                aria-invalid={isOverLimit || undefined}
                aria-describedby={isOverLimit ? 'nous-input-limit' : undefined}
                className="w-full bg-transparent resize-none outline-hidden font-nous-body text-[16px] placeholder:text-(--nous-fg-3)"
                style={{
                  color: 'var(--nous-fg-1)',
                  lineHeight: '1.6',
                  minHeight: '48px',
                  maxHeight: '200px',
                }}
              />
            </ComposerPrimitive.Input>

            {isOverLimit && (
              <p
                id="nous-input-limit"
                role="alert"
                className="mt-2 font-nous-mono text-[10px]"
                style={{ color: 'var(--nous-mars)', letterSpacing: '0.02em' }}
              >
                Message is {charCount - maxChars} characters over the {maxChars}{' '}
                limit. Trim it to send.
              </p>
            )}

            <div
              className="flex flex-wrap items-center justify-between gap-2 mt-1.5 pt-1.5"
              style={{ borderColor: 'var(--nous-border-1)' }}
            >
              <div className="flex items-center gap-0.5">
                <label
                  className="grid place-items-center w-11 h-11 rounded-md cursor-pointer transition-colors"
                  style={{ color: 'var(--nous-fg-3)' }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = 'var(--nous-aurum)';
                    e.currentTarget.style.color = 'var(--nous-sol-safe)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = 'transparent';
                    e.currentTarget.style.color = 'var(--nous-fg-3)';
                  }}
                  title="Attach file"
                >
                  <Paperclip className="w-3.5 h-3.5" strokeWidth={1.7} />
                  <input
                    type="file"
                    multiple
                    aria-label="Attach file"
                    className="sr-only"
                    onChange={(e) => {
                      const files = e.target.files;
                      if (files && files.length > 0) {
                        addFiles(files);
                      }
                      e.target.value = '';
                    }}
                  />
                </label>
                <label
                  className="grid place-items-center w-11 h-11 rounded-md cursor-pointer transition-colors"
                  style={{ color: 'var(--nous-fg-3)' }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = 'var(--nous-aurum)';
                    e.currentTarget.style.color = 'var(--nous-sol-safe)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = 'transparent';
                    e.currentTarget.style.color = 'var(--nous-fg-3)';
                  }}
                  title="Attach image"
                >
                  <ImageIcon className="w-3.5 h-3.5" strokeWidth={1.7} />
                  <input
                    type="file"
                    accept="image/*"
                    multiple
                    aria-label="Attach image"
                    className="sr-only"
                    onChange={(e) => {
                      const files = e.target.files;
                      if (files && files.length > 0) {
                        addFiles(files);
                      }
                      e.target.value = '';
                    }}
                  />
                </label>
                <button
                  type="button"
                  onClick={toggleVoice}
                  disabled={!voiceSupported}
                  aria-pressed={isListening}
                  aria-label={
                    !voiceSupported
                      ? 'Voice input not supported'
                      : isListening
                        ? 'Stop voice input'
                        : 'Voice input'
                  }
                  title={
                    !voiceSupported
                      ? 'Voice input not supported'
                      : isListening
                        ? 'Stop voice input'
                        : 'Voice input'
                  }
                  className={cn(
                    'grid place-items-center w-11 h-11 rounded-md transition-colors',
                    !voiceSupported && 'opacity-40 cursor-not-allowed'
                  )}
                  style={{
                    color: isListening
                      ? 'var(--nous-sol-safe)'
                      : 'var(--nous-fg-3)',
                    background: isListening
                      ? 'var(--nous-aurum)'
                      : 'transparent',
                  }}
                >
                  <Mic
                    className={cn(
                      'w-3.5 h-3.5',
                      isListening && 'animate-pulse'
                    )}
                    strokeWidth={1.7}
                  />
                </button>

                <button
                  type="button"
                  onClick={openCommands}
                  className="hidden @min-[480px]:inline-flex items-center gap-1.5 rounded-md ml-1.5 min-h-[44px] text-[12px] cursor-pointer transition-colors hover:bg-(--nous-aurum)"
                  style={{
                    padding: '4px 8px',
                    border: '1px solid var(--nous-border-1)',
                    background: 'var(--nous-bg-1)',
                    color: 'var(--nous-fg-2)',
                    fontFamily: 'var(--nous-font-ui)',
                  }}
                  aria-label="Open commands"
                  title="Type / to open commands"
                >
                  <kbd
                    className="font-nous-mono font-bold rounded-sm"
                    style={{
                      fontSize: '10px',
                      padding: '1px 4px',
                      background: 'var(--nous-bg-1)',
                      border: '1px solid var(--nous-border-1)',
                      color: 'var(--nous-fg-2)',
                    }}
                  >
                    /
                  </kbd>
                  Commands
                </button>
              </div>

              <div className="flex items-center gap-2">
                {isLoading && (
                  <ComposerPrimitive.Cancel
                    className="inline-flex min-h-11 items-center gap-2 font-medium rounded-lg transition-colors active:scale-[0.97]"
                    style={{
                      padding: '8px 16px',
                      fontSize: '12px',
                      letterSpacing: '0.01em',
                      background: 'rgba(var(--nous-mars-rgb), 0.1)',
                      border: '1px solid rgba(var(--nous-mars-rgb), 0.4)',
                      color: 'var(--nous-mars)',
                    }}
                    aria-label="Stop agent"
                  >
                    <Square className="w-3 h-3" strokeWidth={2.2} />
                    Stop
                  </ComposerPrimitive.Cancel>
                )}
                <ComposerPrimitive.Send
                  disabled={
                    !value.trim() ||
                    isDisabled ||
                    isOverLimit ||
                    hasUnsettledAttachments
                  }
                  onClick={prepareSubmission}
                  title={
                    isOverLimit
                      ? `Message is over the ${maxChars}-character limit`
                      : hasUnsettledAttachments
                        ? 'Finish or remove attachments before sending'
                        : isLoading
                          ? 'Queue follow-up (Enter)'
                          : 'Send (Enter)'
                  }
                  className="group inline-flex min-h-11 items-center gap-2 font-semibold rounded-lg transition-colors active:scale-[0.98] disabled:opacity-40 disabled:cursor-not-allowed disabled:active:scale-100"
                  style={{
                    padding: '8px 16px',
                    fontSize: '12px',
                    letterSpacing: '0.01em',
                    background: 'var(--nous-sol)',
                    color: 'var(--nous-erebus)',
                    boxShadow: '0 1px 2px rgba(var(--nous-sol-rgb), 0.2)',
                  }}
                  onMouseEnter={(e) => {
                    if (!e.currentTarget.disabled) {
                      e.currentTarget.style.background = 'var(--nous-helios)';
                      e.currentTarget.style.boxShadow =
                        '0 4px 12px rgba(var(--nous-sol-rgb), 0.3)';
                    }
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = 'var(--nous-sol)';
                    e.currentTarget.style.boxShadow =
                      '0 1px 2px rgba(var(--nous-sol-rgb), 0.2)';
                  }}
                >
                  {isLoading ? 'Queue' : 'Send'}
                  <ArrowRight
                    className="w-3.5 h-3.5 transition-transform group-hover:translate-x-0.5"
                    strokeWidth={2}
                  />
                </ComposerPrimitive.Send>
              </div>
            </div>
          </div>
        </ComposerPrimitive.Root>
      </div>

      <AlertDialog
        open={pendingCommand !== null}
        onOpenChange={(open) => {
          if (!open) closeCommandConfirmation();
        }}
      >
        <AlertDialogContent
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            restoreDraftSelection();
            draftSelectionRef.current = null;
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>Run {pendingCommand?.label}?</AlertDialogTitle>
            <AlertDialogDescription>
              Running this command will discard your unsent draft.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={closeCommandConfirmation}>
              Keep draft
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingCommand) executeCommand(pendingCommand, true);
                setPendingCommand(null);
              }}
            >
              Run and discard draft
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

export default ChatInput;
