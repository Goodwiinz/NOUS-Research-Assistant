import { api } from '@/services/api-client';
import {
  RAGTriadMetrics,
  SystemPerformanceMetrics,
  SystemUsageMetrics,
  PerformanceAnalytics,
  UsageAnalytics,
  TimeRange,
  CustomTimeRange,
  AnalyticsFilters,
} from '@/types';

// Type guard for CustomTimeRange
const isCustomTimeRange = (
  timeRange: TimeRange
): timeRange is CustomTimeRange => {
  return (
    typeof timeRange === 'object' && 'start' in timeRange && 'end' in timeRange
  );
};

export interface QualityMetric {
  id: string;
  name: string;
  current_value: number;
  target_value: number;
  threshold: number;
  unit: string;
  trend: 'improving' | 'declining' | 'stable';
  status: 'good' | 'warning' | 'critical';
  last_updated: string;
  description: string;
}

export interface QualityAlert {
  id: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  title: string;
  description: string;
  metric_name: string;
  recommendation: string;
  created_at: string;
}

export interface UserBehaviorStats {
  total_users: number;
  active_users_today: number;
  active_users_week: number;
  active_users_month: number;
  total_sessions: number;
  avg_session_duration: number;
  bounce_rate: number;
  search_volume_today: number;
  search_volume_week: number;
  user_satisfaction_score: number;
  returning_users: number;
  new_users: number;
}

export interface SearchQuery {
  id: string;
  query: string;
  user_id: string;
  user_email: string;
  timestamp: string;
  results_count: number;
  response_time_ms: number;
  user_rating: 'up' | 'down' | null;
  clicked_results: number;
  filters_used: string[];
  content_types: string[];
}

export interface UserSession {
  id: string;
  user_id: string;
  user_email: string;
  start_time: string;
  duration_seconds: number;
  search_count: number;
  document_views: number;
  session_quality: 'high' | 'medium' | 'low';
  satisfaction_score: number | null;
}

export interface SystemMetrics {
  timestamp: string;
  cpu_usage: number;
  memory_usage: number;
  disk_usage: number;
  network_io: {
    bytes_in: number;
    bytes_out: number;
  };
  response_time: number;
  request_rate: number;
  error_rate: number;
  active_connections: number;
}

export interface ServiceStatus {
  name: string;
  status: 'healthy' | 'warning' | 'critical' | 'unknown';
  response_time: number;
  last_check: string;
  uptime: number;
  error_count: number;
}

export interface PerformanceAlert {
  id: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  type: 'performance' | 'availability' | 'capacity';
  title: string;
  description: string;
  service: string;
  timestamp: string;
  resolved: boolean;
}

export interface Recommendation {
  id: string;
  title: string;
  description: string;
  category:
    | 'content'
    | 'search_algorithm'
    | 'infrastructure'
    | 'user_experience'
    | 'indexing'
    | 'monitoring';
  priority: 'critical' | 'high' | 'medium' | 'low';
  impact_assessment: string;
  effort_required: 'low' | 'medium' | 'high';
  actionable_steps: string[];
  expected_outcome: string;
  estimated_improvement: number;
  status: 'pending' | 'in_progress' | 'completed' | 'rejected' | 'deferred';
  created_at: string;
  updated_at: string;
  votes: {
    up: number;
    down: number;
  };
  implemented_at?: string;
  results_observed?: string;
}

export interface RecommendationRule {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  category: string;
  condition: string;
  recommendation_template: string;
  last_triggered: string;
  trigger_count: number;
}

class AnalyticsService {
  // Quality Metrics
  async getQualityMetrics(): Promise<{
    metrics: QualityMetric[];
    alerts: QualityAlert[];
  }> {
    try {
      const response = (await api.get(
        '/analytics/quality/metrics'
      )) as any;
      return response.data;
    } catch (error) {
      // Fallback to mock data if API fails
      return this.getMockQualityMetrics();
    }
  }

