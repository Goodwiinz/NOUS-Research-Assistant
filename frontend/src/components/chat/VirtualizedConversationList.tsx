'use client';

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { VariableSizeList, type ListChildComponentProps } from 'react-window';
import { Pin } from 'lucide-react';

interface Conversation {
  id: string;
}

interface Section<T> {
  label: string;
  items: T[];
}

type Row<T> =
  | { kind: 'heading'; key: string; label: string; count: number }
  | { kind: 'conversation'; key: string; item: T; position: number };

interface RowData<T> {
  rows: Row<T>[];
  total: number;
  renderConversation: (item: T) => ReactNode;
  measure: (key: string, index: number, height: number) => void;
}

function ConversationRow<T extends Conversation>({
  index,
  style,
  data,
}: ListChildComponentProps<RowData<T>>): ReactNode {
  const row = data.rows[index];
  const measureRow = data.measure;
  const contentRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!content) return;
    const measure = (): void => {
      const height = content.getBoundingClientRect().height;
      if (height > 0) measureRow(row.key, index, Math.ceil(height));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(content);
    return () => observer.disconnect();
  }, [measureRow, index, row.key]);

  return (
    <div
      style={style}
      data-row-index={index}
      role={row.kind === 'conversation' ? 'listitem' : 'presentation'}
      aria-posinset={row.kind === 'conversation' ? row.position : undefined}
      aria-setsize={row.kind === 'conversation' ? data.total : undefined}
      data-conversation-id={
        row.kind === 'conversation' ? row.item.id : undefined
      }
    >
      <div ref={contentRef} className="px-2.5">
        {row.kind === 'heading' ? (
          <div className="flex items-center gap-1.5 px-2 pt-2.5 pb-1.5">
            {row.label === 'Pinned' && (
              <Pin
                aria-hidden
                className="w-[9px] h-[9px] text-(--nous-sol) dark:text-(--nous-helios)"
              />
            )}
            <h3
              className="text-[11px] font-medium text-(--nous-fg-3)"
              style={{ fontFamily: 'var(--nous-font-ui)' }}
            >
              {row.label}
            </h3>
            <span className="ml-auto px-[5px] py-px bg-(--nous-bg-2) dark:bg-(--nous-obsidian) border border-(--nous-border-1) dark:border-(--nous-shade) rounded-[3px] text-[9px] text-(--nous-fg-2)">
              {row.count}
            </span>
          </div>
        ) : (
          data.renderConversation(row.item)
        )}
      </div>
    </div>
  );
}

