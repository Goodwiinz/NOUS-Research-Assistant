import { clearSupabaseAuthCookies } from '@/lib/supabase/clearAuthCookies';
import { createClient as createSupabaseBrowserClient } from '@/lib/supabase/client';
import { api } from '@/services/api-client';
import { clearWorkspaceServiceCache } from '@/services/workspaceService';
import { getAppQueryClient } from '@/lib/query-client';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
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

// Number of signIn() calls currently running. signIn fetches /auth/me itself
// with the freshly issued access token, so the SIGNED_IN the same call emits
// must NOT kick off a second, concurrent profile fetch for the same login.
let signInInFlight = 0;

// Dedupes concurrent fetchProfile() calls (auth listener, initialize(),
// explicit callers) onto a single /auth/me round-trip.
let profileFetchInFlight: Promise<void> | null = null;

function clearUserScopedClientState(): void {
  clearWorkspaceServiceCache();
  useArtifactPanelStore.getState().reset();
  // The root QueryClient and APIClient singleton survive client-side auth
  // transitions, so neither may retain the previous user's private data or
  // bearer token after a session is rejected.
  getAppQueryClient()?.clear();
  api.clearAuth();
}

function getSupabaseClient(): SupabaseClient {
  const supabase = createSupabaseBrowserClient();

  if (!authStateListenerRegistered) {
    supabase.auth.onAuthStateChange((event, session) => {
      if ((event === 'SIGNED_IN' || event === 'TOKEN_REFRESHED') && session) {
        const currentUser = useAuthStore.getState().user;
        // signInInFlight: signIn() is already loading the profile with this
        // very token; a second fetch here would race it (two /auth/me calls,
        // last writer wins) and could tear the session down on a blip.
        if (!currentUser && signInInFlight === 0) {
          void useAuthStore.getState().fetchProfile();
        }
      }

      if (event === 'SIGNED_OUT') {
        clearUserScopedClientState();
        useAuthStore.setState({
          user: null,
          organization: null,
          isAuthenticated: false,
          error: null,
          ...CLEARED_PENDING_CONFIRMATION,
        });
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
    signInInFlight += 1;

    try {
      const supabase = getSupabaseClient();
      const { data, error: supabaseError } =
        await supabase.auth.signInWithPassword({
          email,
          password,
        });

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
        const accessToken = data.session.access_token;
        const profileData = await api.get<ProfileResponse>('/auth/me', {
          headers: { Authorization: `Bearer ${accessToken}` },
        });

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
      set({
        error: authError.message,
        isLoading: false,
      });
      throw authError;
    } finally {
      signInInFlight -= 1;
    }
  },

  signUp: async (userData: {
    email: string;
    password: string;
    first_name: string;
    last_name: string;
    organization_name?: string;
  }) => {
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

      if (supabaseError) {
        throw new Error(
          supabaseAuthErrorMessage(
            supabaseError,
            'Could not create your account. Please try again.'
          )
        );
      }

      if (supabaseData.session) {
        await get().fetchProfile();
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
      set({
        error: error instanceof Error ? error.message : 'Registration failed',
        isLoading: false,
      });
      throw error;
    }
  },

  signOut: async () => {
    clearUserScopedClientState();

    // Always clear local state first so the UI reflects signed-out
    // immediately regardless of the network outcome.
    set({
      user: null,
      organization: null,
      isAuthenticated: false,
      error: null,
      ...CLEARED_PENDING_CONFIRMATION,
    });

    try {
      const { error } = (await getSupabaseClient().auth.signOut()) ?? {};
      if (error) throw error;
    } catch (error) {
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
    const currentUser = get().user;

    // A late response from user A must never clear a newer user B session.
    if (currentUser && currentUser.id !== expectedUserId) {
      return;
    }

    clearUserScopedClientState();
    clearSupabaseAuthCookies();
    set({
      user: null,
      organization: null,
      isAuthenticated: false,
      error: null,
      ...CLEARED_PENDING_CONFIRMATION,
    });
  },

  resetPassword: async (email: string) => {
    set({ isLoading: true, error: null });

    try {
      const supabase = getSupabaseClient();
      const { error: supabaseError } =
        await supabase.auth.resetPasswordForEmail(email, {
          redirectTo: window.location.origin + '/reset-password',
        });

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

    const request = (async () => {
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

        if (userError || !user) {
          set({
            user: null,
            organization: null,
            isAuthenticated: false,
            isLoading: false,
          });
          return;
        }

        const {
          data: { session },
        } = await supabase.auth.getSession();
        const accessToken = session?.access_token;

        if (!accessToken) {
          set({
            user: null,
            organization: null,
            isAuthenticated: false,
            isLoading: false,
          });
          return;
        }

        const profileData = await api.get<ProfileResponse>('/auth/me', {
          headers: { Authorization: `Bearer ${accessToken}` },
        });

        set({
          user: profileData.user,
          organization: profileData.organization ?? null,
          isAuthenticated: true,
          isLoading: false,
          ...CLEARED_PENDING_CONFIRMATION,
        });
      } catch (error) {
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

        set({
          user: null,
          organization: null,
          isAuthenticated: false,
          isLoading: false,
          error: message,
        });
      }
    })();

    profileFetchInFlight = request;
    try {
      await request;
    } finally {
      profileFetchInFlight = null;
    }
  },

  updateUser: (userData: Partial<User>) => {
    const { user } = get();
    if (user) {
      set({ user: { ...user, ...userData } });
    }
  },

  clearPendingEmailConfirmation: () => set({ ...CLEARED_PENDING_CONFIRMATION }),

  clearError: () => set({ error: null }),
  setLoading: (loading: boolean) => set({ isLoading: loading }),

  initialize: async () => {
    try {
      const supabase = getSupabaseClient();
      // SECURITY (audit #7): verify the JWT with getUser() before treating the
      // app as authenticated; getSession() alone trusts the cookie.
      const {
        data: { user },
        error: userError,
      } = await supabase.auth.getUser();

      if (userError || !user) {
        set({ isAuthenticated: false, isLoading: false });
        return;
      }

      await get().fetchProfile();
    } catch (error) {
      set({
        isAuthenticated: false,
        isLoading: false,
        error:
          error instanceof Error
            ? error.message
            : 'Failed to initialize authentication',
      });
    }
  },
}));