  // User Behavior Analytics
  async getUserBehaviorStats(timeRange: string = '7d'): Promise<{
    stats: UserBehaviorStats;
    queries: SearchQuery[];
    sessions: UserSession[];
  }> {
    try {
      const response = (await api.get(
        `/analytics/user-behavior?timeRange=${timeRange}`
      )) as any;
      return response.data;
    } catch (error) {
      // Fallback to mock data if API fails
      return this.getMockUserBehaviorData();
    }
  }

  // Performance Monitoring
  async getPerformanceMetrics(): Promise<{
    metrics: SystemMetrics;
    services: ServiceStatus[];
    alerts: PerformanceAlert[];
  }> {
    // `/analytics/performance` is mounted on the backend (see
    // `backend/src/main.py` mounting `performance_dashboard_router` under
    // that prefix), but there is no index handler at the bare prefix yet —
    // only sub-paths like `/overview`, `/system-health`, `/search-performance`.
    // The 404 falls through to the mock fallback below. When a backend index
    // handler (or a shape-matching sub-path) lands, wire it up here.
    try {
      const response = (await api.get(
        '/analytics/performance'
      )) as any;
      return response.data;
    } catch (error) {
      return this.getMockPerformanceData();
    }
  }

  // Recommendations Engine
  async getRecommendations(): Promise<{
    recommendations: Recommendation[];
    rules: RecommendationRule[];
    improvements: any[];
  }> {
    try {
      const response = (await api.get(
        '/analytics/recommendations'
      )) as any;
      return response.data;
    } catch (error) {
      // Fallback to mock data if API fails
      return this.getMockRecommendationsData();
    }
  }

  // Update recommendation status
  async updateRecommendationStatus(
    recommendationId: string,
    status: Recommendation['status']
  ): Promise<void> {
    try {
      await api.patch(
        `/analytics/recommendations/${recommendationId}`,
        { status }
      );
    } catch (error) {
      console.error('Failed to update recommendation status:', error);
      throw error;
    }
  }

  // Vote on recommendation
  async voteOnRecommendation(
    recommendationId: string,
    voteType: 'up' | 'down'
  ): Promise<void> {
    try {
      await api.post(
        `/analytics/recommendations/${recommendationId}/vote`,
        { voteType }
      );
    } catch (error) {
      console.error('Failed to vote on recommendation:', error);
      throw error;
    }
  }

  // RAG Triad Metrics (New architecture)
  async getRAGTriadMetrics(
    timeRange: TimeRange
  ): Promise<{ metrics: RAGTriadMetrics }> {
    try {
      const params = isCustomTimeRange(timeRange)
        ? new URLSearchParams({ start: timeRange.start, end: timeRange.end })
        : new URLSearchParams({ preset: timeRange });
      return await api.get(`/analytics/rag-triad?${params}`);
    } catch (error) {
      console.error('Failed to fetch RAG triad metrics:', error);
      throw error;
    }
  }

  // Performance Analytics (New architecture)
  async getPerformanceAnalytics(
    filters: AnalyticsFilters,
    timeRange: TimeRange
  ): Promise<PerformanceAnalytics> {
    try {
      return await api.post('/analytics/performance', {
        filters,
        timeRange,
      });
    } catch (error) {
      console.error('Failed to fetch performance analytics:', error);
      throw error;
    }
  }

  // Usage Analytics (New architecture)
  async getUsageAnalytics(timeRange: TimeRange): Promise<UsageAnalytics> {
    try {
      const params = isCustomTimeRange(timeRange)
        ? new URLSearchParams({ start: timeRange.start, end: timeRange.end })
        : new URLSearchParams({ preset: timeRange });
      return await api.get(`/analytics/usage?${params}`);
    } catch (error) {
      console.error('Failed to fetch usage analytics:', error);
      throw error;
    }
  }

  // Real-time Metrics (New architecture)
  async getRealTimeMetrics(): Promise<{ metrics: RAGTriadMetrics }> {
    try {
      return await api.get('/analytics/real-time');
    } catch (error) {
      console.error('Failed to fetch real-time metrics:', error);
      throw error;
    }
  }

