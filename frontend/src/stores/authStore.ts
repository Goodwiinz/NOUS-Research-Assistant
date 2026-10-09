import { clearSupabaseAuthCookies } from '@/lib/supabase/clearAuthCookies';
import { createClient as createSupabaseBrowserClient } from '@/lib/supabase/client';
import { api } from '@/services/api-client';
import { clearWorkspaceServiceCache } from '@/services/workspaceService';
import { getAppQueryClient } from '@/lib/query-client';
import {
  clearClientOwner,
  readClientOwner,
  writeClientOwner,
} from '@/lib/client-owner';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { resetAccountSession } from '@/lib/account-session';
import { useChatStore } from '@/store/chat-store';
import { useAgentChatStore } from '@/store/agentChatStore';
import { useProjectStore } from '@/store/projectStore';
import { useProjectChatStore } from '@/store/projectChatStore';
import { usePipelineStore } from '@/store/pipelineStore';
import { useCitationStore } from '@/store/citationStore';
import { useNotificationStore } from '@/store/notificationStore';
import { useLLMChatStore } from '@/store/llm-chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { useResearchEngineStore } from '@/store/research-engine-store';
import { Organization, RegisterResult, User } from '@/types';
import { supabaseAuthErrorMessage } from '@/utils/supabaseAuthError';
import type { SupabaseClient } from '@supabase/supabase-js';
import { create } from 'zustand';

interface ProfileResponse {
  user: User;
  organization?: Organization;
}

interface AuthState {
  // State
  user: User | null;
  organization: Organization | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
  pendingEmailConfirmation: boolean;
  /**
   * Address the pending verification mail was sent to. Kept in the store (not
   * in the register form's local state) so the pending screen survives a
   * remount — otherwise it renders an empty address and the resend button
   * calls GoTrue with `email: ''`.
   */
  pendingConfirmationEmail: string | null;
  /**
   * GoTrue's enumeration protection answers a signup for an already-registered
   * address with a success payload, so we cannot promise a mail that will
   * never arrive. Set when that shape is detected.
   */
  pendingSignupPossiblyExisting: boolean;

  // Actions
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (userData: {
    email: string;
    password: string;
    first_name: string;
    last_name: string;
    organization_name?: string;
  }) => Promise<RegisterResult>;
  signOut: () => Promise<void>;
  invalidateRejectedSession: (expectedUserId: string | null) => void;
  resetPassword: (email: string) => Promise<void>;
  fetchProfile: () => Promise<void>;
  updateUser: (user: Partial<User>) => void;
  clearPendingEmailConfirmation: () => void;
  clearError: () => void;
  setLoading: (loading: boolean) => void;
  initialize: () => Promise<void>;
}

// Every site that drops the pending-confirmation flag must drop the address
// and the possibly-existing hint with it, so a shared machine never shows a
// previous visitor's email.
const CLEARED_PENDING_CONFIRMATION = {
  pendingEmailConfirmation: false,
  pendingConfirmationEmail: null,
  pendingSignupPossiblyExisting: false,
} satisfies Partial<AuthState>;

/**
 * Read the HTTP status off an APIErrorClass-shaped rejection. Structural rather
 * than `instanceof` so a re-bundled/duplicated copy of the error class still
 * resolves.
 */
function errorStatusCode(error: unknown): number | undefined {
  if (!error || typeof error !== 'object' || !('error' in error)) {
    return undefined;
  }
  const payload = (error as { error?: { status_code?: unknown } }).error;
  return typeof payload?.status_code === 'number'
    ? payload.status_code
    : undefined;
}

let authStateListenerRegistered = false;

// The latest explicit login or signup owns its profile request. signIn fetches /auth/me itself
// with the freshly issued access token, so the SIGNED_IN the same call emits
// must NOT kick off a second, concurrent profile fetch for the same login.
let activeSignIn: object | null = null;
let authRevision = 0;
// Includes an identity whose profile is still loading (user is then null).
let sessionUserId: string | null = null;

// Dedupes concurrent fetchProfile() calls (auth listener, initialize(),
// explicit callers) onto a single /auth/me round-trip.
let profileFetchInFlight: Promise<void> | null = null;

