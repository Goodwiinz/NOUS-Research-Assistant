'use client';

import { memo, useCallback, useEffect, useMemo, useState } from 'react';

import { cn } from '@/lib/utils';
import { useThreadSearch } from '@/hooks/chat/useThreadSearch';
import { isToday, isYesterday, formatDistanceToNowStrict } from 'date-fns';
import {
  CheckSquare,
  MessageSquare,
  Network,
  Pencil,
  Pin,
  Plus,
  Search,
  Trash2,
  X,
} from 'lucide-react';
import type { Workspace } from '@/types/workspace';

interface SidebarConversation {
  id: string;
  title: string;
  messages: { role: string; content: string }[];
  updatedAt: number;
  previewText?: string;
  messageCount?: number;
  pinned?: boolean;
  unread?: boolean;
  tags?: string[];
  /** Distinct cited sources in the thread. The thread-list endpoint does not
   *  return one today, so this is normally undefined and the row falls back to
   *  the preview snippet. Nothing here fabricates a count. */
  citationCount?: number;
  matchingMessageCount?: number;
}

type FilterKey = 'all' | 'pinned' | 'drafts' | 'shared';

interface ChatSidebarProps {
  conversations: SidebarConversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename?: (id: string) => void;
  onDelete?: (id: string) => void;
  onBulkDelete?: (ids: string[]) => void;
  currentWorkspace?: Workspace | null;
  className?: string;
  // CX8: sidebar thread list is paginated server-side. Both are optional so
  // existing/mobile callers that don't pass them just never show the button.
  hasMoreThreads?: boolean;
  onLoadMoreThreads?: () => void | Promise<void>;
}

function formatCompactTime(date: Date): string {
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffH = Math.floor(diffMs / (1000 * 60 * 60));
  const diffD = Math.floor(diffMs / (1000 * 60 * 60 * 24));
  if (diffH < 1)
    return formatDistanceToNowStrict(date, { addSuffix: false })
      .replace(' minutes', 'm')
      .replace(' minute', 'm')
      .replace(' seconds', 's')
      .replace(' second', 's');
  if (diffH < 24) return `${diffH}h`;
  return `${diffD}d`;
}

interface DateSection {
  label: string;
  items: SidebarConversation[];
}

const SIDEBAR_PREVIEW_MAX_CHARS = 60;

function truncatePreview(content: string): string {
  return content.length > SIDEBAR_PREVIEW_MAX_CHARS
    ? `${content.substring(0, SIDEBAR_PREVIEW_MAX_CHARS)}…`
    : content;
}

function groupByDate(conversations: SidebarConversation[]): DateSection[] {
  const pinned: SidebarConversation[] = [];
  const today: SidebarConversation[] = [];
  const yesterday: SidebarConversation[] = [];
  const earlier: SidebarConversation[] = [];

  for (const conv of conversations) {
    if (conv.pinned) {
      pinned.push(conv);
      continue;
    }
    const d = new Date(conv.updatedAt);
    if (isToday(d)) today.push(conv);
    else if (isYesterday(d)) yesterday.push(conv);
    else earlier.push(conv);
  }

  const sections: DateSection[] = [];
  if (pinned.length > 0) sections.push({ label: 'Pinned', items: pinned });
  if (today.length > 0) sections.push({ label: 'Today', items: today });
  if (yesterday.length > 0)
    sections.push({ label: 'Yesterday', items: yesterday });
  if (earlier.length > 0) sections.push({ label: 'Earlier', items: earlier });
  return sections;
}