  // Historical Trends (New architecture)
  async getHistoricalTrends(
    metric: string,
    timeRange: TimeRange
  ): Promise<any> {
    try {
      const params = isCustomTimeRange(timeRange)
        ? new URLSearchParams({ start: timeRange.start, end: timeRange.end })
        : new URLSearchParams({ preset: timeRange });
      return await api.get(`/analytics/trends/${metric}?${params}`);
    } catch (error) {
      console.error('Failed to fetch historical trends:', error);
      throw error;
    }
  }

  // Comparison Data (New architecture)
  async getComparisonData(timeRanges: TimeRange[]): Promise<any> {
    try {
      return await api.post('/analytics/compare', {
        timeRanges,
      });
    } catch (error) {
      console.error('Failed to fetch comparison data:', error);
      throw error;
    }
  }

  // Export Analytics (New architecture)
  async exportAnalytics(
    format: 'csv' | 'json' | 'pdf',
    filters: AnalyticsFilters,
    timeRange: TimeRange
  ): Promise<void> {
    try {
      await api.download(
        `/analytics/export?format=${format}`,
        `analytics-${Date.now()}.${format}`
      );
    } catch (error) {
      console.error('Failed to export analytics:', error);
      throw error;
    }
  }

  // Mock Data Methods (used as fallbacks)
  private getMockQualityMetrics() {
    const mockMetrics: QualityMetric[] = [
      {
        id: 'answer_relevancy',
        name: 'Answer Relevancy',
        current_value: 94.2,
        target_value: 95.0,
        threshold: 70.0,
        unit: '%',
        trend: 'improving',
        status: 'good',
        last_updated: new Date().toISOString(),
        description:
          'Measures how relevant the generated answers are to the user query',
      },
      {
        id: 'faithfulness',
        name: 'Faithfulness',
        current_value: 89.5,
        target_value: 90.0,
        threshold: 90.0,
        unit: '%',
        trend: 'stable',
        status: 'warning',
        last_updated: new Date().toISOString(),
        description:
          'Ensures answers are factually consistent with source documents',
      },
      {
        id: 'contextual_relevancy',
        name: 'Contextual Relevancy',
        current_value: 96.1,
        target_value: 95.0,
        threshold: 70.0,
        unit: '%',
        trend: 'improving',
        status: 'good',
        last_updated: new Date().toISOString(),
        description: 'Measures how well retrieved context matches the query',
      },
      {
        id: 'response_time',
        name: 'Response Time',
        current_value: 1.8,
        target_value: 2.0,
        threshold: 5.0,
        unit: 'seconds',
        trend: 'improving',
        status: 'good',
        last_updated: new Date().toISOString(),
        description: 'Average time to generate responses',
      },
      {
        id: 'user_satisfaction',
        name: 'User Satisfaction',
        current_value: 4.3,
        target_value: 4.5,
        threshold: 3.5,
        unit: '/5',
        trend: 'stable',
        status: 'warning',
        last_updated: new Date().toISOString(),
        description: 'Average user rating of response quality',
      },
      {
        id: 'hallucination_rate',
        name: 'Hallucination Rate',
        current_value: 8.2,
        target_value: 5.0,
        threshold: 10.0,
        unit: '%',
        trend: 'declining',
        status: 'warning',
        last_updated: new Date().toISOString(),
        description: 'Percentage of responses with factual inconsistencies',
      },
    ];

    const mockAlerts: QualityAlert[] = [
      {
        id: '1',
        severity: 'medium',
        title: 'Faithfulness Below Target',
        description: 'Faithfulness score has fallen below the 90% threshold',
        metric_name: 'Faithfulness',
        recommendation:
          'Review context retrieval and adjust similarity thresholds',
        created_at: new Date(Date.now() - 3600000).toISOString(),
      },
    ];

    return { metrics: mockMetrics, alerts: mockAlerts };
  }

