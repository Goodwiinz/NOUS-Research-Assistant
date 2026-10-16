import { useState, useEffect } from 'react';
import { useRealtimeStore } from '@/stores/analytics';
import { AnalyticsMetric, TimeSeriesData } from '@/stores/analytics';
import { getPublicWebSocketOrigin } from '@/utils/publicEndpoints';

// WebSocket Configuration
export interface WebSocketConfig {
  url: string;
  protocols?: string[];
  reconnectInterval?: number;
  maxReconnectAttempts?: number;
  heartbeatInterval?: number;
  connectionTimeout?: number;
}

// Message Types
type WebSocketMessageType =
  | 'metric_update'
  | 'time_series_update'
  | 'graph_update'
  | 'alert_triggered'
  | 'connection_status'
  | 'heartbeat'
  | 'error';

export interface WebSocketMessage {
  type: WebSocketMessageType;
  payload: WebSocketMessagePayload;
  timestamp: string;
  id: string;
}

type WebSocketMessagePayload =
  | AnalyticsMetric
  | TimeSeriesUpdatePayload
  | GraphUpdatePayload
  | AlertTriggeredPayload
  | ConnectionStatusPayload
  | HeartbeatPayload
  | ErrorPayload
  | SubscriptionPayload;

interface TimeSeriesUpdatePayload {
  metricId: string;
  data: TimeSeriesData;
}

interface GraphUpdatePayload {
  action: 'node_added' | 'node_removed' | 'edge_added' | 'edge_removed' | 'node_updated' | 'edge_updated';
  data: GraphNodeData | GraphEdgeData;
}

interface GraphNodeData {
  id: string;
  label?: string;
  type?: string;
  properties?: Record<string, unknown>;
}

interface GraphEdgeData {
  id: string;
  source: string;
  target: string;
  type?: string;
  properties?: Record<string, unknown>;
}

interface AlertTriggeredPayload {
  alertId: string;
  ruleId: string;
  metric: string;
  value: number;
  threshold: number;
  severity: 'low' | 'medium' | 'high' | 'critical';
  message: string;
}

interface ConnectionStatusPayload {
  status: 'connected' | 'disconnected' | 'error';
  message?: string;
}

interface HeartbeatPayload {
  timestamp: string;
  latency?: number;
}

interface ErrorPayload {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

interface SubscriptionPayload {
  action: 'subscribe' | 'unsubscribe';
  metrics: string[];
}

export interface MetricUpdateMessage extends WebSocketMessage {
  type: 'metric_update';
  payload: AnalyticsMetric;
}

export interface TimeSeriesUpdateMessage extends WebSocketMessage {
  type: 'time_series_update';
  payload: {
    metricId: string;
    data: TimeSeriesData;
  };
}

export interface GraphUpdateMessage extends WebSocketMessage {
  type: 'graph_update';
  payload: GraphUpdatePayload;
}

export interface AlertTriggeredMessage extends WebSocketMessage {
  type: 'alert_triggered';
  payload: {
    alertId: string;
    ruleId: string;
    metric: string;
    value: number;
    threshold: number;
    severity: 'low' | 'medium' | 'high' | 'critical';
    message: string;
  };
}

export interface ConnectionStatusMessage extends WebSocketMessage {
  type: 'connection_status';
  payload: {
    status: 'connected' | 'disconnected' | 'error';
    message?: string;
  };
}

export interface HeartbeatMessage extends WebSocketMessage {
  type: 'heartbeat';
  payload: {
    timestamp: string;
    latency?: number;
  };
}

export interface ErrorMessage extends WebSocketMessage {
  type: 'error';
  payload: ErrorPayload;
}

// Event Handlers
type WebSocketEventHandler<T = any> = (data: T) => void;

export interface WebSocketEventHandlers {
  onConnect?: () => void;
  onDisconnect?: () => void;
  onError?: (error: Error) => void;
  onMessage?: (message: WebSocketMessage) => void;
  onMetricUpdate?: (metric: AnalyticsMetric) => void;
  onTimeSeriesUpdate?: (metricId: string, data: TimeSeriesData) => void;
  onGraphUpdate?: (update: GraphUpdateMessage['payload']) => void;
  onAlertTriggered?: (alert: AlertTriggeredMessage['payload']) => void;
  onHeartbeat?: (latency: number) => void;
}

// WebSocket Service Class
export class WebSocketService {
  private ws: WebSocket | null = null;
  private config: WebSocketConfig;
  private handlers: WebSocketEventHandlers = {};
  private reconnectTimer: NodeJS.Timeout | null = null;
  private heartbeatTimer: NodeJS.Timeout | null = null;
  private connectionTimer: NodeJS.Timeout | null = null;
  private messageQueue: WebSocketMessage[] = [];
  private connecting = false;
  private reconnectAttempts = 0;
  private isManualClose = false;
  private lastHeartbeat = 0;
  private pendingSubscriptions: Set<string> = new Set();