/** A later account transition superseded this password login. */
export class SignInSupersededError extends Error {
  constructor() {
    super(
      'This sign-in was interrupted by another sign-in or sign-out. Please try again.'
    );
    this.name = 'SignInSupersededError';
  }
}

/**
 * One step of an account change. Storage can refuse a write (quota) or any
 * access (SecurityError), and zustand's persist middleware forwards that
 * synchronous failure out of setState, so one persisted store could
 * otherwise abort every step after it. The in-memory part of a failed step
 * has already happened by the time it throws; log it and keep going.
 */
function attempt(step: string, run: () => void): void {
  try {
    run();
  } catch (error) {
    console.warn(`[Auth] Could not ${step} during an account change`, error);
  }
}

function clearUserScopedClientState(): void {
  // Storage-free and security-critical first, so no later failure can skip
  // them: the APIClient singleton and root QueryClient survive client-side
  // auth transitions and must not keep the previous user's bearer token or
  // private data; the account signal revokes async ownership before any
  // observable state resets (abort alone is insufficient: an already queued
  // callback or decoded body can still run).
  api.clearAuth();
  getAppQueryClient()?.clear();
  resetAccountSession();
  attempt('reset the chat store', () => useChatStore.getState().reset());
  attempt('reset the agent chat store', () =>
    useAgentChatStore.getState().reset()
  );
  attempt('reset the project store', () => useProjectStore.getState().reset());
  attempt('reset the project chat store', () =>
    useProjectChatStore.getState().reset()
  );
  attempt('reset the pipeline store', () =>
    usePipelineStore.getState().reset()
  );
  attempt('clear citations', () => useCitationStore.getState().clearCitations());
  attempt('reset the research engine store', () =>
    useResearchEngineStore.setState(useResearchEngineStore.getInitialState())
  );
  attempt('reset agent activity', () =>
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null })
  );
  attempt('clear notifications', () =>
    useNotificationStore.getState().clearAll()
  );
  attempt('clear the copied message id', () =>
    useLLMChatStore.getState().setCopiedMessageId(null)
  );
  attempt('clear the workspace cache', clearWorkspaceServiceCache);
  // Nobody vouches for the persisted caches once they have been cleared, so
  // forget who owned them: the next sign-in, by anyone, clears again instead
  // of trusting whatever a late write put back. The next verified profile
  // re-stamps its own account.
  attempt('forget the client owner', clearClientOwner);
  attempt('reset the artifact panel', () =>
    useArtifactPanelStore.getState().reset()
  );
}

function clearSession(): void {
  authRevision += 1;
  profileFetchInFlight = null;
  sessionUserId = null;
  activeSignIn = null;
  clearUserScopedClientState();
  useAuthStore.setState({
    user: null,
    organization: null,
    isAuthenticated: false,
    isLoading: false,
    error: null,
    ...CLEARED_PENDING_CONFIRMATION,
  });
}

function settleMissingSession(): void {
  // A missing session during anonymous bootstrap is not an account change.
  // Keep public forms mounted, but still revoke a known or pending identity
  // (including a login whose SDK request has not returned its user yet).
  const { user, isAuthenticated } = useAuthStore.getState();
  if (isAuthenticated || user || sessionUserId || activeSignIn) {
    clearSession();
  } else {
    useAuthStore.setState({ isLoading: false });
  }
}

function observeIdentity(userId: string): void {
  // In-memory identity answers first: a tab still holding A must clear on
  // B's event even when another tab already re-stamped the shared storage as
  // B. Only a fresh load, where memory knows nothing, consults the persisted
  // owner stamp so the same account keeps its caches across a reload. An
  // unknown stamp (first deploy, storage denied) counts as a different owner.
  const previousId =
    useAuthStore.getState().user?.id ?? sessionUserId ?? readClientOwner();
  if (previousId !== userId) {
    authRevision += 1;
    profileFetchInFlight = null;
    clearUserScopedClientState();
    useAuthStore.setState({
      user: null,
      organization: null,
      isAuthenticated: false,
      isLoading: true,
      error: null,
      ...CLEARED_PENDING_CONFIRMATION,
    });
  }
  sessionUserId = userId;
}

