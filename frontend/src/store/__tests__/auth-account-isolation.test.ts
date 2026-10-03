import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import { useAuthStore } from '@/stores/authStore';
import { useChatStore } from '@/store/chat-store';
import { useAgentChatStore } from '@/store/agentChatStore';
import { useProjectStore } from '@/store/projectStore';
import { useProjectChatStore } from '@/store/projectChatStore';
import { usePipelineStore } from '@/store/pipelineStore';
import { useCitationStore } from '@/store/citationStore';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { useResearchEngineStore } from '@/store/research-engine-store';
import { useNotificationStore } from '@/store/notificationStore';
import { setAppQueryClient } from '@/lib/query-client';
import { api } from '@/services/api-client';
import { projectService } from '@/services/projectService';
import { projectChatService } from '@/services/projectChatService';
import { citationService } from '@/services/citationService';
import * as scispaceService from '@/services/scispaceService';

const auth = vi.hoisted(() => ({
  listener: undefined as
    undefined | ((event: string, session: unknown) => void),
  getUser: vi.fn(),
  getSession: vi.fn(),
  signInWithPassword: vi.fn(),
  signOut: vi.fn(),
  signUp: vi.fn(),
  resetPasswordForEmail: vi.fn(),
}));
vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: {
      ...auth,
      onAuthStateChange: (listener: typeof auth.listener) => {
        auth.listener = listener;
      },
    },
  }),
}));
vi.mock('@/lib/supabase/clearAuthCookies', () => ({
  clearSupabaseAuthCookies: vi.fn(),
}));
vi.mock('@/services/api-client', () => ({
  api: { get: vi.fn(), clearAuth: vi.fn() },
}));
vi.mock('@/services/projectService', () => ({
  projectService: {
    listProjects: vi.fn(),
    getProject: vi.fn(),
    listProjectDocuments: vi.fn(),
    listProjectNotes: vi.fn(),
    getProjectBibliography: vi.fn(),
    createNote: vi.fn(),
  },
}));
vi.mock('@/services/projectChatService', () => ({
  projectChatService: {
    listProjectThreads: vi.fn(),
    linkThreadToProject: vi.fn(),
    startChatFromProject: vi.fn(),
  },
}));
vi.mock('@/services/citationService', () => ({
  citationService: { getCitationsForMessage: vi.fn() },
}));
vi.mock('@/services/scispaceService', () => ({ getPipeline: vi.fn() }));

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const profile = (
  id: string
): { user: { id: string }; organization: { id: string } } => ({
  user: { id },
  organization: { id: `org-${id}` },
});
const session = (
  id: string
): { user: { id: string }; access_token: string } => ({
  user: { id },
  access_token: `synthetic-${id}`,
});
function transportIdentity(id: string): void {
  auth.getUser.mockResolvedValue({ data: { user: { id } }, error: null });
  auth.getSession.mockResolvedValue({ data: { session: session(id) } });
  auth.signInWithPassword.mockResolvedValue({
    data: { session: session(id) },
    error: null,
  });
  vi.mocked(api.get).mockResolvedValue(profile(id));
}
let queryClient: QueryClient;
beforeEach(async () => {
  auth.signOut.mockResolvedValue({ error: null });
  await useAuthStore.getState().signOut();
  // Reset test fixtures independently: the regression must fail on the real
  // transition, not depend on the previous test's failed cleanup.
  useChatStore.getState().reset();
  useAgentChatStore.getState().reset();
  useProjectStore.getState().reset();
  useProjectChatStore.getState().reset();
  useCitationStore.getState().clearCitations();
  usePipelineStore.setState({ pipeline: null, error: null, loading: false });
  queryClient = new QueryClient();
  setAppQueryClient(queryClient);
  transportIdentity('A');
  await useAuthStore
    .getState()
    .signIn('a@example.invalid', 'synthetic-password');
});

