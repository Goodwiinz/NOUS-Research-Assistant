import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Organization, User } from '@/types';
describe('useAuthStore signOut', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  it('clears workspace-scoped caches when signing out', async () => {
    const mockBrowserSignOut = vi.fn().mockResolvedValue(undefined);
    const mockClearWorkspaceServiceCache = vi.fn();

    vi.doMock('@/lib/supabase/client', () => ({
      createClient: () => ({
        auth: {
          onAuthStateChange: vi.fn(),
          signOut: mockBrowserSignOut,
        },
      }),
    }));

    vi.doMock('@/services/workspaceService', () => ({
      clearWorkspaceServiceCache: mockClearWorkspaceServiceCache,
    }));

    const { useAuthStore } =
      await vi.importActual<typeof import('@/stores/authStore')>(
        '@/stores/authStore'
      );

    useAuthStore.setState({
      user: { id: 'user-1' } as any,
      organization: { id: 'org-1' } as any,
      isAuthenticated: true,
      isLoading: false,
      error: 'stale error',
      pendingEmailConfirmation: true,
      pendingConfirmationEmail: 'ada@example.com',
      pendingSignupPossiblyExisting: true,
    });

    useAuthStore.getState().signOut();

    expect(mockClearWorkspaceServiceCache).toHaveBeenCalledTimes(1);
    expect(mockBrowserSignOut).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState()).toEqual(
      expect.objectContaining({
        user: null,
        organization: null,
        isAuthenticated: false,
        error: null,
        pendingEmailConfirmation: false,
      })
    );
  });

  it('clears the pending confirmation email when signing out', async () => {
    // A shared machine must never show the previous visitor's address.
    const mockBrowserSignOut = vi.fn().mockResolvedValue(undefined);

    vi.doMock('@/lib/supabase/client', () => ({
      createClient: () => ({
        auth: {
          onAuthStateChange: vi.fn(),
          signOut: mockBrowserSignOut,
        },
      }),
    }));

    vi.doMock('@/services/workspaceService', () => ({
      clearWorkspaceServiceCache: vi.fn(),
    }));

    const { useAuthStore } =
      await vi.importActual<typeof import('@/stores/authStore')>(
        '@/stores/authStore'
      );

    useAuthStore.setState({
      pendingEmailConfirmation: true,
      pendingConfirmationEmail: 'ada@example.com',
      pendingSignupPossiblyExisting: true,
    });

    useAuthStore.getState().signOut();

    expect(useAuthStore.getState()).toEqual(
      expect.objectContaining({
        pendingEmailConfirmation: false,
        pendingConfirmationEmail: null,
        pendingSignupPossiblyExisting: false,
      })
    );
  });

  it('destroys the local auth cookie when server-side revocation fails', async () => {
    // supabase-js returns { error } WITHOUT clearing the local session when the
    // revocation request fails, so the SSR cookie would otherwise survive and
    // sign the user straight back in on the next page load.
    const mockBrowserSignOut = vi
      .fn()
      .mockResolvedValue({ error: new Error('Failed to fetch') });

    vi.doMock('@/lib/supabase/client', () => ({
      createClient: () => ({
        auth: {
          onAuthStateChange: vi.fn(),
          signOut: mockBrowserSignOut,
        },
      }),
    }));

    vi.doMock('@/services/workspaceService', () => ({
      clearWorkspaceServiceCache: vi.fn(),
    }));

    const { useAuthStore } =
      await vi.importActual<typeof import('@/stores/authStore')>(
        '@/stores/authStore'
      );

    document.cookie = 'sb-example-auth-token=chunkless-session; Path=/';
    document.cookie = 'sb-example-auth-token.0=first-chunk; Path=/';
    document.cookie = 'unrelated-cookie=keep-me; Path=/';
    expect(document.cookie).toContain('sb-example-auth-token=');

    await expect(useAuthStore.getState().signOut()).resolves.toBeUndefined();

    expect(document.cookie).not.toContain('sb-example-auth-token');
    expect(document.cookie).toContain('unrelated-cookie=keep-me');
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().error).toMatch(/could not be revoked/i);
  });

  it('invalidates a rejected session synchronously without starting SDK signOut', async () => {
    const mockBrowserSignOut = vi.fn();
    const mockCreateClient = vi.fn(() => ({
      auth: {
        onAuthStateChange: vi.fn(),
        signOut: mockBrowserSignOut,
      },
    }));
    const mockClearWorkspaceServiceCache = vi.fn();
    const mockClearArtifact = vi.fn();
    const mockClearQueryCache = vi.fn();
    const mockClearApiAuth = vi.fn();
    const mockClearCookies = vi.fn();

    vi.doMock('@/lib/supabase/client', () => ({
      createClient: mockCreateClient,
    }));
    vi.doMock('@/services/workspaceService', () => ({
      clearWorkspaceServiceCache: mockClearWorkspaceServiceCache,
    }));
    vi.doMock('@/store/artifactPanelStore', () => ({
      useArtifactPanelStore: {
        getState: () => ({ reset: mockClearArtifact }),
      },
    }));
    vi.doMock('@/lib/query-client', () => ({
      getAppQueryClient: () => ({ clear: mockClearQueryCache }),
    }));
    vi.doMock('@/services/api-client', () => ({
      api: { clearAuth: mockClearApiAuth },
    }));
    vi.doMock('@/lib/supabase/clearAuthCookies', () => ({
      clearSupabaseAuthCookies: mockClearCookies,
    }));

    const { useAuthStore } =
      await vi.importActual<typeof import('@/stores/authStore')>(
        '@/stores/authStore'
      );
    useAuthStore.setState({
      user: { id: 'user-A' } as User,
      organization: { id: 'org-A' } as Organization,
      isAuthenticated: true,
      error: 'old error',
    });

    const result = useAuthStore.getState().invalidateRejectedSession('user-A');

    expect(result).toBeUndefined();
    expect(useAuthStore.getState()).toEqual(
      expect.objectContaining({
        user: null,
        organization: null,
        isAuthenticated: false,
        error: null,
      })
    );
    expect(mockClearWorkspaceServiceCache).toHaveBeenCalledTimes(1);
    expect(mockClearArtifact).toHaveBeenCalledTimes(1);
    expect(mockClearQueryCache).toHaveBeenCalledTimes(1);
    expect(mockClearApiAuth).toHaveBeenCalledTimes(1);
    expect(mockClearCookies).toHaveBeenCalledTimes(1);
    expect(mockCreateClient).not.toHaveBeenCalled();
    expect(mockBrowserSignOut).not.toHaveBeenCalled();
  });

  it('accepts a null expected owner only while no account is present', async () => {
    const mockCreateClient = vi.fn();
    const mockClearWorkspaceServiceCache = vi.fn();
    const mockClearCookies = vi.fn();

    vi.doMock('@/lib/supabase/client', () => ({
      createClient: mockCreateClient,
    }));
    vi.doMock('@/services/workspaceService', () => ({
      clearWorkspaceServiceCache: mockClearWorkspaceServiceCache,
    }));
    vi.doMock('@/lib/supabase/clearAuthCookies', () => ({
      clearSupabaseAuthCookies: mockClearCookies,
    }));

    const { useAuthStore } =
      await vi.importActual<typeof import('@/stores/authStore')>(
        '@/stores/authStore'
      );
    useAuthStore.setState({
      user: null,
      organization: null,
      isAuthenticated: true,
    });

    useAuthStore.getState().invalidateRejectedSession(null);

    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(mockClearWorkspaceServiceCache).toHaveBeenCalledTimes(1);
    expect(mockClearCookies).toHaveBeenCalledTimes(1);
    expect(mockCreateClient).not.toHaveBeenCalled();

    useAuthStore.setState({
      user: { id: 'user-B' } as User,
      organization: { id: 'org-B' } as Organization,
      isAuthenticated: true,
    });
    useAuthStore.getState().invalidateRejectedSession(null);

    expect(useAuthStore.getState()).toEqual(
      expect.objectContaining({
        user: { id: 'user-B' },
        organization: { id: 'org-B' },
        isAuthenticated: true,
      })
    );
    expect(mockClearWorkspaceServiceCache).toHaveBeenCalledTimes(1);
    expect(mockClearCookies).toHaveBeenCalledTimes(1);
  });

  it('does not let user A recovery clear user B or schedule a late SDK wipe', async () => {
    let finishSdkSignOut!: () => void;
    const deferredSdkSignOut = new Promise<void>((resolve) => {
      finishSdkSignOut = resolve;
    });
    const sdkStorage = { ownerUserId: 'user-A' as string | null };
    const mockBrowserSignOut = vi.fn(async () => {
      await deferredSdkSignOut;
      sdkStorage.ownerUserId = null;
    });
    const mockCreateClient = vi.fn(() => ({
      auth: {
        onAuthStateChange: vi.fn(),
        signOut: mockBrowserSignOut,
      },
    }));
    const mockClearWorkspaceServiceCache = vi.fn();
    const mockClearCookies = vi.fn();

    vi.doMock('@/lib/supabase/client', () => ({
      createClient: mockCreateClient,
    }));
    vi.doMock('@/services/workspaceService', () => ({
      clearWorkspaceServiceCache: mockClearWorkspaceServiceCache,
    }));
    vi.doMock('@/lib/supabase/clearAuthCookies', () => ({
      clearSupabaseAuthCookies: mockClearCookies,
    }));

    const { useAuthStore } =
      await vi.importActual<typeof import('@/stores/authStore')>(
        '@/stores/authStore'
      );
    useAuthStore.setState({
      user: { id: 'user-A' } as User,
      organization: { id: 'org-A' } as Organization,
      isAuthenticated: true,
    });

    useAuthStore.getState().invalidateRejectedSession('user-A');
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(mockCreateClient).not.toHaveBeenCalled();
    expect(mockBrowserSignOut).not.toHaveBeenCalled();

    // A different account can be established immediately: recovery launched
    // no deferred SDK cleanup capable of removing B after it settles.
    useAuthStore.setState({
      user: { id: 'user-B' } as User,
      organization: { id: 'org-B' } as Organization,
      isAuthenticated: true,
    });

    // Demonstrate the rejected design with an operation the test actually
    // starts and settles: its late local removal erases B from SDK storage.
    const unsafeLateSignOut = mockBrowserSignOut();
    sdkStorage.ownerUserId = 'user-B';
    finishSdkSignOut();
    await unsafeLateSignOut;
    expect(mockBrowserSignOut).toHaveBeenCalledTimes(1);
    expect(sdkStorage.ownerUserId).toBeNull();

    // A late A-owned callback is also rejected without repeating teardown.
    useAuthStore.getState().invalidateRejectedSession('user-A');

    expect(mockClearWorkspaceServiceCache).toHaveBeenCalledTimes(1);
    expect(mockClearCookies).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState()).toEqual(
      expect.objectContaining({
        user: { id: 'user-B' },
        organization: { id: 'org-B' },
        isAuthenticated: true,
      })
    );
  });
});