/**
 * A verified account (getUser + /auth/me, not merely a cookie session) is
 * about to be published. Only now is the origin stamped with it, so the
 * stamp and the payload owners share one trust level, and the persisted
 * chat selection is handed over: the chat store adopts it only if it was
 * written for this same account and stamps its later writes with it, so
 * the consumers that react to `isAuthenticated` already see a settled
 * selection and never a previous account's. A storage failure while
 * adopting must not tear down a login the backend has just verified.
 */
function publishVerifiedAccount(userId: string): void {
  writeClientOwner(userId);
  attempt('adopt the persisted chat selection', () =>
    useChatStore.getState().adoptPersistedSelection(userId)
  );
}

function scheduleProfileFetch(): void {
  const revision = authRevision;
  // Supabase invokes auth listeners while holding its auth lock. Leave the
  // callback before calling getUser/getSession, and discard stale schedules.
  setTimeout(() => {
    if (
      revision === authRevision &&
      !activeSignIn &&
      !useAuthStore.getState().user
    ) {
      void useAuthStore.getState().fetchProfile();
    }
  }, 0);
}

function getSupabaseClient(): SupabaseClient {
  const supabase = createSupabaseBrowserClient();

  if (!authStateListenerRegistered) {
    supabase.auth.onAuthStateChange((event, session) => {
      if ((event === 'SIGNED_IN' || event === 'TOKEN_REFRESHED') && session) {
        observeIdentity(session.user.id);
        scheduleProfileFetch();
      }

      if (event === 'SIGNED_OUT') {
        settleMissingSession();
      }
    });

    authStateListenerRegistered = true;
  }

  return supabase;
}