function seedPrivateState(): void {
  useChatStore.setState({
    currentWorkspaceId: 'A-only-workspace',
    currentConversationId: 'A-only-conversation',
    currentThreadId: 'A-only-thread',
    messages: {
      'A-only-thread': [
        { id: 'A-only-message', content: 'A-only-chat' } as never,
      ],
    },
  });
  useAgentChatStore.setState({
    activeThreadId: 'A-only-thread',
    inputValue: 'A-only-draft',
    messages: [{ id: 'A-only-message', content: 'A-only-agent' } as never],
  });
  useProjectStore.setState({
    projects: [{ id: 'A-only-project' } as never],
    currentProject: { id: 'A-only-project' } as never,
    projectDocuments: [{ document_id: 'A-only-document' } as never],
    projectNotes: [{ id: 'A-only-note' } as never],
    bibliography: { content: 'A-only-bib' } as never,
  });
  useProjectChatStore.setState({
    linkedThreads: {
      'A-only-project': [{ thread_id: 'A-only-thread' } as never],
    },
  });
  usePipelineStore.setState({
    pipeline: { project_id: 'A-only-project' } as never,
  });
  useCitationStore.setState({
    citations: [{ id: 'A-only-citation' } as never],
    citationsByMessage: { 'A-only-message': [] },
  });
  useAgentActivityStore
    .getState()
    .startRun('A-only-thread', 'A-only-run', 'A-only-task');
  useNotificationStore.setState({
    notifications: [{ id: 'A-only-notification' } as never],
  });
  useArtifactPanelStore.getState().openArtifact({
    kind: 'document',
    id: 'A-only-document',
    title: 'A-only-title',
  });
  queryClient.setQueryData(['private'], 'A-only-query');
  localStorage.setItem('default-workspace-id', 'A-only-workspace');
  localStorage.setItem('default-conversation-id', 'A-only-conversation');
}
function assertEmptyPrivateState(): void {
  expect(useChatStore.getState().messages).toEqual({});
  expect(useAgentChatStore.getState()).toMatchObject({
    messages: [],
    inputValue: '',
    activeThreadId: null,
  });
  expect(useProjectStore.getState()).toMatchObject({
    projects: [],
    currentProject: null,
    projectDocuments: [],
    projectNotes: [],
    bibliography: null,
  });
  expect(useProjectChatStore.getState().linkedThreads).toEqual({});
  expect(usePipelineStore.getState().pipeline).toBeNull();
  expect(useCitationStore.getState()).toMatchObject({
    citations: [],
    citationsByMessage: {},
  });
  expect(useAgentActivityStore.getState().runs).toEqual({});
  expect(useNotificationStore.getState().notifications).toEqual([]);
  expect(useArtifactPanelStore.getState().artifact).toBeNull();
  expect(queryClient.getQueryData(['private'])).toBeUndefined();
  for (const key of [
    'chat-storage',
    'default-workspace-id',
    'default-conversation-id',
  ]) {
    expect(localStorage.getItem(key) ?? '').not.toContain('A-only');
  }
}
async function switchToB(): Promise<void> {
  await useAuthStore.getState().signOut();
  transportIdentity('B');
  await useAuthStore
    .getState()
    .signIn('b@example.invalid', 'synthetic-password');
}