/** One measured scroll window for date headings and conversation rows. */
export function VirtualizedConversationList<T extends Conversation>({
  sections,
  activeId,
  renderConversation,
}: {
  sections: Section<T>[];
  activeId: string | null;
  renderConversation: (item: T) => ReactNode;
}): ReactNode {
  const containerRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<VariableSizeList<RowData<T>>>(null);
  const sizes = useRef(new Map<string, number>());
  const lastRevealedId = useRef<string | null>(null);
  const lastRevealedIndex = useRef(-1);
  const revealTarget = useRef<number | null>(null);
  const revealFrame = useRef<number | null>(null);
  const [height, setHeight] = useState(400);
  const pendingFocus = useRef<{
    id: string;
    control: 'primary' | 'first' | 'last';
  } | null>(null);
  const [focusRequest, setFocusRequest] = useState(0);
  const focusedId = useRef<string | null>(null);
  const [visibleRange, setVisibleRange] = useState({ start: 0, stop: 0 });
  const rows = useMemo(() => {
    let position = 0;
    return sections.flatMap((section): Row<T>[] => [
      {
        kind: 'heading',
        key: `heading:${section.label}`,
        label: section.label,
        count: section.items.length,
      },
      ...section.items.map((item): Row<T> => ({
        kind: 'conversation',
        key: item.id,
        item,
        position: ++position,
      })),
    ]);
  }, [sections]);
  const conversationIndices = useMemo(
    () =>
      rows.flatMap((row, index) =>
        row.kind === 'conversation' ? [index] : []
      ),
    [rows]
  );
  const finishReveal = useCallback((): void => {
    if (revealTarget.current === null) return;
    if (revealFrame.current !== null) cancelAnimationFrame(revealFrame.current);
    revealFrame.current = requestAnimationFrame(() => {
      const scroll = scrollRef.current;
      const target = scroll?.querySelector<HTMLElement>(
        `[data-row-index="${revealTarget.current}"]`
      );
      if (scroll && target && revealTarget.current !== null) {
        const viewport = scroll.getBoundingClientRect();
        const bounds = target.getBoundingClientRect();
        const delta =
          bounds.top < viewport.top
            ? bounds.top - viewport.top
            : Math.max(0, bounds.bottom - viewport.bottom);
        if (delta) listRef.current?.scrollTo(scroll.scrollTop + delta);
      }
      revealFrame.current = null;
    });
  }, []);
  const measure = useCallback(
    (key: string, index: number, measuredHeight: number): void => {
      if (sizes.current.get(key) === measuredHeight) return;
      sizes.current.set(key, measuredHeight);
      listRef.current?.resetAfterIndex(index);
      // Finish the estimated reveal after the real row geometry settles.
      finishReveal();
    },
    [finishReveal]
  );
  const itemSize = useCallback(
    (index: number): number =>
      sizes.current.get(rows[index].key) ??
      (rows[index].kind === 'heading' ? 36 : 96),
    [rows]
  );

  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const measureHeight = (): void => {
      const next = container.getBoundingClientRect().height;
      if (next > 0) setHeight(next);
    };
    measureHeight();
    const observer = new ResizeObserver(measureHeight);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    listRef.current?.resetAfterIndex(0);
    // Removed/search-filtered identities must not accumulate height entries.
    const keys = new Set(rows.map((row) => row.key));
    for (const key of sizes.current.keys()) {
      if (!keys.has(key)) sizes.current.delete(key);
    }
  }, [rows]);
  useLayoutEffect(() => {
    const index = rows.findIndex(
      (row) => row.kind === 'conversation' && row.item.id === activeId
    );
    if (index < 0) {
      lastRevealedId.current = null;
    } else if (
      lastRevealedId.current !== activeId ||
      lastRevealedIndex.current !== index
    ) {
      lastRevealedId.current = activeId;
      lastRevealedIndex.current = index;
      revealTarget.current = index;
      listRef.current?.scrollToItem(index, 'smart');
      finishReveal();
    }
  }, [activeId, rows, finishReveal]);
  useLayoutEffect(finishReveal, [finishReveal, height, visibleRange]);
  useEffect(
    () => () => {
      if (revealFrame.current !== null)
        cancelAnimationFrame(revealFrame.current);
    },
    []
  );

  const rowElement = useCallback(
    (id: string): HTMLElement | undefined =>
      Array.from(
        containerRef.current?.querySelectorAll<HTMLElement>(
          '[data-conversation-id]'
        ) ?? []
      ).find((element) => element.dataset.conversationId === id),
    []
  );
  useLayoutEffect(() => {
    if (pendingFocus.current) {
      const { id, control } = pendingFocus.current;
      const row = rowElement(id);
      const controls = row?.querySelectorAll<HTMLElement>('button, input');
      const target =
        control === 'primary'
          ? row?.querySelector<HTMLElement>('.sb-conv')
          : control === 'first'
            ? controls?.[0]
            : controls?.[controls.length - 1];
      if (target) {
        target.focus();
        pendingFocus.current = null;
      }
    } else if (
      focusedId.current &&
      !rowElement(focusedId.current) &&
      document.activeElement === document.body
    ) {
      // A mouse/trackpad scroll may unmount the focused row. Retain focus in
      // the list so the next arrow key can reveal its logical neighbour.
      containerRef.current?.focus();
    }
  }, [focusRequest, rowElement, visibleRange]);

  const focusRow = (
    index: number,
    control: 'primary' | 'first' | 'last' = 'primary'
  ): void => {
    const row = rows[index];
    if (row?.kind !== 'conversation') return;
    revealTarget.current = index;
    listRef.current?.scrollToItem(index, 'smart');
    finishReveal();
    pendingFocus.current = { id: row.item.id, control };
    setFocusRequest((request) => request + 1);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const element = event.target as HTMLElement;
    const currentRow = element.closest<HTMLElement>('[data-conversation-id]');
    const id = currentRow?.dataset.conversationId ?? focusedId.current;
    const current = conversationIndices.findIndex((index) => {
      const row = rows[index];
      return row.kind === 'conversation' && row.item.id === id;
    });
    let target = current;
    if (event.key === 'Home') target = 0;
    else if (event.key === 'End') target = conversationIndices.length - 1;
    else if (event.key === 'ArrowDown')
      target = Math.min(current + 1, conversationIndices.length - 1);
    else if (event.key === 'ArrowUp') target = Math.max(current - 1, 0);
    else if (event.key === 'Tab' && currentRow) {
      const controls = Array.from(
        currentRow.querySelectorAll<HTMLElement>('button, input')
      );
      const boundary = event.shiftKey
        ? controls[0]
        : controls[controls.length - 1];
      target = current + (event.shiftKey ? -1 : 1);
      const nextRow = rows[conversationIndices[target]];
      if (
        element !== boundary ||
        nextRow?.kind !== 'conversation' ||
        rowElement(nextRow.item.id)
      )
        return;
    } else return;
    if (target < 0 || conversationIndices[target] === undefined) return;
    event.preventDefault();
    focusRow(
      conversationIndices[target],
      event.key === 'Tab' ? (event.shiftKey ? 'last' : 'first') : 'primary'
    );
  };

  return (
    <div
      ref={containerRef}
      className="flex-1 min-h-0"
      role="list"
      aria-label="Conversations"
      tabIndex={-1}
      onWheelCapture={() => {
        revealTarget.current = null;
      }}
      onTouchStartCapture={() => {
        revealTarget.current = null;
      }}
      onPointerDownCapture={() => {
        revealTarget.current = null;
      }}
      onKeyDown={onKeyDown}
      onFocusCapture={(event) => {
        const row = (event.target as HTMLElement).closest<HTMLElement>(
          '[data-conversation-id]'
        );
        if (row) focusedId.current = row.dataset.conversationId ?? null;
      }}
      onBlurCapture={(event) => {
        if (
          event.relatedTarget &&
          !event.currentTarget.contains(event.relatedTarget as Node)
        )
          focusedId.current = null;
      }}
    >
      <VariableSizeList<RowData<T>>
        ref={listRef}
        outerRef={scrollRef}
        height={height}
        width="100%"
        itemCount={rows.length}
        itemSize={itemSize}
        itemKey={(index, data) => data.rows[index].key}
        itemData={{
          rows,
          total: conversationIndices.length,
          renderConversation,
          measure,
        }}
        overscanCount={5}
        onItemsRendered={({ overscanStartIndex, overscanStopIndex }) => {
          setVisibleRange((range) =>
            range.start === overscanStartIndex &&
            range.stop === overscanStopIndex
              ? range
              : { start: overscanStartIndex, stop: overscanStopIndex }
          );
        }}
      >
        {ConversationRow<T>}
      </VariableSizeList>
    </div>
  );
}