export const useAuthStore = create<AuthState>()((set, get) => ({
  // Initial state
  user: null,
  organization: null,
  isAuthenticated: false,
  isLoading: true,
  error: null,
  ...CLEARED_PENDING_CONFIRMATION,

  signIn: async (email: string, password: string) => {
    set({ isLoading: true, error: null });
    const attempt = {};
    activeSignIn = attempt;
    let revision = ++authRevision;
    profileFetchInFlight = null;
    let loginUserId: string | undefined;

    try {
      const supabase = getSupabaseClient();
      const { data, error: supabaseError } =
        await supabase.auth.signInWithPassword({
          email,
          password,
        });

      if (activeSignIn !== attempt) throw new SignInSupersededError();
      if (supabaseError) {
        throw new Error(
          supabaseAuthErrorMessage(
            supabaseError,
            'Could not sign you in. Please try again.'
          )
        );
      }

      // Use the session from signIn directly — getSession() may return null
      // before the SSR cookie is established
      if (data.session) {
        loginUserId = data.session.user?.id;
        // An SDK identity event may have fired during signInWithPassword.
        // Its own event is fine; another account's event supersedes this login.
        if (revision !== authRevision && sessionUserId !== loginUserId)
          throw new SignInSupersededError();
        if (loginUserId) observeIdentity(loginUserId);
        revision = authRevision;
        const accessToken = data.session.access_token;
        const profileData = await api.get<ProfileResponse>('/auth/me', {
          headers: { Authorization: `Bearer ${accessToken}` },
        });

        if (activeSignIn !== attempt || revision !== authRevision)
          throw new SignInSupersededError();
        observeIdentity(profileData.user.id);
        publishVerifiedAccount(profileData.user.id);
        set({
          user: profileData.user,
          organization: profileData.organization ?? null,
          isAuthenticated: true,
          isLoading: false,
          ...CLEARED_PENDING_CONFIRMATION,
        });
      } else {
        set({ isLoading: false });
      }
    } catch (error) {
      const authError =
        error instanceof Error ? error : new Error('Login failed');
      if (activeSignIn !== attempt || revision !== authRevision)
        throw authError;
      set({
        error: authError.message,
        isLoading: false,
      });
      throw authError;
    } finally {
      if (activeSignIn === attempt) {
        activeSignIn = null;
        if (sessionUserId && sessionUserId !== loginUserId)
          scheduleProfileFetch();
      }
    }
  },

  signUp: async (userData: {
    email: string;
    password: string;
    first_name: string;
    last_name: string;
    organization_name?: string;
  }) => {
    const attempt = {};
    activeSignIn = attempt;
    let revision = ++authRevision;
    profileFetchInFlight = null;
    let signupUserId: string | undefined;
    set({ isLoading: true, error: null, ...CLEARED_PENDING_CONFIRMATION });

    try {
      const supabase = getSupabaseClient();
      const emailRedirectTo = new URL('/auth/callback', window.location.origin);
      emailRedirectTo.searchParams.set('next', '/verify-email');

      const { data: supabaseData, error: supabaseError } =
        await supabase.auth.signUp({
          email: userData.email,
          password: userData.password,
          options: {
            data: {
              first_name: userData.first_name,
              last_name: userData.last_name,
              organization_name: userData.organization_name,
            },
            emailRedirectTo: emailRedirectTo.toString(),
          },
        });

      if (activeSignIn !== attempt)
        throw new DOMException('Authentication superseded', 'AbortError');
      signupUserId = supabaseData.session?.user?.id;
      if (revision !== authRevision && sessionUserId !== signupUserId)
        throw new DOMException('Authentication superseded', 'AbortError');
      if (supabaseError) {
        throw new Error(
          supabaseAuthErrorMessage(
            supabaseError,
            'Could not create your account. Please try again.'
          )
        );
      }

      if (supabaseData.session) {
        if (signupUserId) observeIdentity(signupUserId);
        revision = authRevision;
        await get().fetchProfile();
        if (activeSignIn !== attempt || revision !== authRevision)
          throw new DOMException('Authentication superseded', 'AbortError');
        return { requiresEmailConfirmation: false };
      }

      // No session = email confirmation required.
      //
      // GoTrue's enumeration protection answers a signup for an
      // already-registered address with the same success shape, but with an
      // obfuscated user carrying an EMPTY `identities` array. Telling that
      // visitor "check your inbox" is a fake success — no mail is ever sent.
      // `Array.isArray` is load-bearing: `identities` is optional, and a
      // missing field must NOT be read as "empty".
      const signupUser = supabaseData.user;
      const possiblyExistingAccount =
        !!signupUser &&
        Array.isArray(signupUser.identities) &&
        signupUser.identities.length === 0;

      set({
        isLoading: false,
        pendingEmailConfirmation: true,
        pendingConfirmationEmail: userData.email,
        pendingSignupPossiblyExisting: possiblyExistingAccount,
      });
      // The outer flow stays identical for every visitor — the branch only
      // changes the guidance shown on the pending screen.
      return { requiresEmailConfirmation: true };
    } catch (error) {
      if (activeSignIn !== attempt || revision !== authRevision)
        throw new DOMException('Authentication superseded', 'AbortError');
      set({
        error: error instanceof Error ? error.message : 'Registration failed',
        isLoading: false,
      });
      throw error;
    } finally {
      if (activeSignIn === attempt) {
        activeSignIn = null;
        if (sessionUserId && sessionUserId !== signupUserId)
          scheduleProfileFetch();
      }
    }
  },

  signOut: async () => {
    clearSession();
    const revision = authRevision;

    try {
      const { error } = (await getSupabaseClient().auth.signOut()) ?? {};
      if (error) throw error;
    } catch (error) {
      if (revision !== authRevision) return;
      const message =
        error instanceof Error ? error.message : 'Sign out failed';

      // supabase-js returns early from signOut() whenever the revocation
      // request fails with anything other than 401/403/404 (network error,
      // 5xx): it never reaches _removeSession(), so the SSR auth cookie
      // SURVIVES and the next page load silently signs the user back in.
      // Destroy the cookie ourselves so the signed-out state the UI just
      // rendered is actually true. Re-throwing instead would be a lie AND
      // unobservable — every logout call site invokes this bare, so the
      // rejection only ever became an unhandled promise rejection.
      clearSupabaseAuthCookies();

      set({
        error: `Signed out on this device, but the session could not be revoked on the server: ${message}`,
      });
    }
  },

  invalidateRejectedSession: (expectedUserId: string | null) => {
    const currentUserId = get().user?.id ?? sessionUserId;

    // Includes B while its profile is still loading, before user is populated.
    if (currentUserId && currentUserId !== expectedUserId) {
      return;
    }

    clearSession();
    clearSupabaseAuthCookies();
  },

  resetPassword: async (email: string) => {
    const revision = authRevision;
    set({ isLoading: true, error: null });

    try {
      const supabase = getSupabaseClient();
      const { error: supabaseError } =
        await supabase.auth.resetPasswordForEmail(email, {
          redirectTo: window.location.origin + '/reset-password',
        });

      if (revision !== authRevision)
        throw new DOMException('Authentication superseded', 'AbortError');
      if (supabaseError) {
        throw new Error(
          supabaseAuthErrorMessage(
            supabaseError,
            'Could not send the reset email. Please try again.'
          )
        );
      }

      set({ isLoading: false });
    } catch (error) {
      if (revision !== authRevision)
        throw new DOMException('Authentication superseded', 'AbortError');
      set({
        error:
          error instanceof Error ? error.message : 'Failed to send reset email',
        isLoading: false,
      });
      throw error;
    }
  },

  fetchProfile: async () => {
    // In-flight guard: a second concurrent profile fetch is a no-op that just
    // awaits the first one's result instead of issuing another /auth/me.
    if (profileFetchInFlight) {
      await profileFetchInFlight;
      return;
    }

    let revision = authRevision;
    // Start on a microtask so the deduplication promise is installed before
    // SDK callbacks can re-enter this action.
    const request = Promise.resolve().then(async () => {
      if (revision !== authRevision) return;
      try {
        const supabase = getSupabaseClient();
        // SECURITY (audit #7): gate on getUser(), which verifies the JWT with
        // Supabase, rather than getSession(), which only reads the (forgeable)
        // cookie — a forged session cookie must not make the app look
        // authenticated. getSession() is then used solely to read the token to
        // forward to /auth/me (the backend re-validates it).
        const {
          data: { user },
          error: userError,
        } = await supabase.auth.getUser();

        if (revision !== authRevision) return;
        if (userError || !user) {
          settleMissingSession();
          return;
        }
        observeIdentity(user.id);
        revision = authRevision;
        profileFetchInFlight = request;

        const {
          data: { session },
        } = await supabase.auth.getSession();
        if (revision !== authRevision) return;
        const accessToken = session?.access_token;

        if (!accessToken || (session.user?.id && session.user.id !== user.id)) {
          clearSession();
          return;
        }

        const profileData = await api.get<ProfileResponse>('/auth/me', {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        if (revision !== authRevision) return;
        observeIdentity(profileData.user.id);
        publishVerifiedAccount(profileData.user.id);

        set({
          user: profileData.user,
          organization: profileData.organization ?? null,
          isAuthenticated: true,
          isLoading: false,
          ...CLEARED_PENDING_CONFIRMATION,
        });
      } catch (error) {
        if (revision !== authRevision) return;
        const message =
          error instanceof Error ? error.message : 'Failed to fetch profile';
        const statusCode = errorStatusCode(error);
        const tokenRejected = statusCode === 401 || statusCode === 403;

        // A transient /auth/me failure (network blip, 5xx, timeout) must NOT
        // tear down a session that is already established — signIn() sets
        // isAuthenticated before this can resolve, and clearing it here bounces
        // the user straight back to /login moments after a successful login.
        // Only an outright rejection of the token (401/403) invalidates the
        // session; the two explicit "no verified user / no token" branches
        // above still clear it, so a genuinely dead session is never kept.
        if (get().isAuthenticated && get().user !== null && !tokenRejected) {
          set({ isLoading: false, error: message });
          return;
        }

        settleMissingSession();
        set({ error: message });
      }
    });

    profileFetchInFlight = request;
    try {
      await request;
    } finally {
      if (profileFetchInFlight === request) profileFetchInFlight = null;
    }
  },

  updateUser: (userData: Partial<User>) => {
    const { user } = get();
    if (user) {
      const updated = { ...user, ...userData };
      observeIdentity(updated.id);
      set({ user: updated, isAuthenticated: true, isLoading: false });
    }
  },

  clearPendingEmailConfirmation: () => set({ ...CLEARED_PENDING_CONFIRMATION }),

  clearError: () => set({ error: null }),
  setLoading: (loading: boolean) => set({ isLoading: loading }),

  // fetchProfile verifies the JWT and owns teardown/error handling too.
  initialize: async () => get().fetchProfile(),
}));