describe('private account lifetime', () => {
  it.each(['verification', 'signedOutEvent'])(
    '%s rejects an identity whose profile is still loading',
    async (transition) => {
      await useAuthStore.getState().signOut();
      const pending = deferred<unknown>();
      auth.getUser.mockReturnValueOnce(pending.promise);
      auth.listener!('SIGNED_IN', session('A'));
      const verification = useAuthStore.getState().fetchProfile();
      await vi.waitFor(() => expect(auth.getUser).toHaveBeenCalledOnce());
      expect(useAuthStore.getState().user).toBeNull();
      seedPrivateState();

      if (transition === 'verification') {
        pending.resolve({
          data: { user: null },
          error: new Error('Session expired'),
        });
      } else {
        auth.listener!('SIGNED_OUT', null);
        pending.resolve({ data: { user: { id: 'A' } }, error: null });
      }

      await verification;
      assertEmptyPrivateState();
      expect(useAuthStore.getState().user).toBeNull();
      expect(api.get).toHaveBeenCalledTimes(1);
    }
  );

  it('a signed-out event cancels a login before its identity is known', async () => {
    await useAuthStore.getState().signOut();
    const pending = deferred<unknown>();
    auth.signInWithPassword.mockReturnValueOnce(pending.promise);
    const login = useAuthStore
      .getState()
      .signIn('a@example.invalid', 'synthetic-password')
      .then(
        () => 'accepted',
        (error: Error) => error.name
      );

    auth.listener!('SIGNED_OUT', null);
    pending.resolve({ data: { session: session('A') }, error: null });
    expect(await login).toBe('AbortError');
    expect(useAuthStore.getState().user).toBeNull();
    expect(useAuthStore.getState().isLoading).toBe(false);
  });

  it.each([
    'signOut',
    'rejected',
    'signedOutEvent',
    'signIn',
    'noUser',
    'noToken',
    '401',
    '403',
    'initialize',
  ])(
    '%s clears all private owners and persisted selections',
    async (transition) => {
      seedPrivateState();
      if (transition === 'signOut') await useAuthStore.getState().signOut();
      else if (transition === 'rejected')
        useAuthStore.getState().invalidateRejectedSession('A');
      else if (transition === 'signedOutEvent')
        auth.listener!('SIGNED_OUT', null);
      else if (transition === 'signIn') {
        transportIdentity('B');
        await useAuthStore.getState().signIn('b@example.invalid', 'pw');
      } else {
        if (transition === 'noUser' || transition === 'initialize')
          auth.getUser.mockResolvedValueOnce({
            data: { user: null },
            error: new Error('Rejected'),
          });
        else if (transition === 'noToken')
          auth.getSession.mockResolvedValueOnce({ data: { session: null } });
        else
          vi.mocked(api.get).mockRejectedValueOnce({
            error: { status_code: Number(transition) },
          });
        if (transition === 'initialize')
          await useAuthStore.getState().initialize();
        else await useAuthStore.getState().fetchProfile();
      }
      assertEmptyPrivateState();
    }
  );

  it.each(['SIGNED_IN', 'TOKEN_REFRESHED'])(
    '%s replaces identity before the new profile resolves',
    async (event) => {
      seedPrivateState();
      const pending = deferred<ReturnType<typeof profile>>();
      transportIdentity('B');
      vi.mocked(api.get).mockReturnValueOnce(pending.promise);
      auth.listener!(event, session('B'));
      assertEmptyPrivateState();
      expect(useAuthStore.getState().user).toBeNull();
      await vi.waitFor(() =>
        expect(api.get).toHaveBeenCalledWith(
          '/auth/me',
          expect.objectContaining({
            headers: { Authorization: 'Bearer synthetic-B' },
          })
        )
      );
      pending.resolve(profile('B'));
      await vi.waitFor(() =>
        expect(useAuthStore.getState().user?.id).toBe('B')
      );
    }
  );

  it.each(['SIGNED_IN', 'TOKEN_REFRESHED'])(
    'same-user %s preserves current work',
    (event) => {
      seedPrivateState();
      const controller = new AbortController();
      useAgentChatStore.setState({
        _abortController: controller,
        isStreaming: true,
      });
      auth.listener!(event, session('A'));
      expect(controller.signal.aborted).toBe(false);
      expect(useAgentChatStore.getState().inputValue).toBe('A-only-draft');
      expect(useProjectStore.getState().currentProject?.id).toBe(
        'A-only-project'
      );
      expect(queryClient.getQueryData(['private'])).toBe('A-only-query');
      expect(localStorage.getItem('chat-storage')).toContain('A-only-thread');
    }
  );

  it.each(['success', 'rejection'])(
    'ignores an old profile %s after B signs in',
    async (outcome) => {
      const pending = deferred<ReturnType<typeof profile>>();
      vi.mocked(api.get).mockReturnValueOnce(pending.promise);
      const old = useAuthStore.getState().fetchProfile();
      await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
      await switchToB();
      useProjectStore.setState({
        currentProject: { id: 'B-project' } as never,
      });
      if (outcome === 'success') pending.resolve(profile('A'));
      else pending.reject({ error: { status_code: 401 } });
      await old;
      expect(useAuthStore.getState().user?.id).toBe('B');
      expect(useProjectStore.getState().currentProject?.id).toBe('B-project');
    }
  );

  it('does not resurrect a signed-out user from a pending password login', async () => {
    const pending = deferred<ReturnType<typeof profile>>();
    vi.mocked(api.get).mockReturnValueOnce(pending.promise);
    const login = useAuthStore.getState().signIn('a@example.invalid', 'pw');
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    await useAuthStore.getState().signOut();
    pending.resolve(profile('A'));
    await expect(login).rejects.toMatchObject({ name: 'AbortError' });
    expect(useAuthStore.getState().user).toBeNull();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it('a late rejected-session notification for A preserves B', async () => {
    await switchToB();
    useProjectStore.setState({ currentProject: { id: 'B-project' } as never });
    useAuthStore.getState().invalidateRejectedSession('A');
    expect(useAuthStore.getState().user?.id).toBe('B');
    expect(useProjectStore.getState().currentProject?.id).toBe('B-project');
  });

  it.each(
    [
      'projects',
      'project',
      'documents',
      'notes',
      'bibliography',
      'createNote',
      'linkedThreads',
      'link',
      'startChat',
      'pipeline',
      'citations',
    ].flatMap((kind) =>
      ['success', 'rejection'].map((outcome) => ({ kind, outcome }))
    )
  )(
    'discards deferred A $kind $outcome and follow-up work after B starts',
    async ({ kind, outcome }) => {
      const pending = deferred<never>();
      const operations = {
        projects: () => {
          vi.mocked(projectService.listProjects).mockReturnValueOnce(
            pending.promise
          );
          return useProjectStore.getState().fetchProjects();
        },
        project: () => {
          vi.mocked(projectService.getProject).mockReturnValueOnce(
            pending.promise
          );
          return useProjectStore.getState().fetchProject('shared-id');
        },
        documents: () => {
          vi.mocked(projectService.listProjectDocuments).mockReturnValueOnce(
            pending.promise
          );
          return useProjectStore.getState().fetchProjectDocuments('shared-id');
        },
        notes: () => {
          vi.mocked(projectService.listProjectNotes).mockReturnValueOnce(
            pending.promise
          );
          return useProjectStore.getState().fetchProjectNotes('shared-id');
        },
        bibliography: () => {
          vi.mocked(projectService.getProjectBibliography).mockReturnValueOnce(
            pending.promise
          );
          return useProjectStore.getState().fetchBibliography('shared-id');
        },
        createNote: () => {
          vi.mocked(projectService.createNote).mockReturnValueOnce(
            pending.promise
          );
          return useProjectStore
            .getState()
            .createNote('shared-id', { title: 'A-only', content: 'A-only' });
        },
        linkedThreads: () => {
          vi.mocked(projectChatService.listProjectThreads).mockReturnValueOnce(
            pending.promise
          );
          return useProjectChatStore
            .getState()
            .fetchProjectThreads('shared-id');
        },
        link: () => {
          vi.mocked(projectChatService.linkThreadToProject).mockReturnValueOnce(
            pending.promise
          );
          return useProjectChatStore
            .getState()
            .linkThreadToProject('shared-id', { thread_id: 'A-only-thread' });
        },
        startChat: () => {
          vi.mocked(
            projectChatService.startChatFromProject
          ).mockReturnValueOnce(pending.promise);
          return useProjectChatStore
            .getState()
            .startChatFromProject('shared-id', {} as never);
        },
        pipeline: () => {
          vi.mocked(scispaceService.getPipeline).mockReturnValueOnce(
            pending.promise
          );
          return usePipelineStore.getState().fetchPipeline('shared-id');
        },
        citations: () => {
          vi.mocked(citationService.getCitationsForMessage).mockReturnValueOnce(
            pending.promise
          );
          return useCitationStore
            .getState()
            .fetchCitationsForMessage('A-only-message');
        },
      };
      const old = operations[kind as keyof typeof operations]();
      // A rejected stale mutation is allowed, but must not publish A's result.
      const settled = old.catch(() => undefined);
      await switchToB();
      useProjectStore.setState({
        currentProject: { id: 'B-project' } as never,
        loading: true,
      });
      const snapshots = [
        useProjectStore.getState(),
        useProjectChatStore.getState(),
        usePipelineStore.getState(),
        useCitationStore.getState(),
      ];
      const payloads: Record<string, unknown> = {
        projects: { projects: [{ id: 'A-only-project' }], total: 1 },
        project: { id: 'A-only-project' },
        documents: { documents: [{ document_id: 'A-only-document' }] },
        notes: { notes: [{ id: 'A-only-note' }] },
        bibliography: { content: 'A-only-bib' },
        createNote: { id: 'A-only-note' },
        linkedThreads: { threads: [{ thread_id: 'A-only-thread' }] },
        link: { thread_id: 'A-only-thread' },
        startChat: { thread_id: 'A-only-thread' },
        pipeline: { project_id: 'A-only-project' },
        citations: [{ id: 'A-only-citation' }],
      };
      const listCalls = vi.mocked(projectChatService.listProjectThreads).mock
        .calls.length;
      if (outcome === 'success') pending.resolve(payloads[kind] as never);
      else pending.reject(new Error('A-only-error'));
      await settled;
      expect([
        useProjectStore.getState(),
        useProjectChatStore.getState(),
        usePipelineStore.getState(),
        useCitationStore.getState(),
      ]).toEqual(snapshots);
      expect(projectChatService.listProjectThreads).toHaveBeenCalledTimes(
        listCalls
      );
    }
  );
  it('clears the research-engine project and run cache', async () => {
    useResearchEngineStore.setState({
      projects: [{ id: 'A-only-project' } as never],
      activeProject: { id: 'A-only-project' } as never,
      activeRunId: 'A-only-run',
      activeRun: { id: 'A-only-run' } as never,
      runSteps: [{ output: { text: 'A-only-output' } } as never],
      pendingReview: { title: 'A-only-review' } as never,
    });
    await switchToB();
    expect(useResearchEngineStore.getState()).toMatchObject({
      projects: [],
      activeProject: null,
      activeRunId: null,
      activeRun: null,
      runSteps: [],
      pendingReview: null,
    });
  });

  it('keeps B project deduplication when A finishes with the same project ID', async () => {
    const a = deferred<never>();
    const b = deferred<never>();
    vi.mocked(projectService.getProject)
      .mockReturnValueOnce(a.promise)
      .mockReturnValueOnce(b.promise);
    const old = useProjectStore.getState().fetchProject('shared-id');
    await switchToB();
    const current = useProjectStore.getState().fetchProject('shared-id');
    a.resolve({ id: 'shared-id', title: 'A-only' } as never);
    await old;
    const joined = useProjectStore.getState().fetchProject('shared-id');
    expect(projectService.getProject).toHaveBeenCalledTimes(2);
    expect(useProjectStore.getState().currentProject).toBeNull();
    b.resolve({ id: 'shared-id', title: 'B-only' } as never);
    await Promise.all([current, joined]);
    expect(useProjectStore.getState().currentProject).toMatchObject({
      title: 'B-only',
    });
  });

  it('keeps B profile deduplication when A finishes during B verification', async () => {
    const a = deferred<ReturnType<typeof profile>>();
    const b = deferred<ReturnType<typeof profile>>();
    vi.mocked(api.get).mockReturnValueOnce(a.promise);
    const old = useAuthStore.getState().fetchProfile();
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    transportIdentity('B');
    vi.mocked(api.get).mockReturnValueOnce(b.promise);
    auth.listener!('SIGNED_IN', session('B'));
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(3));
    useAuthStore.getState().invalidateRejectedSession('A');
    a.resolve(profile('A'));
    await old;
    const joined = useAuthStore.getState().fetchProfile();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(api.get).toHaveBeenCalledTimes(3);
    b.resolve(profile('B'));
    await joined;
    expect(useAuthStore.getState().user?.id).toBe('B');
  });
});

it.each(['signup', 'password'] as const)(
  'ignores old %s success and errors after B signs in',
  async (kind) => {
    for (const rejected of [false, true]) {
      const pending = deferred<never>();
      if (kind === 'signup') auth.signUp.mockReturnValueOnce(pending.promise);
      else auth.resetPasswordForEmail.mockReturnValueOnce(pending.promise);
      const request =
        kind === 'signup'
          ? useAuthStore.getState().signUp({
              email: 'A-private@example.invalid',
              password: 'synthetic',
              first_name: 'A',
              last_name: 'A',
            })
          : useAuthStore.getState().resetPassword('A-private@example.invalid');
      const settled = request.then(
        () => 'success',
        (error: Error) => error.name
      );
      transportIdentity('B');
      await useAuthStore.getState().signIn('B@example.invalid', 'synthetic');
      useAuthStore.setState({ isLoading: true, error: 'B current error' });
      if (rejected) pending.reject(new Error('A private error'));
      else
        pending.resolve({
          data: { session: null, user: { identities: [{}] } },
          error: null,
        } as never);
      expect(await settled).toBe('AbortError');
      expect(useAuthStore.getState()).toMatchObject({
        user: { id: 'B' },
        isLoading: true,
        error: 'B current error',
        pendingConfirmationEmail: null,
      });
    }
  }
);

it("accepts signup's own SIGNED_IN event and waits for its profile", async () => {
  await useAuthStore.getState().signOut();
  transportIdentity('new-user');
  const profileRequest = deferred<ReturnType<typeof profile>>();
  vi.mocked(api.get).mockReturnValueOnce(profileRequest.promise);
  auth.signUp.mockImplementationOnce(async () => {
    auth.listener?.('SIGNED_IN', {
      user: { id: 'new-user' },
      access_token: 'signup-token',
    });
    return {
      data: {
        session: { user: { id: 'new-user' }, access_token: 'signup-token' },
      },
      error: null,
    };
  });
  let done = false;
  const signup = useAuthStore
    .getState()
    .signUp({
      email: 'new@example.invalid',
      password: 'pw',
      first_name: 'New',
      last_name: 'User',
    })
    .then((result) => {
      done = true;
      return result;
    });
  await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  expect(done).toBe(false);
  profileRequest.resolve(profile('new-user'));
  expect(await signup).toEqual({ requiresEmailConfirmation: false });
  expect(useAuthStore.getState().user?.id).toBe('new-user');
});
