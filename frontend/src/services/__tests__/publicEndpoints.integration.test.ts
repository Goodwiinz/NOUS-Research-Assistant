import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';

type TestGlobal = typeof globalThis & { __TEST_BROWSER_ORIGIN__?: string };

// Mutation proof, 2026-10-16: replacing the hook's lazy useState owner with
// a render-created client fails "keeps one WebSocket" with two sockets.
// Removing scheduleReconnect's retry-budget guard fails "stops reconnecting"
// with four sockets instead of three. Each source variant was restored
// byte-identically and its focused test passed again. Commands:
// pnpm --dir frontend test src/services/__tests__/publicEndpoints.integration.test.ts -t "keeps one WebSocket"
// pnpm --dir frontend test src/services/__tests__/publicEndpoints.integration.test.ts -t "stops reconnecting"
const originalOrigin = (globalThis as TestGlobal).__TEST_BROWSER_ORIGIN__;

describe('default API callers use public endpoint policy', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv('NEXT_PUBLIC_API_URL', 'http://localhost:8000');
    vi.stubEnv('NEXT_PUBLIC_API_BASE_URL', 'http://localhost:8000/api/v1');
    vi.stubEnv('NEXT_PUBLIC_WS_URL', 'ws://localhost:8000');
    vi.stubEnv('NEXT_PUBLIC_WEBSOCKET_URL', '');
    (globalThis as TestGlobal).__TEST_BROWSER_ORIGIN__ =
      'https://dev-app.example';
    vi.doMock('@/lib/supabase/client', () => ({
      createClient: () => ({
        auth: {
          getSession: vi.fn().mockResolvedValue({
            data: { session: { access_token: 'test-session' } },
          }),
        },
      }),
    }));
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.doUnmock('@/lib/supabase/client');
    vi.useRealTimers();
    (globalThis as TestGlobal).__TEST_BROWSER_ORIGIN__ = originalOrigin;
  });

  it('routes authenticated profile requests to the deployed API despite baked local config', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({ id: 'test-user' }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const { APIClient } = await import('../api-client');

    await new APIClient().get('/auth/me');

    expect(fetchMock).toHaveBeenCalledWith(
      'https://dev-api.example/api/v1/auth/me',
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer test-session',
        }),
      })
    );
  });

  it('routes the real analytics service through the same configured API', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({ success: true, data: [] }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const { MetricsApiService } = await import('../analytics/analyticsApi');

    expect(await new MetricsApiService().getMetrics()).toEqual([]);
    expect(fetchMock).toHaveBeenCalledWith(
      'https://dev-api.example/api/v1/analytics/metrics',
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer test-session',
        }),
      })
    );
  });

  it('makes the analytics factory exports available as actual local bindings', async () => {
    const { analyticsServiceFactory, getServiceStatus } =
      await import('../analytics');
    expect(analyticsServiceFactory.getMetricsApi()).toBeDefined();
    expect(getServiceStatus().api.baseUrl).toBe('https://dev-api.example');
    expect(getServiceStatus().websocket.connecting).toBe(false);
  });

  it('initializes every analytics service without persisting the supplied token', async () => {
    const services = await import('../analytics');
    localStorage.removeItem('authToken');
    const initialized = services.initializeAnalyticsServices({
      authToken: 'never-persist-this-token',
    });
    expect(initialized).toEqual({
      metrics: services.metricsApi,
      graph: services.graphApi,
      dashboard: services.dashboardApi,
      reports: services.reportApi,
      alerts: services.alertApi,
      export: services.exportApi,
      websocket: services.analyticsWebSocket,
    });
    expect(services.initializeAnalyticsServices()).toEqual(initialized);
    expect(localStorage.getItem('authToken')).toBeNull();
    services.analyticsServiceFactory.reset();
    expect(services.analyticsServiceFactory.getMetricsApi()).toBe(
      services.metricsApi
    );
  });

  const reads: Array<{
    name: string;
    path: string;
    query?: Record<string, string>;
    request: (services: typeof import('../analytics')) => Promise<unknown>;
  }> = [
    {
      name: 'metric detail',
      path: '/metrics/metric-1',
      request: (s) => s.metricsApi.getMetric('metric-1'),
    },
    {
      name: 'metric time series',
      path: '/metrics/metric-1/timeseries',
      query: {
        granularity: 'hour',
        timeRange: JSON.stringify({ start: '2026-10-01', end: '2026-10-16' }),
      },
      request: (s) =>
        s.metricsApi.getTimeSeriesData(
          'metric-1',
          { start: '2026-10-01', end: '2026-10-16' },
          'hour'
        ),
    },
    {
      name: 'metric aggregation',
      path: '/metrics/aggregate',
      query: {
        metricIds: JSON.stringify(['one', 'two']),
        aggregation: 'average',
      },
      request: (s) =>
        s.metricsApi.getAggregatedMetrics(['one', 'two'], 'average'),
    },
    {
      name: 'filtered graph',
      path: '/graph',
      query: {
        nodeTypes: JSON.stringify(['paper']),
        searchQuery: 'clinical RAG',
      },
      request: (s) =>
        s.graphApi.getGraphData({
          nodeTypes: ['paper'],
          searchQuery: 'clinical RAG',
        }),
    },
    {
      name: 'graph node',
      path: '/graph/nodes/node-1',
      request: (s) => s.graphApi.getNode('node-1'),
    },
    {
      name: 'graph neighbors',
      path: '/graph/nodes/node-1/neighbors',
      query: { depth: '2' },
      request: (s) => s.graphApi.getNodeNeighbors('node-1', 2),
    },
    {
      name: 'graph path',
      path: '/graph/shortest-path',
      query: { source: 'one', target: 'two' },
      request: (s) => s.graphApi.findShortestPath('one', 'two'),
    },
    {
      name: 'dashboards',
      path: '/dashboards',
      request: (s) => s.dashboardApi.getDashboards(),
    },
    {
      name: 'dashboard detail',
      path: '/dashboards/dashboard-1',
      request: (s) => s.dashboardApi.getDashboard('dashboard-1'),
    },
    {
      name: 'reports',
      path: '/reports',
      request: (s) => s.reportApi.getReports(),
    },
    {
      name: 'report detail',
      path: '/reports/report-1',
      request: (s) => s.reportApi.getReport('report-1'),
    },
    {
      name: 'alert rules',
      path: '/alerts/rules',
      request: (s) => s.alertApi.getAlertRules(),
    },
    {
      name: 'bounded active alerts',
      path: '/alerts',
      query: { limit: '5' },
      request: (s) => s.alertApi.getActiveAlerts(5),
    },
    {
      name: 'export status',
      path: '/export/export-1/status',
      request: (s) => s.exportApi.getExportStatus('export-1'),
    },
  ];

  it.each(reads)(
    'routes authenticated $name reads through the shared API',
    async ({ path, query, request }) => {
      const data = { receipt: 'from-the-configured-api' };
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({ success: true, data }),
      });
      vi.stubGlobal('fetch', fetchMock);
      expect(await request(await import('../analytics'))).toEqual(data);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [target, options] = fetchMock.mock.calls[0];
      const url = new URL(target);
      expect(url.origin).toBe('https://dev-api.example');
      expect(url.pathname).toBe(`/api/v1/analytics${path}`);
      expect(Object.fromEntries(url.searchParams)).toEqual(query ?? {});
      expect(options.headers.Authorization).toBe('Bearer test-session');
    }
  );

  it.each([true, false])(
    'reports API health from the response success flag (%s)',
    async (success) => {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({
          ok: true,
          headers: new Headers({ 'content-type': 'application/json' }),
          json: async () => ({
            success,
            data: [],
            message: 'Analytics unavailable',
          }),
        })
      );
      const { performHealthCheck } = await import('../analytics');
      const result = await performHealthCheck();
      expect(result.healthy).toBe(success);
      expect(result.services.api.status).toBe(
        success ? 'healthy' : 'unhealthy'
      );
      if (!success)
        expect(result.services.api).toMatchObject({
          error: 'Analytics unavailable',
        });
    }
  );

  it('keeps one WebSocket client per mount when configuration is passed inline', async () => {
    const urls: string[] = [];
    class Socket {
      static OPEN = 1;
      static CONNECTING = 0;
      readyState = 0;
      constructor(url: string) {
        urls.push(url);
      }
      close(): void {
        this.readyState = 3;
      }
    }
    vi.stubGlobal('WebSocket', Socket);
    const { useWebSocket } = await import('../analytics/websocketService');
    const hook = renderHook(() =>
      useWebSocket({ url: 'wss://api.example/ws' })
    );
    try {
      hook.rerender();
      expect(urls).toEqual(['wss://api.example/ws']);
    } finally {
      hook.unmount();
    }
  });

  it('stops reconnecting once the configured retry budget is exhausted', async () => {
    vi.useFakeTimers();
    const sockets: Socket[] = [];
    class Socket {
      static OPEN = 1;
      static CONNECTING = 0;
      readyState = 0;
      onclose: ((event: { code: number }) => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(_url: string) {
        sockets.push(this);
      }
      close(): void {
        this.readyState = 3;
      }
    }
    vi.stubGlobal('WebSocket', Socket);
    const { WebSocketService } = await import('../analytics/websocketService');
    const client = new WebSocketService({
      url: 'wss://api.example/ws',
      maxReconnectAttempts: 2,
      reconnectInterval: 10,
    });
    const fail = (socket: Socket): void => {
      socket.onerror?.();
      socket.readyState = 3;
      socket.onclose?.({ code: 1006 });
    };
    try {
      const first = client.connect();
      fail(sockets[0]);
      await expect(first).rejects.toThrow('WebSocket connection error');
      await vi.advanceTimersByTimeAsync(10);
      fail(sockets[1]);
      await vi.advanceTimersByTimeAsync(10);
      fail(sockets[2]);
      await vi.advanceTimersByTimeAsync(50);
      expect(sockets).toHaveLength(3);
    } finally {
      client.disconnect();
    }
  });

  it.each([
    ['http://localhost:3000', 'http://localhost:8000/api/v1'],
    ['https://staging-app.example', 'https://staging-api.example/api/v1'],
    ['https://app.example', 'https://api.example/api/v1'],
  ])('uses shared routing for %s', async (origin, expected) => {
    (globalThis as TestGlobal).__TEST_BROWSER_ORIGIN__ = origin;
    const { API_CONFIG } = await import('@/types/api');
    expect(API_CONFIG.BASE_URL).toBe(expected);
  });

  it('connects and reconnects the analytics stream to the same deployed WebSocket origin', async () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    class FakeWebSocket {
      static OPEN = 1;
      static CONNECTING = 0;
      readyState = 0;
      onopen: (() => void) | null = null;
      onclose: ((event: { code: number }) => void) | null = null;
      send = vi.fn();
      constructor(public url: string) {
        sockets.push(this);
      }
      close(): void {
        this.readyState = 3;
      }
    }
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const { useRealtimeStore } = await import('@/stores/analytics');
    useRealtimeStore.setState({ reconnectAttempts: 0 });
    const { analyticsWebSocket } =
      await import('../analytics/websocketService');
    try {
      expect(analyticsWebSocket.isConnecting()).toBe(false);
      const connected = analyticsWebSocket.connect();
      expect(analyticsWebSocket.isConnecting()).toBe(true);
      sockets[0].readyState = FakeWebSocket.OPEN;
      sockets[0].onopen?.();
      await connected;
      expect(analyticsWebSocket.isConnecting()).toBe(false);
      expect(sockets[0].url).toBe('wss://dev-api.example/ws/analytics');

      sockets[0].readyState = 3;
      sockets[0].onclose?.({ code: 1006 });
      await vi.advanceTimersByTimeAsync(3000);
      expect(sockets).toHaveLength(2);
      expect(sockets[1].url).toBe('wss://dev-api.example/ws/analytics');
    } finally {
      analyticsWebSocket.disconnect();
    }
  });
});
