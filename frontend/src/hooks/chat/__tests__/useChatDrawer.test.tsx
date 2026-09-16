import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useChatDrawer } from '@/hooks/chat/useChatDrawer';

function makeFocusable(): HTMLButtonElement {
  const btn = document.createElement('button');
  document.body.appendChild(btn);
  return btn;
}

afterEach(() => {
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe('useChatDrawer', () => {
  it('closes at the docked breakpoint, focuses the composer and removes the listener', () => {
    const media = window.matchMedia('(min-width: 1280px)');
    const add = vi.spyOn(media, 'addEventListener');
    const remove = vi.spyOn(media, 'removeEventListener');
    vi.spyOn(window, 'matchMedia').mockReturnValue(media);
    const composer = document.createElement('textarea');
    document.body.appendChild(composer);
    const focusRef = { current: composer };
    const { result } = renderHook(() => useChatDrawer(focusRef));
    act(() => result.current.openDrawer());
    const onChange = add.mock.calls[0][1] as (
      event: MediaQueryListEvent
    ) => void;

    act(() => onChange({ matches: false } as MediaQueryListEvent));
    expect(result.current.isOpen).toBe(true);
    act(() => onChange({ matches: true } as MediaQueryListEvent));
    expect(result.current.isOpen).toBe(false);
    expect(document.activeElement).toBe(composer);
    expect(remove).toHaveBeenCalledWith('change', onChange);
    expect(window.matchMedia).toHaveBeenCalledWith('(min-width: 1280px)');
  });

  it('opens, closes, and toggles', () => {
    const { result } = renderHook(() => useChatDrawer());
    expect(result.current.isOpen).toBe(false);

    act(() => result.current.openDrawer());
    expect(result.current.isOpen).toBe(true);

    act(() => result.current.closeDrawer());
    expect(result.current.isOpen).toBe(false);

    act(() => result.current.toggleDrawer());
    expect(result.current.isOpen).toBe(true);
    act(() => result.current.toggleDrawer());
    expect(result.current.isOpen).toBe(false);
  });

  it('focuses the drawer on open and returns focus to the opener on close', () => {
    const opener = makeFocusable();
    opener.focus();
    const { result } = renderHook(() => useChatDrawer());
    const drawerEl = document.createElement('div');
    drawerEl.tabIndex = -1;
    document.body.appendChild(drawerEl);
    // Wire the ref the way JSX would (React doesn't render here).
    (
      result.current.drawerRef as React.MutableRefObject<HTMLDivElement | null>
    ).current = drawerEl;
    const focusSpy = vi.spyOn(drawerEl, 'focus');

    act(() => result.current.openDrawer());
    expect(focusSpy).toHaveBeenCalled();

    const openerFocusSpy = vi.spyOn(opener, 'focus');
    act(() => result.current.closeDrawer());
    expect(openerFocusSpy).toHaveBeenCalled();
  });

  it('closes on Escape while open', () => {
    const { result } = renderHook(() => useChatDrawer());
    act(() => result.current.openDrawer());
    expect(result.current.isOpen).toBe(true);

    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });

    expect(result.current.isOpen).toBe(false);
  });

  it('traps Tab focus inside the drawer at the last focusable element', () => {
    const { result } = renderHook(() => useChatDrawer());
    const root = document.createElement('div');
    const first = document.createElement('button');
    const last = document.createElement('button');
    root.appendChild(first);
    root.appendChild(last);
    document.body.appendChild(root);
    (
      result.current.drawerRef as React.MutableRefObject<HTMLDivElement | null>
    ).current = root;
    last.focus();
    expect(document.activeElement).toBe(last);

    const preventDefault = vi.fn();
    act(() => {
      result.current.handleDrawerKeyDown({
        key: 'Tab',
        shiftKey: false,
        preventDefault,
      } as unknown as React.KeyboardEvent);
    });

    expect(preventDefault).toHaveBeenCalled();
    expect(document.activeElement).toBe(first);
  });

  it('traps shift+Tab at the first focusable element back to the last', () => {
    const { result } = renderHook(() => useChatDrawer());
    const root = document.createElement('div');
    const first = document.createElement('button');
    const last = document.createElement('button');
    root.appendChild(first);
    root.appendChild(last);
    document.body.appendChild(root);
    (
      result.current.drawerRef as React.MutableRefObject<HTMLDivElement | null>
    ).current = root;
    first.focus();

    const preventDefault = vi.fn();
    act(() => {
      result.current.handleDrawerKeyDown({
        key: 'Tab',
        shiftKey: true,
        preventDefault,
      } as unknown as React.KeyboardEvent);
    });

    expect(preventDefault).toHaveBeenCalled();
    expect(document.activeElement).toBe(last);
  });

  it('traps shift+Tab when focus is still on the drawer root itself (open-focus state)', () => {
    const { result } = renderHook(() => useChatDrawer());
    const root = document.createElement('div');
    root.tabIndex = -1;
    const first = document.createElement('button');
    const last = document.createElement('button');
    root.appendChild(first);
    root.appendChild(last);
    document.body.appendChild(root);
    (
      result.current.drawerRef as React.MutableRefObject<HTMLDivElement | null>
    ).current = root;
    root.focus();
    expect(document.activeElement).toBe(root);

    const preventDefault = vi.fn();
    act(() => {
      result.current.handleDrawerKeyDown({
        key: 'Tab',
        shiftKey: true,
        preventDefault,
      } as unknown as React.KeyboardEvent);
    });

    expect(preventDefault).toHaveBeenCalled();
    expect(document.activeElement).toBe(last);
  });

  it('keeps the trap when there are no focusable descendants', () => {
    const { result } = renderHook(() => useChatDrawer());
    const root = document.createElement('div');
    root.tabIndex = -1;
    document.body.appendChild(root);
    (
      result.current.drawerRef as React.MutableRefObject<HTMLDivElement | null>
    ).current = root;
    root.focus();

    const focusSpy = vi.spyOn(root, 'focus');
    const preventDefault = vi.fn();
    act(() => {
      result.current.handleDrawerKeyDown({
        key: 'Tab',
        shiftKey: false,
        preventDefault,
      } as unknown as React.KeyboardEvent);
    });

    expect(preventDefault).toHaveBeenCalled();
    expect(focusSpy).toHaveBeenCalled();
  });
});