export const ChatSidebar = memo(function ChatSidebar({
  conversations,
  activeId,
  onSelect,
  onNew,
  onRename,
  onDelete,
  onBulkDelete,
  currentWorkspace,
  className,
  hasMoreThreads,
  onLoadMoreThreads,
}: ChatSidebarProps) {
  const {
    query: searchQuery,
    setQuery: setSearchQuery,
    results: searchResults,
    totalResults: searchTotalResults,
    hasMore: hasMoreSearchResults,
    isLoading: isSearching,
    isFetchingNextPage: isFetchingMoreSearchResults,
    isError: searchError,
    retry: retrySearch,
    loadMore: loadMoreSearchResults,
  } = useThreadSearch(currentWorkspace?.id);
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [activeFilter, setActiveFilter] = useState<FilterKey>('all');
  const [isLoadingMore, setIsLoadingMore] = useState(false);

  const handleLoadMore = useCallback(() => {
    if (!onLoadMoreThreads || isLoadingMore) return;
    setIsLoadingMore(true);
    Promise.resolve(onLoadMoreThreads()).finally(() => setIsLoadingMore(false));
  }, [onLoadMoreThreads, isLoadingMore]);

  const hasSearchQuery = searchQuery.trim().length > 0;
  const hasServerSearchQuery =
    searchQuery.trim().length >= 2 && Boolean(currentWorkspace?.id);

  const searchConversations = useMemo(() => {
    const pinnedById = new Map(
      conversations.map((conversation) => [
        conversation.id,
        conversation.pinned,
      ])
    );

    return searchResults.map((result): SidebarConversation => {
      const summary = result.summary?.trim() || undefined;
      const updatedAt = Date.parse(result.last_message_at || result.created_at);
      return {
        id: result.thread_id,
        title: result.title?.trim() || 'New Chat',
        messages: [],
        updatedAt: Number.isNaN(updatedAt) ? 0 : updatedAt,
        previewText: summary,
        messageCount: result.message_count,
        matchingMessageCount: result.matching_message_count ?? undefined,
        pinned: pinnedById.get(result.thread_id),
      };
    });
  }, [conversations, searchResults]);

  const filteredConversations = useMemo(() => {
    let list = conversations;
    if (hasServerSearchQuery) {
      list = searchConversations;
    } else if (hasSearchQuery && currentWorkspace?.id) {
      list = [];
    } else {
      const q = searchQuery.trim().toLowerCase();
      if (q) list = list.filter((c) => c.title.toLowerCase().includes(q));
    }
    if (activeFilter === 'pinned') list = list.filter((c) => c.pinned);
    return list;
  }, [
    activeFilter,
    conversations,
    currentWorkspace?.id,
    hasServerSearchQuery,
    hasSearchQuery,
    searchConversations,
    searchQuery,
  ]);

  const sections = useMemo(
    () => groupByDate(filteredConversations),
    [filteredConversations]
  );
  const pinnedCount = useMemo(
    () => conversations.filter((c) => c.pinned).length,
    [conversations]
  );

  const exitSelectMode = useCallback((): void => {
    setSelectMode(false);
    setSelectedIds([]);
  }, []);

  const toggleSelected = useCallback((id: string): void => {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }, []);

  const handleBulkDelete = useCallback((): void => {
    if (selectedIds.length === 0) return;
    onBulkDelete?.(selectedIds);
    exitSelectMode();
  }, [selectedIds, onBulkDelete, exitSelectMode]);

  const handleSearchChange = useCallback(
    (value: string): void => {
      if (value.trim()) exitSelectMode();
      setSearchQuery(value);
    },
    [exitSelectMode, setSearchQuery]
  );

  // ⌘N shortcut
  useEffect(() => {
    const handler = (e: KeyboardEvent): void => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'n') {
        e.preventDefault();
        exitSelectMode();
        onNew();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onNew, exitSelectMode]);

  return (
    <aside
      aria-label="Conversations"
      className={cn(
        'flex flex-col w-[260px] border-r border-(--nous-border-1) bg-(--nous-bg-1) dark:bg-(--nous-nyx) dark:border-(--nous-shade) h-full',
        className
      )}
    >
      {/* Top chrome — pinned, doesn't scroll */}
      <div className="shrink-0 p-3.5 pb-3 flex flex-col gap-2.5 border-b border-(--nous-border-1) dark:border-(--nous-shade)">
        {/* Workspace switcher */}
        <div className="w-full flex items-center gap-[9px] px-2.5 py-2 rounded-lg border border-(--nous-border-1) dark:border-(--nous-shade) bg-transparent min-w-0">
          <div className="w-[22px] h-[22px] rounded-[5px] flex items-center justify-center bg-(--nous-aurum) dark:bg-(--nous-ember) shrink-0">
            <Network className="w-3 h-3 text-(--nous-sol-safe) dark:text-(--nous-helios)" />
          </div>
          <div className="flex-1 min-w-0 text-left flex flex-col gap-px">
            <span
              className="text-[13px] font-semibold text-(--nous-fg-1) truncate leading-tight"
              style={{
                fontFamily: 'var(--nous-font-ui)',
                letterSpacing: '-0.005em',
              }}
            >
              {currentWorkspace?.name || 'My Workspace'}
            </span>
            <span
              className="text-[11px] text-(--nous-fg-3)"
              style={{ fontFamily: 'var(--nous-font-ui)' }}
            >
              {conversations.length} thread
              {conversations.length !== 1 ? 's' : ''}
            </span>
          </div>
        </div>

        {/* New chat */}
        <button
          onClick={() => {
            exitSelectMode();
            onNew();
          }}
          className="w-full flex items-center justify-center gap-[7px] py-[9px] px-3 rounded-lg bg-(--nous-sol) text-(--nous-erebus) shadow-xs hover:shadow-md hover:brightness-105 transition-colors"
          style={{ fontFamily: 'var(--nous-font-ui)' }}
        >
          <Plus className="w-[13px] h-[13px]" />
          <span
            className="text-[13px] font-semibold"
            style={{ letterSpacing: '-0.005em' }}
          >
            New chat
          </span>
          <kbd
            className="ml-auto px-[5px] py-px rounded-[3px] bg-(--nous-erebus)/10 text-(--nous-erebus) text-[9px] font-semibold"
            style={{ fontFamily: 'var(--nous-font-mono)' }}
          >
            ⌘N
          </kbd>
        </button>

        {/* Search */}
        <div className="relative">
          <Search className="absolute left-[10px] top-1/2 -translate-y-1/2 w-[13px] h-[13px] text-(--nous-fg-3) pointer-events-none" />
          <input
            type="text"
            aria-label="Search threads"
            placeholder="Search threads..."
            value={searchQuery}
            onChange={(e) => handleSearchChange(e.target.value)}
            className="w-full h-8 bg-(--nous-bg-2) dark:bg-(--nous-obsidian) border border-(--nous-border-1) dark:border-(--nous-shade) rounded-[7px] py-0 pl-[30px] pr-2.5 text-xs text-(--nous-fg-1) placeholder-(--nous-fg-3) focus:outline-hidden focus:border-(--nous-sol) dark:focus:border-(--nous-helios) focus:bg-(--nous-bg-1) transition-colors"
            style={{ fontFamily: 'var(--nous-font-ui)' }}
          />
        </div>
      </div>

      {/* Filter chips */}
      <div className="shrink-0 flex gap-1 px-3.5 py-2.5 overflow-x-auto border-b border-(--nous-border-1) dark:border-(--nous-shade) scrollbar-hide">
        {(
          [
            {
              key: 'all' as FilterKey,
              label: 'All',
              count: hasServerSearchQuery
                ? searchTotalResults
                : conversations.length,
            },
            { key: 'pinned' as FilterKey, label: 'Pinned', count: pinnedCount },
          ] as const
        ).map((f) => (
          <button
            key={f.key}
            onClick={() => setActiveFilter(f.key)}
            className={cn(
              'inline-flex items-center px-[9px] py-2 rounded-full border text-[12px] whitespace-nowrap transition-colors',
              activeFilter === f.key
                ? 'bg-(--nous-sol) text-(--nous-erebus) border-(--nous-sol) dark:bg-(--nous-helios) dark:text-(--nous-nyx) dark:border-(--nous-helios)'
                : 'bg-transparent border-(--nous-border-1) dark:border-(--nous-shade) text-(--nous-fg-2) hover:border-(--nous-sol) hover:text-(--nous-sol-safe)'
            )}
            style={{ fontFamily: 'var(--nous-font-ui)' }}
          >
            {f.label}
            <span
              className={cn(
                'ml-1',
                activeFilter === f.key ? 'opacity-70' : 'opacity-55'
              )}
            >
              {f.count}
            </span>
          </button>
        ))}

        {/* Select mode toggle */}
        {!hasSearchQuery ? (
          selectMode ? (
            <div className="ml-auto flex items-center gap-1 shrink-0">
              <button
                onClick={handleBulkDelete}
                disabled={selectedIds.length === 0}
                className="inline-flex items-center gap-1 px-2 py-[3px] rounded-full border border-(--nous-mars)/40 text-(--nous-mars) text-[10px] hover:bg-(--nous-mars)/10 transition-colors disabled:opacity-40"
                style={{ fontFamily: 'var(--nous-font-mono)' }}
              >
                <Trash2 className="w-2.5 h-2.5" />
                {selectedIds.length}
              </button>
              <button
                onClick={exitSelectMode}
                aria-label="Exit select mode"
                className="min-w-11 min-h-11 md:w-5 md:h-5 md:min-w-0 md:min-h-0 flex items-center justify-center rounded-full border border-(--nous-border-1) hover:bg-(--nous-sol)/5 transition-colors"
              >
                <X className="w-2.5 h-2.5 text-(--nous-fg-3)" />
              </button>
            </div>
          ) : (
            <button
              onClick={() => setSelectMode(true)}
              aria-label="Select conversations"
              className="ml-auto inline-flex items-center gap-1 px-[9px] py-[3px] rounded-full border border-(--nous-border-1) dark:border-(--nous-shade) text-[10px] text-(--nous-fg-3) hover:border-(--nous-fg-3) transition-colors shrink-0"
              style={{ fontFamily: 'var(--nous-font-mono)' }}
            >
              <CheckSquare className="w-2.5 h-2.5" aria-hidden="true" />
            </button>
          )
        ) : null}
      </div>

      {/* Scroll region — only this scrolls */}
      <div className="flex-1 overflow-y-auto pt-1">
        {sections.map((section) => (
          <div key={section.label} className="px-2.5 pb-1.5">
            {/* Section label */}
            <div className="flex items-center gap-1.5 px-2 pt-2.5 pb-1.5">
              {section.label === 'Pinned' && (
                <Pin className="w-[9px] h-[9px] text-(--nous-sol) dark:text-(--nous-helios)" />
              )}
              <span
                className="text-[11px] font-medium text-(--nous-fg-3)"
                style={{ fontFamily: 'var(--nous-font-ui)' }}
              >
                {section.label}
              </span>
              <span className="ml-auto px-[5px] py-px bg-(--nous-bg-2) dark:bg-(--nous-obsidian) border border-(--nous-border-1) dark:border-(--nous-shade) rounded-[3px] text-[9px] text-(--nous-fg-2)">
                {section.items.length}
              </span>
            </div>

            {/* Conversation items */}
            {section.items.map((conv) => {
              const isActive = conv.id === activeId;
              const isSelected = selectedIds.includes(conv.id);
              const timeStr = formatCompactTime(new Date(conv.updatedAt));
              const messageCount = conv.messageCount ?? conv.messages.length;
              const lastMessage = conv.messages[conv.messages.length - 1];
              const previewSource = conv.previewText || lastMessage?.content;
              const citationCount = conv.citationCount ?? 0;
              const snippet =
                citationCount > 0
                  ? `${citationCount} source${citationCount === 1 ? '' : 's'} · ${messageCount} turn${messageCount === 1 ? '' : 's'}`
                  : previewSource
                    ? truncatePreview(previewSource)
                    : conv.matchingMessageCount
                      ? `${conv.matchingMessageCount} matching message${conv.matchingMessageCount === 1 ? '' : 's'}`
                      : messageCount > 0
                        ? `${messageCount} message${messageCount === 1 ? '' : 's'}`
                        : 'No messages yet';

              return (
                <div
                  key={conv.id}
                  className={cn(
                    'relative group/row',
                    selectMode && 'flex items-center gap-1.5'
                  )}
                >
                  {/* Outside the row <button>: interactive content nested in a
                      button is invalid HTML and the checkbox was neither
                      focusable nor operable on its own. */}
                  {selectMode && (
                    <input
                      type="checkbox"
                      aria-label={`Select ${conv.title}`}
                      checked={isSelected}
                      onChange={() => toggleSelected(conv.id)}
                      onClick={(e) => e.stopPropagation()}
                      className="w-3 h-3 accent-(--nous-sol) shrink-0"
                    />
                  )}
                  <button
                    onClick={() =>
                      selectMode ? toggleSelected(conv.id) : onSelect(conv.id)
                    }
                    className={cn(
                      'sb-conv w-full text-left block p-[9px_11px] rounded-[7px] border transition-[background,border-color] duration-180 mb-px relative',
                      isActive
                        ? 'bg-(--nous-aurum) dark:bg-(--nous-ember) border-[rgba(var(--nous-sol-rgb),0.2)] dark:border-[rgba(var(--nous-helios-rgb),0.25)]'
                        : 'bg-transparent border-transparent hover:bg-(--nous-bg-2) dark:hover:bg-(--nous-obsidian)'
                    )}
                  >
                    {/* Row 1: pin + title + unread */}
                    <div className="flex items-center gap-1.5 mb-[3px]">
                      {conv.pinned && !selectMode && (
                        <Pin className="w-[10px] h-[10px] text-(--nous-sol) dark:text-(--nous-helios) shrink-0" />
                      )}
                      <span
                        className="text-[13px] font-medium text-(--nous-fg-1) truncate flex-1 min-w-0"
                        style={{ fontFamily: 'var(--nous-font-ui)' }}
                      >
                        {conv.title}
                      </span>
                      {conv.unread && (
                        <>
                          <span className="sr-only">Unread</span>
                          <span
                            aria-hidden
                            className="w-1.5 h-1.5 rounded-full bg-(--nous-sol) dark:bg-(--nous-helios) shadow-[0_0_0_2px_rgba(var(--nous-sol-rgb),0.15)] shrink-0"
                          />
                        </>
                      )}
                    </div>

                    {/* Snippet */}
                    <div
                      className="text-[12px] leading-normal text-(--nous-fg-2) truncate"
                      style={{ fontFamily: 'var(--nous-font-body)' }}
                    >
                      {snippet}
                    </div>

                    {/* Meta row: tags + time */}
                    <div className="flex items-center justify-between gap-2 mt-1.5">
                      <div className="flex gap-1 min-w-0 overflow-hidden">
                        {(conv.tags || []).slice(0, 2).map((tag) => (
                          <span
                            key={tag}
                            className="inline-flex items-center px-1.5 py-px bg-(--nous-bg-2) dark:bg-(--nous-nyx) border border-(--nous-border-1) dark:border-(--nous-shade) rounded-[3px] text-[9px] text-(--nous-fg-2) dark:text-(--nous-parchment) whitespace-nowrap"
                          >
                            {tag}
                          </span>
                        ))}
                        {messageCount > 0 && (
                          <span className="inline-flex items-center px-1.5 py-px bg-(--nous-bg-2) dark:bg-(--nous-nyx) border border-(--nous-border-1) dark:border-(--nous-shade) rounded-[3px] text-[9px] text-(--nous-fg-2) dark:text-(--nous-parchment) whitespace-nowrap">
                            {messageCount}
                          </span>
                        )}
                      </div>
                      <span className="text-[12px] text-(--nous-fg-2) shrink-0">
                        {timeStr}
                      </span>
                    </div>
                  </button>

                  {/* Hover actions */}
                  {!selectMode && (onRename || onDelete) && (
                    <div className="absolute right-1.5 top-2 hidden group-hover/row:flex group-focus-within/row:flex items-center gap-0.5 bg-(--nous-bg-2) dark:bg-(--nous-obsidian) border border-(--nous-border-1) dark:border-(--nous-shade) rounded-md px-0.5 py-0.5 shadow-xs">
                      {onRename && (
                        <button
                          type="button"
                          aria-label={`Rename ${conv.title}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            onRename(conv.id);
                          }}
                          className="p-2.5 md:p-1 rounded hover:bg-(--nous-sol)/8 text-(--nous-fg-3) hover:text-(--nous-fg-1) transition-colors"
                        >
                          <Pencil className="w-2.5 h-2.5" />
                        </button>
                      )}
                      {onDelete && (
                        <button
                          type="button"
                          aria-label={`Delete ${conv.title}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            onDelete(conv.id);
                          }}
                          className="p-2.5 md:p-1 rounded hover:bg-(--nous-mars)/10 text-(--nous-fg-3) hover:text-(--nous-mars) transition-colors"
                        >
                          <Trash2 className="w-2.5 h-2.5" />
                        </button>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ))}

        {hasServerSearchQuery && isSearching && sections.length === 0 && (
          <div
            className="px-4 py-8 text-center"
            role="status"
            aria-live="polite"
          >
            <p
              className="text-[11px] text-(--nous-fg-3)"
              style={{ fontFamily: 'var(--nous-font-body)' }}
            >
              Searching threads…
            </p>
          </div>
        )}

        {hasServerSearchQuery && searchError && (
          <div
            className="mx-4 my-2 rounded-md border border-(--nous-mars)/30 px-3 py-2 text-center"
            role="alert"
          >
            <p
              className="text-[11px] text-(--nous-fg-3)"
              style={{ fontFamily: 'var(--nous-font-body)' }}
            >
              Search failed. Try again.
            </p>
            <button
              type="button"
              onClick={() => void retrySearch()}
              className="mt-2 px-2.5 py-1 rounded-md border border-(--nous-border-1) text-[11px] text-(--nous-fg-2) hover:border-(--nous-sol)/40 hover:text-(--nous-fg-1)"
            >
              Retry search
            </button>
          </div>
        )}

        {sections.length === 0 && !isSearching && !searchError && (
          <div className="px-4 py-8 text-center">
            <MessageSquare className="w-6 h-6 text-(--nous-fg-3)/40 mx-auto mb-2" />
            <p
              className="text-[11px] text-(--nous-fg-3)"
              style={{ fontFamily: 'var(--nous-font-body)' }}
            >
              {hasSearchQuery
                ? searchQuery.trim().length < 2
                  ? 'Type at least 2 characters to search'
                  : 'No matching threads'
                : 'No conversations yet'}
            </p>
          </div>
        )}

        {hasServerSearchQuery && hasMoreSearchResults && (
          <div className="px-2.5 pb-2.5 pt-1">
            <button
              type="button"
              onClick={() => void loadMoreSearchResults()}
              disabled={isFetchingMoreSearchResults}
              className="w-full py-[7px] rounded-lg border border-(--nous-border-1) dark:border-(--nous-shade) text-[11px] text-(--nous-fg-3) hover:text-(--nous-fg-1) hover:border-(--nous-sol)/30 transition-colors disabled:opacity-50"
              style={{ fontFamily: 'var(--nous-font-ui)' }}
            >
              {isFetchingMoreSearchResults
                ? 'Loading…'
                : 'Show more search results'}
            </button>
          </div>
        )}

        {/* CX8: the normal list pager is independent from server search. */}
        {hasMoreThreads && !hasSearchQuery && (
          <div className="px-2.5 pb-2.5 pt-1">
            <button
              type="button"
              onClick={handleLoadMore}
              disabled={isLoadingMore}
              className="w-full py-[7px] rounded-lg border border-(--nous-border-1) dark:border-(--nous-shade) text-[11px] text-(--nous-fg-3) hover:text-(--nous-fg-1) hover:border-(--nous-sol)/30 transition-colors disabled:opacity-50"
              style={{ fontFamily: 'var(--nous-font-ui)' }}
            >
              {isLoadingMore ? 'Loading…' : 'Show older threads'}
            </button>
          </div>
        )}
      </div>
    </aside>
  );
});