  private getMockUserBehaviorData() {
    const mockStats: UserBehaviorStats = {
      total_users: 1250,
      active_users_today: 89,
      active_users_week: 342,
      active_users_month: 789,
      total_sessions: 5678,
      avg_session_duration: 345,
      bounce_rate: 23.5,
      search_volume_today: 1234,
      search_volume_week: 5678,
      user_satisfaction_score: 4.6,
      returning_users: 890,
      new_users: 360,
    };

    const mockQueries: SearchQuery[] = [
      {
        id: '1',
        query: 'quarterly financial report Q3 2024',
        user_id: 'user_123',
        user_email: 'john.doe@company.com',
        timestamp: new Date(Date.now() - 3600000).toISOString(),
        results_count: 12,
        response_time_ms: 1250,
        user_rating: 'up',
        clicked_results: 3,
        filters_used: ['date_range', 'content_type'],
        content_types: ['PDF', 'DOCX'],
      },
    ];

    const mockSessions: UserSession[] = [
      {
        id: 'session_1',
        user_id: 'user_123',
        user_email: 'john.doe@company.com',
        start_time: new Date(Date.now() - 1800000).toISOString(),
        duration_seconds: 1800,
        search_count: 8,
        document_views: 12,
        session_quality: 'high',
        satisfaction_score: 5.0,
      },
    ];

    return { stats: mockStats, queries: mockQueries, sessions: mockSessions };
  }

  private getMockPerformanceData() {
    const mockMetrics: SystemMetrics = {
      timestamp: new Date().toISOString(),
      cpu_usage: 45.2,
      memory_usage: 67.8,
      disk_usage: 34.1,
      network_io: {
        bytes_in: 1024000,
        bytes_out: 2048000,
      },
      response_time: 245,
      request_rate: 125.5,
      error_rate: 1.2,
      active_connections: 89,
    };

    const mockServices: ServiceStatus[] = [
      {
        name: 'Backend API',
        status: 'healthy',
        response_time: 245,
        last_check: new Date().toISOString(),
        uptime: 99.9,
        error_count: 0,
      },
      {
        name: 'Database',
        status: 'healthy',
        response_time: 12,
        last_check: new Date().toISOString(),
        uptime: 99.95,
        error_count: 0,
      },
      {
        name: 'Vector Store',
        status: 'warning',
        response_time: 450,
        last_check: new Date().toISOString(),
        uptime: 97.2,
        error_count: 3,
      },
    ];

    const mockAlerts: PerformanceAlert[] = [
      {
        id: '1',
        severity: 'medium',
        type: 'performance',
        title: 'High Response Time',
        description: 'Vector store response time is above threshold',
        service: 'Vector Store',
        timestamp: new Date(Date.now() - 300000).toISOString(),
        resolved: false,
      },
    ];

    return { metrics: mockMetrics, services: mockServices, alerts: mockAlerts };
  }

  private getMockRecommendationsData() {
    const mockRecommendations: Recommendation[] = [
      {
        id: '1',
        title: 'Improve Document Quality Scores',
        description:
          'Update outdated content and enhance metadata for better search relevance',
        category: 'content',
        priority: 'high',
        impact_assessment:
          'High impact on search accuracy and user satisfaction',
        effort_required: 'medium',
        actionable_steps: [
          'Audit existing documents for quality issues',
          'Update document metadata and tags',
          'Implement content quality scoring system',
        ],
        expected_outcome: '15-20% improvement in search relevance',
        estimated_improvement: 18,
        status: 'pending',
        created_at: '2025-10-09T21:41:00Z',
        updated_at: '2025-10-09T21:41:00Z',
        votes: { up: 5, down: 1 },
      },
    ];

    const mockRules: RecommendationRule[] = [
      {
        id: '1',
        name: 'High Response Time Alert',
        description: 'Triggers when average response time exceeds 2 seconds',
        enabled: true,
        category: 'performance',
        condition: 'avg_response_time > 2000ms',
        recommendation_template: 'Optimize {service} performance',
        last_triggered: '2025-10-09T20:30:00Z',
        trigger_count: 15,
      },
    ];

    return {
      recommendations: mockRecommendations,
      rules: mockRules,
      improvements: [],
    };
  }
}

// Create singleton instance
const analyticsService = new AnalyticsService();

export default analyticsService;
