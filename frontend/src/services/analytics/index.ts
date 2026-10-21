import { getPublicApiOrigin } from '@/utils/publicEndpoints';
import {
  metricsApi,
  graphApi,
  dashboardApi,
  reportApi,
  alertApi,
  exportApi,
  isApiError,
  handleApiError,
} from './analyticsApi';
import { createWebSocketService, analyticsWebSocket } from './websocketService';

// API Services
export {
  metricsApi,
  graphApi,
  dashboardApi,
  reportApi,
  alertApi,
  exportApi,
  createApiError,
  isApiError,
  handleApiError,
} from './analyticsApi';

// WebSocket Service
export {
  WebSocketService,
  createWebSocketService,
  analyticsWebSocket,
  useWebSocket,
} from './websocketService';

export type {
  WebSocketConfig,
  WebSocketMessage,
  WebSocketEventHandlers,
  MetricUpdateMessage,
  TimeSeriesUpdateMessage,
  GraphUpdateMessage,
  AlertTriggeredMessage,
  ConnectionStatusMessage,
  HeartbeatMessage,
  ErrorMessage,
} from './websocketService';

// Service Factory
export class AnalyticsServiceFactory {
  private static instance: AnalyticsServiceFactory;
  private services: Map<string, any> = new Map();

  static getInstance(): AnalyticsServiceFactory {
    if (!AnalyticsServiceFactory.instance) {
      AnalyticsServiceFactory.instance = new AnalyticsServiceFactory();
    }
    return AnalyticsServiceFactory.instance;
  }

  getMetricsApi() {
    if (!this.services.has('metrics')) {
      this.services.set('metrics', metricsApi);
    }
    return this.services.get('metrics');
  }

  getGraphApi() {
    if (!this.services.has('graph')) {
      this.services.set('graph', graphApi);
    }
    return this.services.get('graph');
  }

  getDashboardApi() {
    if (!this.services.has('dashboard')) {
      this.services.set('dashboard', dashboardApi);
    }
    return this.services.get('dashboard');
  }

  getReportApi() {
    if (!this.services.has('report')) {
      this.services.set('report', reportApi);
    }
    return this.services.get('report');
  }

  getAlertApi() {
    if (!this.services.has('alert')) {
      this.services.set('alert', alertApi);
    }
    return this.services.get('alert');
  }

  getExportApi() {
    if (!this.services.has('export')) {
      this.services.set('export', exportApi);
    }
    return this.services.get('export');
  }

  getWebSocketService(config?: any) {
    if (!this.services.has('websocket')) {
      this.services.set(
        'websocket',
        config ? createWebSocketService(config) : analyticsWebSocket
      );
    }
    return this.services.get('websocket');
  }

  reset(): void {
    this.services.clear();
  }
}

// Export factory instance
export const analyticsServiceFactory = AnalyticsServiceFactory.getInstance();

// Utility functions for service initialization
export const initializeAnalyticsServices = (config?: {
  apiBaseUrl?: string;
  websocketUrl?: string;
  authToken?: string;
}) => {
  // Update API configuration
  if (config?.apiBaseUrl) {
    process.env.NEXT_PUBLIC_API_URL = config.apiBaseUrl;
  }

  if (config?.websocketUrl) {
    process.env.NEXT_PUBLIC_WEBSOCKET_URL = config.websocketUrl;
  }

  // SECURITY (audit #18): do NOT persist the auth token to localStorage — it
  // is readable by any XSS payload and outlives the session. Supabase manages
  // auth via ssr cookies; the analytics client sources its token from the live
  // session at call time, not a cached copy.

  // Return service instances
  return {
    metrics: analyticsServiceFactory.getMetricsApi(),
    graph: analyticsServiceFactory.getGraphApi(),
    dashboard: analyticsServiceFactory.getDashboardApi(),
    reports: analyticsServiceFactory.getReportApi(),
    alerts: analyticsServiceFactory.getAlertApi(),
    export: analyticsServiceFactory.getExportApi(),
    websocket: analyticsServiceFactory.getWebSocketService(),
  };
};

// Error handling utilities
export const createServiceErrorHandler = (serviceName: string) => {
  return (error: any): string => {
    console.error(`Error in ${serviceName}:`, error);

    if (isApiError(error)) {
      return error.error;
    }

    if (error.response?.status === 401) {
      return 'Authentication required. Please log in again.';
    }

    if (error.response?.status === 403) {
      return 'You do not have permission to perform this action.';
    }

    if (error.response?.status === 404) {
      return 'The requested resource was not found.';
    }

    if (error.response?.status === 429) {
      return 'Too many requests. Please try again later.';
    }

    if (error.response?.status >= 500) {
      return 'Server error. Please try again later.';
    }

    if (error.code === 'ECONNABORTED') {
      return 'Request timeout. Please check your connection and try again.';
    }

    if (error.code === 'NETWORK_ERROR') {
      return 'Network error. Please check your connection and try again.';
    }

    return error.message || 'An unexpected error occurred';
  };
};

// Service status monitoring
export const getServiceStatus = () => {
  const ws = analyticsServiceFactory.getWebSocketService();

  return {
    websocket: {
      connected: ws.isConnected(),
      connecting: ws.isConnecting(),
      stats: ws.getConnectionStats(),
    },
    api: {
      baseUrl: getPublicApiOrigin(),
      authenticated: !!localStorage.getItem('authToken'),
    },
  };
};

// Service health check
export const performHealthCheck = async () => {
  try {
    const metricsApi = analyticsServiceFactory.getMetricsApi();
    await metricsApi.getMetrics();

    return {
      healthy: true,
      services: {
        api: { status: 'healthy', responseTime: Date.now() },
        websocket: {
          status: analyticsWebSocket.isConnected() ? 'healthy' : 'unhealthy',
          stats: analyticsWebSocket.getConnectionStats(),
        },
      },
    };
  } catch (error) {
    return {
      healthy: false,
      services: {
        api: { status: 'unhealthy', error: handleApiError(error) },
        websocket: {
          status: analyticsWebSocket.isConnected() ? 'healthy' : 'unhealthy',
          stats: analyticsWebSocket.getConnectionStats(),
        },
      },
    };
  }
};
