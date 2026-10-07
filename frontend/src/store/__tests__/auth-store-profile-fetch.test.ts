import { beforeEach, describe, expect, it, vi } from 'vitest';
import { APIErrorClass } from '@/types/api';

type AuthChangeHandler = (event: string, session: unknown) => void;

const SESSION = {
  access_token: 'access-token-1',
  user: { id: 'user-1' },
};

const PROFILE = {
  user: { id: 'user-1', email: 'ada@example.com' },
  organization: { id: 'org-1' },
};

interface Harness {
  useAuthStore: typeof import('@/stores/authStore').useAuthStore;
  apiGet: ReturnType<typeof vi.fn>;
  getUser: ReturnType<typeof vi.fn>;
  emit: (event: string, session: unknown) => void;
  signInWithPassword: ReturnType<typeof vi.fn>;
  getSession: ReturnType<typeof vi.fn>;
}

async function loadStore(
  options: { emitSignedInDuringSignIn?: boolean } = {}
): Promise<Harness> {
  const handlers: AuthChangeHandler[] = [];
  const emit = (event: string, session: unknown) =>
    handlers.forEach((handler) => handler(event, session));

  const apiGet = vi.fn().mockResolvedValue(PROFILE);
  const getUser = vi
    .fn()
    .mockResolvedValue({ data: { user: SESSION.user }, error: null });
  const getSession = vi.fn().mockResolvedValue({ data: { session: SESSION } });
  const signInWithPassword = vi.fn().mockImplementation(async () => {
    if (options.emitSignedInDuringSignIn) {
      // Mirrors supabase-js: SIGNED_IN is broadcast as part of the sign-in call.
      emit('SIGNED_IN', SESSION);
    }
    return { data: { session: SESSION }, error: null };
  });

  vi.doMock('@/lib/supabase/client', () => ({
    createClient: () => ({
      auth: {
        onAuthStateChange: (handler: AuthChangeHandler) => {
          handlers.push(handler);
          return { data: { subscription: { unsubscribe: vi.fn() } } };
        },
        signInWithPassword,
        signOut: vi.fn().mockResolvedValue({ error: null }),
        getUser,
        getSession,
      },
    }),
  }));

  vi.doMock('@/services/workspaceService', () => ({
    clearWorkspaceServiceCache: vi.fn(),
  }));

  vi.doMock('@/services/api-client', () => ({
    api: { get: apiGet, post: vi.fn(), clearAuth: vi.fn() },
  }));

  const { useAuthStore } =
    await vi.importActual<typeof import('@/stores/authStore')>(
      '@/stores/authStore'
    );

  return {
    useAuthStore,
    apiGet,
    getUser,
    emit,
    signInWithPassword,
    getSession,
  };
}

describe('useAuthStore profile fetching', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  it('does not issue a second /auth/me when SIGNED_IN fires during signIn', async () => {
    const { useAuthStore, apiGet } = await loadStore({
      emitSignedInDuringSignIn: true,
    });

    await useAuthStore.getState().signIn('ada@example.com', 'hunter2hunter2');
    // Let any listener-scheduled work run.
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(apiGet).toHaveBeenCalledTimes(1);
    expect(apiGet).toHaveBeenCalledWith('/auth/me', expect.anything());
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });

  it('collapses concurrent fetchProfile calls onto one /auth/me', async () => {
    const { useAuthStore, apiGet } = await loadStore();

    await Promise.all([
      useAuthStore.getState().fetchProfile(),
      useAuthStore.getState().fetchProfile(),
    ]);

    expect(apiGet).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });

  it('keeps an established session when the profile fetch fails transiently', async () => {
    const { useAuthStore, apiGet } = await loadStore();

    useAuthStore.setState({
      user: { id: 'user-1' } as never,
      organization: { id: 'org-1' } as never,
      isAuthenticated: true,
      isLoading: false,
      error: null,
    });

    apiGet.mockRejectedValueOnce(new Error('Network request failed'));
    await useAuthStore.getState().fetchProfile();

    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(true);
    expect(state.user).toEqual({ id: 'user-1' });
    expect(state.isLoading).toBe(false);
    expect(state.error).toBe('Network request failed');
  });

  it('still tears down the session when the token is rejected (401)', async () => {
    const { useAuthStore, apiGet } = await loadStore();

    useAuthStore.setState({
      user: { id: 'user-1' } as never,
      organization: { id: 'org-1' } as never,
      isAuthenticated: true,
      isLoading: false,
      error: null,
    });

    apiGet.mockRejectedValueOnce(
      new APIErrorClass({
        message: 'Invalid token',
        status_code: 401,
        type: 'auth_error',
      })
    );
    await useAuthStore.getState().fetchProfile();

    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(false);
    expect(state.user).toBeNull();
    expect(state.error).toBe('Invalid token');
  });

  it('still tears down the session when Supabase cannot verify the user', async () => {
    const { useAuthStore, getUser } = await loadStore();

    useAuthStore.setState({
      user: { id: 'user-1' } as never,
      isAuthenticated: true,
      isLoading: false,
    });

    getUser.mockResolvedValueOnce({
      data: { user: null },
      error: { message: 'Auth session missing' },
    });
    await useAuthStore.getState().fetchProfile();

    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().user).toBeNull();
  });
});

