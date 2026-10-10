import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { useAuthStore } from '@/stores/authStore';

/** Guard user navigation only; identity transitions must always clear private UI. */
export function confirmArtifactNavigation(): boolean {
  const panel = useArtifactPanelStore.getState();
  const { user, isAuthenticated } = useAuthStore.getState();
  if (!panel.navigationGuard || !panel.scope || !isAuthenticated || !user)
    return true;
  let owner: unknown;
  try {
    owner = JSON.parse(panel.scope);
  } catch {
    return true;
  }
  if (
    !Array.isArray(owner) ||
    owner[0] !== user.id ||
    owner[1] !== user.organization_id
  )
    return true;
  return panel.navigationGuard();
}

/** UI-only lease: prevent a new buffer during an approved asynchronous binding. */
export function beginArtifactNavigation(): (() => void) | null {
  if (!confirmArtifactNavigation()) return null;
  const scope = useArtifactPanelStore.getState().scope;
  if (!scope) return () => {};
  const token = crypto.randomUUID();
  useArtifactPanelStore.setState({ pendingNavigation: { scope, token } });
  return () => {
    if (useArtifactPanelStore.getState().pendingNavigation?.token === token)
      useArtifactPanelStore.setState({ pendingNavigation: null });
  };
}

let committedUrl = '';
let committedState: unknown;
/** Snapshot only browser history metadata; never retain artifact bytes or guards. */
export function refreshCommittedArtifactRoute(): void {
  if (typeof window === 'undefined') return;
  committedUrl = window.location.href;
  committedState = window.history.state;
}
function installNavigationGuard(): void {
  if (typeof window === 'undefined') return;
  const host = window as Window & {
    __nousArtifactNavigationCleanup?: () => void;
  };
  host.__nousArtifactNavigationCleanup?.();
  refreshCommittedArtifactRoute();
  let approvedTraversal = false;
  const links = (event: MouseEvent): void => {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    const anchor =
      event.target instanceof Element ? event.target.closest('a[href]') : null;
    if (
      !(anchor instanceof HTMLAnchorElement) ||
      anchor.target === '_blank' ||
      anchor.hasAttribute('download')
    )
      return;
    const target = new URL(anchor.href, window.location.href);
    const current = new URL(window.location.href);
    if (
      target.origin !== current.origin ||
      (target.pathname === current.pathname && target.search === current.search)
    )
      return;
    if (!confirmArtifactNavigation()) {
      event.preventDefault();
      event.stopPropagation();
    }
  };
  const navigation = (window as unknown as { navigation?: EventTarget })
    .navigation;
  const traverse = (event: Event): void => {
    const value = event as Event & {
      navigationType?: string;
      hashChange?: boolean;
    };
    if (
      value.navigationType !== 'traverse' ||
      value.hashChange ||
      !event.cancelable
    )
      return;
    approvedTraversal = confirmArtifactNavigation();
    if (!approvedTraversal) event.preventDefault();
  };
  const pop = (event: PopStateEvent): void => {
    if (approvedTraversal) {
      approvedTraversal = false;
      return;
    }
    if (!confirmArtifactNavigation()) {
      event.stopImmediatePropagation();
      window.history.pushState(committedState, '', committedUrl);
    }
  };
  document.addEventListener('click', links, true);
  navigation?.addEventListener('navigate', traverse);
  window.addEventListener('popstate', pop, true);
  host.__nousArtifactNavigationCleanup = () => {
    document.removeEventListener('click', links, true);
    navigation?.removeEventListener('navigate', traverse);
    window.removeEventListener('popstate', pop, true);
  };
}
// Loaded by the root client provider before Next installs its popstate effect.
// Keep this listener stable across route renders: Next may synchronously commit
// during the same dispatch and remove a later component-owned listener.
installNavigationGuard();