  constructor(config: WebSocketConfig) {
    this.config = {
      reconnectInterval: 3000,
      maxReconnectAttempts: 5,
      heartbeatInterval: 30000,
      connectionTimeout: 10000,
      ...config,
    };
  }

  // Connection Management
  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        resolve();
        return;
      }

      if (this.connecting) {
        reject(new Error('Connection already in progress'));
        return;
      }

      this.connecting = true;
      this.isManualClose = false;

      try {
        this.ws = new WebSocket(this.config.url, this.config.protocols);

        // Connection timeout
        this.connectionTimer = setTimeout(() => {
          if (this.ws?.readyState === WebSocket.CONNECTING) {
            this.ws.close();
            reject(new Error('Connection timeout'));
          }
        }, this.config.connectionTimeout);

        this.ws.onopen = () => {
          this.connecting = false;
          this.reconnectAttempts = 0;
          this.clearConnectionTimer();
          this.startHeartbeat();
          this.processPendingSubscriptions();
          this.processMessageQueue();
          this.updateStoreConnection('connected');
          this.handlers.onConnect?.();
          resolve();
        };

        this.ws.onclose = (event) => {
          this.connecting = false;
          this.clearConnectionTimer();
          this.stopHeartbeat();
          this.updateStoreConnection('disconnected');

          if (!this.isManualClose && event.code !== 1000) {
            this.scheduleReconnect();
          }

          this.handlers.onDisconnect?.();
        };

        this.ws.onerror = (error) => {
          this.connecting = false;
          this.clearConnectionTimer();
          this.updateStoreConnection('error');
          const errorObj = new Error('WebSocket connection error');
          this.handlers.onError?.(errorObj);
          reject(errorObj);
        };

        this.ws.onmessage = (event) => {
          this.handleMessage(event.data);
        };

      } catch (error) {
        this.connecting = false;
        this.clearConnectionTimer();
        reject(error);
      }
    });
  }

  disconnect(): void {
    this.isManualClose = true;
    this.clearReconnectTimer();
    this.clearConnectionTimer();
    this.stopHeartbeat();

    if (this.ws) {
      this.ws.close(1000, 'Manual disconnect');
      this.ws = null;
    }

    this.messageQueue = [];
    this.pendingSubscriptions.clear();
    this.connecting = false;
    this.reconnectAttempts = 0;
    this.updateStoreConnection('disconnected');
  }

  private scheduleReconnect(): void {
    if (this.reconnectAttempts >= this.config.maxReconnectAttempts!) {
      this.updateStoreConnection('error');
      return;
    }

    this.reconnectAttempts += 1;
    useRealtimeStore.setState({ reconnectAttempts: this.reconnectAttempts });
    this.clearReconnectTimer();
    this.reconnectTimer = setTimeout(() => {
      if (!this.isManualClose) {
        this.connect().catch(() => {
          // Connection failed, will try again
        });
      }
    }, this.config.reconnectInterval!);
  }

  // Message Handling
  private handleMessage(data: string): void {
    try {
      const message: WebSocketMessage = JSON.parse(data);
      this.handlers.onMessage?.(message);

      switch (message.type) {
        case 'metric_update':
          this.handleMetricUpdate(message as MetricUpdateMessage);
          break;
        case 'time_series_update':
          this.handleTimeSeriesUpdate(message as TimeSeriesUpdateMessage);
          break;
        case 'graph_update':
          this.handleGraphUpdate(message as GraphUpdateMessage);
          break;
        case 'alert_triggered':
          this.handleAlertTriggered(message as AlertTriggeredMessage);
          break;
        case 'connection_status':
          this.handleConnectionStatus(message as ConnectionStatusMessage);
          break;
        case 'heartbeat':
          this.handleHeartbeat(message as HeartbeatMessage);
          break;
        case 'error':
          this.handleError(message as ErrorMessage);
          break;
        default:
          console.warn('Unknown WebSocket message type:', message.type);
      }
    } catch (error) {
      console.error('Error parsing WebSocket message:', error);
    }
  }

  private handleMetricUpdate(message: MetricUpdateMessage): void {
    const store = useRealtimeStore.getState();
    store.processIncomingData(message);
    this.handlers.onMetricUpdate?.(message.payload);
  }

  private handleTimeSeriesUpdate(message: TimeSeriesUpdateMessage): void {
    const store = useRealtimeStore.getState();
    store.processIncomingData(message);
    this.handlers.onTimeSeriesUpdate?.(message.payload.metricId, message.payload.data);
  }

  private handleGraphUpdate(message: GraphUpdateMessage): void {
    this.handlers.onGraphUpdate?.(message.payload);
  }

  private handleAlertTriggered(message: AlertTriggeredMessage): void {
    this.handlers.onAlertTriggered?.(message.payload);
  }

  private handleConnectionStatus(message: ConnectionStatusMessage): void {
    this.updateStoreConnection(message.payload.status);
  }

  private handleHeartbeat(message: HeartbeatMessage): void {
    this.lastHeartbeat = Date.now();
    const latency = this.lastHeartbeat - new Date(message.payload.timestamp).getTime();
    this.handlers.onHeartbeat?.(latency);

    // Send heartbeat response
    this.sendMessage({
      type: 'heartbeat',
      payload: { timestamp: new Date().toISOString(), latency },
      id: this.generateMessageId(),
      timestamp: new Date().toISOString(),
    });
  }

  private handleError(message: ErrorMessage): void {
    console.error('WebSocket error:', message.payload);
    this.handlers.onError?.(new Error(message.payload.message));
  }

  // Message Sending
  sendMessage(message: WebSocketMessage): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    } else {
      this.messageQueue.push(message);
    }
  }

  private processMessageQueue(): void {
    while (this.messageQueue.length > 0 && this.ws?.readyState === WebSocket.OPEN) {
      const message = this.messageQueue.shift();
      if (message) {
        this.ws.send(JSON.stringify(message));
      }
    }
  }

  // Subscription Management
  subscribe(metrics: string[]): void {
    metrics.forEach(metricId => this.pendingSubscriptions.add(metricId));

    if (this.ws?.readyState === WebSocket.OPEN) {
      this.sendSubscription(metrics);
    }
  }

  unsubscribe(metrics: string[]): void {
    metrics.forEach(metricId => this.pendingSubscriptions.delete(metricId));

    if (this.ws?.readyState === WebSocket.OPEN) {
      this.sendUnsubscription(metrics);
    }
  }

  private sendSubscription(metrics: string[]): void {
    this.sendMessage({
      type: 'metric_update',
      payload: { action: 'subscribe', metrics },
      id: this.generateMessageId(),
      timestamp: new Date().toISOString(),
    });
  }

  private sendUnsubscription(metrics: string[]): void {
    this.sendMessage({
      type: 'metric_update',
      payload: { action: 'unsubscribe', metrics },
      id: this.generateMessageId(),
      timestamp: new Date().toISOString(),
    });
  }

  private processPendingSubscriptions(): void {
    if (this.pendingSubscriptions.size > 0) {
      this.sendSubscription(Array.from(this.pendingSubscriptions));
    }
  }

  // Heartbeat Management
  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      this.sendMessage({
        type: 'heartbeat',
        payload: { timestamp: new Date().toISOString() },
        id: this.generateMessageId(),
        timestamp: new Date().toISOString(),
      });
    }, this.config.heartbeatInterval);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  // Utility Methods
  private generateMessageId(): string {
    return `msg_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private clearConnectionTimer(): void {
    if (this.connectionTimer) {
      clearTimeout(this.connectionTimer);
      this.connectionTimer = null;
    }
  }

  private updateStoreConnection(status: 'connecting' | 'connected' | 'disconnected' | 'error'): void {
    useRealtimeStore.setState({
      connectionStatus: status,
      isConnected: status === 'connected',
      reconnectAttempts: this.reconnectAttempts,
    });
  }

  // Event Handler Registration
  onConnect(handler: () => void): void {
    this.handlers.onConnect = handler;
  }

  onDisconnect(handler: () => void): void {
    this.handlers.onDisconnect = handler;
  }

  onError(handler: (error: Error) => void): void {
    this.handlers.onError = handler;
  }

  onMessage(handler: (message: WebSocketMessage) => void): void {
    this.handlers.onMessage = handler;
  }

  onMetricUpdate(handler: (metric: AnalyticsMetric) => void): void {
    this.handlers.onMetricUpdate = handler;
  }

  onTimeSeriesUpdate(handler: (metricId: string, data: TimeSeriesData) => void): void {
    this.handlers.onTimeSeriesUpdate = handler;
  }

  onGraphUpdate(handler: (update: GraphUpdateMessage['payload']) => void): void {
    this.handlers.onGraphUpdate = handler;
  }

  onAlertTriggered(handler: (alert: AlertTriggeredMessage['payload']) => void): void {
    this.handlers.onAlertTriggered = handler;
  }

  onHeartbeat(handler: (latency: number) => void): void {
    this.handlers.onHeartbeat = handler;
  }

  // Get Connection Status
  getReadyState(): number {
    return this.ws?.readyState ?? WebSocket.CLOSED;
  }

  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  isConnecting(): boolean {
    return this.connecting || this.ws?.readyState === WebSocket.CONNECTING;
  }

  getConnectionStats(): {
    connected: boolean;
    connecting: boolean;
    lastHeartbeat: number;
    pendingSubscriptions: number;
    queuedMessages: number;
  } {
    return {
      connected: this.isConnected(),
      connecting: this.isConnecting(),
      lastHeartbeat: this.lastHeartbeat,
      pendingSubscriptions: this.pendingSubscriptions.size,
      queuedMessages: this.messageQueue.length,
    };
  }
}

// Create and export WebSocket service instance
export const createWebSocketService = (config: WebSocketConfig): WebSocketService => {
  return new WebSocketService(config);
};

// Default WebSocket service
export const analyticsWebSocket = createWebSocketService({
  url: `${getPublicWebSocketOrigin()}/ws/analytics`,
});

// React Hook for WebSocket
export const useWebSocket = (config?: WebSocketConfig) => {
  // Configuration selects the initial service; renders retain that service.
  const [ws] = useState(
    () => config ? createWebSocketService(config) : analyticsWebSocket
  );
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const service = ws;

    service.onConnect(() => {
      setIsConnected(true);
      setError(null);
    });

    service.onDisconnect(() => {
      setIsConnected(false);
    });

    service.onError((err) => {
      setError(err.message);
    });

    service.connect().catch((err) => {
      setError(err.message);
    });

    return () => {
      service.disconnect();
    };
  }, [ws]);

  return {
    ws,
    isConnected,
    error,
    subscribe: (metrics: string[]) => ws?.subscribe(metrics),
    unsubscribe: (metrics: string[]) => ws?.unsubscribe(metrics),
  };
};