describe('useAuthStore profile responses across account transitions', () => {
  // Real auth + chat stores; only Supabase and /auth/me are faked. User A's
  // profile request is held open while the account changes underneath it.
  const PROFILE_A = { user: { id: 'user-A' }, organization: { id: 'org-A' } };
  const PROFILE_B = { user: { id: 'user-B' }, organization: { id: 'org-B' } };

  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  interface Deferred<T> {
    promise: Promise<T>;
    resolve: (value: T) => void;
    reject: (reason: unknown) => void;
  }

  function deferred<T>(): Deferred<T> {
    let resolve!: (value: T) => void;
    let reject!: (reason: unknown) => void;
    const promise = new Promise<T>((done, fail) => {
      resolve = done;
      reject = fail;
    });
    return { promise, resolve, reject };
  }

  async function startWithUserAProfileInFlight(): Promise<
    Harness & {
      useChatStore: typeof import('@/store/chat-store').useChatStore;
      profileA: Deferred<typeof PROFILE_A>;
      pendingA: Promise<void>;
    }
  > {
    const harness = await loadStore();
    const { useChatStore } = await import('@/store/chat-store');
    harness.useAuthStore.setState({
      user: { id: 'user-A' } as never,
      organization: { id: 'org-A' } as never,
      isAuthenticated: true,
      isLoading: false,
    });
    const profileA = deferred<typeof PROFILE_A>();
    harness.apiGet
      .mockReturnValueOnce(profileA.promise)
      .mockResolvedValue(PROFILE_B);
    const pendingA = harness.useAuthStore.getState().fetchProfile();
    await vi.waitFor(() => expect(harness.apiGet).toHaveBeenCalledTimes(1));
    return { ...harness, useChatStore, profileA, pendingA };
  }

  function seedUserBChat(
    useChatStore: typeof import('@/store/chat-store').useChatStore
  ): void {
    useChatStore.setState({
      currentThreadId: 'user-B-thread',
      messages: { 'user-B-thread': [{ id: 'user-B-message' } as never] },
    });
  }

  it.each(['signOutThenSignIn', 'switchAccount'] as const)(
    'discards user A’s late /auth/me after %s to user B',
    async (transition) => {
      const {
        useAuthStore,
        useChatStore,
        apiGet,
        getUser,
        getSession,
        profileA,
        pendingA,
      } = await startWithUserAProfileInFlight();

      if (transition === 'signOutThenSignIn') {
        await useAuthStore.getState().signOut();
      }
      await useAuthStore
        .getState()
        .signIn('user-b@example.invalid', 'synthetic-password');
      expect(useAuthStore.getState().user?.id).toBe('user-B');
      seedUserBChat(useChatStore);
      // The verified SDK identity must follow the completed account switch.
      // Leaving its default user-1 would correctly start another transition
      // when B explicitly verifies the session below.
      getUser.mockResolvedValue({
        data: { user: PROFILE_B.user },
        error: null,
      });
      getSession.mockResolvedValue({
        data: { session: { ...SESSION, user: PROFILE_B.user } },
      });

      // B's own profile read starts and owns the in-flight slot: it is not
      // joined onto A's abandoned request.
      const profileB = deferred<typeof PROFILE_B>();
      apiGet.mockReturnValueOnce(profileB.promise);
      const pendingB = useAuthStore.getState().fetchProfile();
      await vi.waitFor(() => expect(apiGet).toHaveBeenCalledTimes(3));

      // A's response lands last, out of order.
      profileA.resolve(PROFILE_A);
      await pendingA;

      expect(useAuthStore.getState()).toMatchObject({
        user: { id: 'user-B' },
        organization: { id: 'org-B' },
        isAuthenticated: true,
      });
      expect(useChatStore.getState().currentThreadId).toBe('user-B-thread');
      expect(Object.keys(useChatStore.getState().messages)).toEqual([
        'user-B-thread',
      ]);

      // A settling does not release B's slot: a concurrent caller joins B.
      const joinedB = useAuthStore.getState().fetchProfile();
      profileB.resolve(PROFILE_B);
      await Promise.all([pendingB, joinedB]);
      expect(apiGet).toHaveBeenCalledTimes(3);
      expect(useAuthStore.getState().user?.id).toBe('user-B');
    }
  );

  it('does not re-authenticate user A when A’s /auth/me lands after sign-out', async () => {
    const { useAuthStore, profileA, pendingA } =
      await startWithUserAProfileInFlight();

    await useAuthStore.getState().signOut();
    profileA.resolve(PROFILE_A);
    await pendingA;

    expect(useAuthStore.getState()).toMatchObject({
      user: null,
      organization: null,
      isAuthenticated: false,
    });
  });

  it('ignores user A’s late 401 after the account switched to user B', async () => {
    const { useAuthStore, useChatStore, profileA, pendingA } =
      await startWithUserAProfileInFlight();

    await useAuthStore
      .getState()
      .signIn('user-b@example.invalid', 'synthetic-password');
    seedUserBChat(useChatStore);

    profileA.reject(
      new APIErrorClass({
        message: 'Invalid token',
        status_code: 401,
        type: 'auth_error',
      })
    );
    await pendingA;

    expect(useAuthStore.getState()).toMatchObject({
      user: { id: 'user-B' },
      isAuthenticated: true,
      error: null,
    });
    expect(useChatStore.getState().currentThreadId).toBe('user-B-thread');
  });

  it('keeps the later of two concurrent sign-ins when the earlier /auth/me lands last', async () => {
    const harness = await loadStore();
    const { useAuthStore, apiGet } = harness;
    const { useChatStore } = await import('@/store/chat-store');
    useAuthStore.setState({ user: null, isAuthenticated: false });
    const profileA = deferred<typeof PROFILE_A>();
    apiGet.mockReturnValueOnce(profileA.promise).mockResolvedValue(PROFILE_B);

    const signInA = useAuthStore
      .getState()
      .signIn('user-a@example.invalid', 'synthetic-password');
    await vi.waitFor(() => expect(apiGet).toHaveBeenCalledTimes(1));
    await useAuthStore
      .getState()
      .signIn('user-b@example.invalid', 'synthetic-password');
    seedUserBChat(useChatStore);

    profileA.resolve(PROFILE_A);
    // The superseded sign-in is not a success for its caller.
    await expect(signInA).rejects.toMatchObject({
      name: 'SignInSupersededError',
    });

    expect(useAuthStore.getState()).toMatchObject({
      user: { id: 'user-B' },
      organization: { id: 'org-B' },
      isAuthenticated: true,
      isLoading: false,
    });
    expect(useChatStore.getState().currentThreadId).toBe('user-B-thread');
  });

  it.each(['getUser', 'getSession'] as const)(
    'ignores user A’s late signed-out %s answer after the switch to user B',
    async (stage) => {
      const harness = await loadStore();
      const { useAuthStore, getUser, getSession } = harness;
      const { useChatStore } = await import('@/store/chat-store');
      useAuthStore.setState({
        user: { id: 'user-A' } as never,
        isAuthenticated: true,
        isLoading: false,
      });
      harness.apiGet.mockResolvedValue(PROFILE_B);
      const lateAnswer = deferred<unknown>();
      if (stage === 'getUser') getUser.mockReturnValueOnce(lateAnswer.promise);
      else getSession.mockReturnValueOnce(lateAnswer.promise);

      const pendingA = useAuthStore.getState().fetchProfile();
      await vi.waitFor(() =>
        expect(stage === 'getUser' ? getUser : getSession).toHaveBeenCalled()
      );
      await useAuthStore
        .getState()
        .signIn('user-b@example.invalid', 'synthetic-password');
      seedUserBChat(useChatStore);

      lateAnswer.resolve(
        stage === 'getUser'
          ? { data: { user: null }, error: { message: 'Auth session missing' } }
          : { data: { session: null } }
      );
      await pendingA;

      expect(useAuthStore.getState()).toMatchObject({
        user: { id: 'user-B' },
        isAuthenticated: true,
      });
      expect(useChatStore.getState().currentThreadId).toBe('user-B-thread');
    }
  );

  it('rejects a sign-in interrupted by sign-out and clears the loading flag', async () => {
    const { useAuthStore, apiGet } = await loadStore();
    useAuthStore.setState({ user: null, isAuthenticated: false });
    const profileA = deferred<typeof PROFILE_A>();
    apiGet.mockReturnValueOnce(profileA.promise);

    const signIn = useAuthStore
      .getState()
      .signIn('user-a@example.invalid', 'synthetic-password');
    await vi.waitFor(() => expect(apiGet).toHaveBeenCalledTimes(1));
    await useAuthStore.getState().signOut();
    profileA.resolve(PROFILE_A);

    await expect(signIn).rejects.toMatchObject({
      name: 'SignInSupersededError',
    });
    expect(useAuthStore.getState()).toMatchObject({
      user: null,
      isAuthenticated: false,
      isLoading: false,
      error: null,
    });
  });

  it('does not write a superseded sign-in’s failure onto the newer session', async () => {
    const { useAuthStore, apiGet } = await loadStore();
    useAuthStore.setState({ user: null, isAuthenticated: false });
    const profileA = deferred<typeof PROFILE_A>();
    apiGet.mockReturnValueOnce(profileA.promise);

    const signIn = useAuthStore
      .getState()
      .signIn('user-a@example.invalid', 'synthetic-password');
    await vi.waitFor(() => expect(apiGet).toHaveBeenCalledTimes(1));
    await useAuthStore.getState().signOut();
    profileA.reject(new Error('Synthetic network failure'));

    await expect(signIn).rejects.toThrow('Synthetic network failure');
    expect(useAuthStore.getState()).toMatchObject({
      user: null,
      isLoading: false,
      error: null,
    });
  });

  it('clears the loading flag when a fenced-out profile read was the last loader', async () => {
    const { useAuthStore, apiGet } = await loadStore();
    // Cold start: initialize() is loading the profile when the user signs out.
    useAuthStore.setState({
      user: null,
      isAuthenticated: false,
      isLoading: true,
    });
    const profileA = deferred<typeof PROFILE_A>();
    apiGet.mockReturnValueOnce(profileA.promise);

    const pending = useAuthStore.getState().fetchProfile();
    await vi.waitFor(() => expect(apiGet).toHaveBeenCalledTimes(1));
    await useAuthStore.getState().signOut();
    profileA.resolve(PROFILE_A);
    await pending;

    expect(useAuthStore.getState()).toMatchObject({
      user: null,
      isAuthenticated: false,
      isLoading: false,
    });
  });

  it('clears the loading flag on a SIGNED_OUT event', async () => {
    const { useAuthStore, emit } = await loadStore();
    // Registers the Supabase listener.
    await useAuthStore.getState().fetchProfile();
    useAuthStore.setState({ isLoading: true });

    emit('SIGNED_OUT', null);

    expect(useAuthStore.getState()).toMatchObject({
      user: null,
      isAuthenticated: false,
      isLoading: false,
    });
  });

  it('publishes the profile when the same user signs out and back in', async () => {
    const { useAuthStore, apiGet } = await loadStore();
    apiGet.mockResolvedValue(PROFILE);

    await useAuthStore
      .getState()
      .signIn('ada@example.com', 'synthetic-password');
    await useAuthStore.getState().signOut();
    expect(useAuthStore.getState().user).toBeNull();

    await useAuthStore
      .getState()
      .signIn('ada@example.com', 'synthetic-password');
    expect(useAuthStore.getState()).toMatchObject({
      user: { id: 'user-1' },
      isAuthenticated: true,
      isLoading: false,
    });
    // A later profile refresh in the new generation also publishes.
    useAuthStore.setState({ organization: null });
    await useAuthStore.getState().fetchProfile();
    expect(useAuthStore.getState().organization).toEqual({ id: 'org-1' });
  });
});
