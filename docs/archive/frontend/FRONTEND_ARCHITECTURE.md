> **Archived 2026-10-17: early frontend architecture proposal that does not match the current app; see frontend/README.md and docs/engineering/frontend.md.**
> Historical record, not a live contract. Originally at `frontend/docs/FRONTEND_ARCHITECTURE.md`; relative links and paths below were not updated.

# Frontend Architecture - Multimodal Enterprise RAG System

## Overview

This document outlines the comprehensive frontend architecture for the Multimodal Enterprise RAG System evaluation and analytics platform. The architecture is built with React 18, TypeScript, and focuses on performance, accessibility, and maintainability.

## Technology Stack

- **React 18** - Main UI framework with concurrent features
- **TypeScript** - Type safety and developer experience
- **React Router v6** - Client-side routing with lazy loading
- **Zustand** - Lightweight state management
- **React Query v3** - Server state management and caching
- **Radix UI + shadcn/ui** - Accessible component library
- **Tailwind CSS** - Utility-first styling
- **Recharts** - Data visualization
- **React Hook Form** - Form management
- **Playwright** - End-to-end testing
- **React Testing Library** - Component testing

## Architecture Principles

1. **Component-First**: Build reusable, accessible components
2. **Evaluation-First**: Test-driven development with comprehensive coverage
3. **Performance-First**: Code splitting, lazy loading, and virtualization
4. **Accessibility-First**: WCAG 2.1 AA compliance throughout
5. **Mobile-First**: Responsive design with progressive enhancement

## Directory Structure

```
frontend/src/
├── components/              # Reusable UI components
│   ├── analytics/          # Analytics-specific components
│   ├── evaluation/         # Evaluation management components
│   ├── search/            # Search analytics components
│   ├── monitoring/        # System monitoring components
│   ├── ab-testing/        # A/B testing interface
│   ├── settings/          # Settings and configuration
│   ├── common/            # Shared components
│   ├── layout/            # Layout components
│   └── ui/                # Base UI components (shadcn/ui)
├── pages/                 # Page components with lazy loading
├── hooks/                 # Custom React hooks
├── stores/                # Zustand state stores
├── services/              # API services and data fetching
├── utils/                 # Utility functions
├── types/                 # TypeScript type definitions
├── constants/             # Application constants
├── styles/                # Global styles and themes
└── __tests__/             # Test files
```

## Component Hierarchy

### Root Level
```
App
├── AuthProvider
├── QueryClientProvider
├── ThemeProvider
├── Router
└── ErrorBoundary
```

### Layout Structure
```
AppLayout
├── Header
│   ├── Navigation
│   ├── UserMenu
│   └── NotificationBell
├── Sidebar
│   ├── NavigationItems
│   └── UserInfo
├── MainContent
│   └── PageContent
└── Footer
```

### Page Components
```
Pages/
├── DashboardPage (Analytics)
├── EvaluationPage
├── ABTestingPage
├── SearchAnalyticsPage
├── MonitoringPage
└── SettingsPage
```

## State Management Architecture

### Global State (Zustand)

1. **Auth Store** - User authentication state
2. **UI Store** - Theme, sidebar state, notifications
3. **Analytics Store** - Real-time metrics and filters
4. **Evaluation Store** - Evaluation configuration and results
5. **Settings Store** - User preferences and organization settings

### Server State (React Query)

1. **Analytics Queries** - RAG triad metrics, performance data
2. **Evaluation Queries** - Evaluation results and comparisons
3. **System Queries** - Health checks, resource utilization
4. **User Queries** - User preferences, feedback data

### Component State (useState/useReducer)

1. **Form State** - Local form inputs and validation
2. **UI State** - Component-specific UI interactions
3. **Temporary State** - Loading states, error states

## Routing Configuration

### Route Structure

```typescript
const routes = [
  {
    path: '/',
    element: <AppLayout />,
    children: [
      {
        index: true,
        element: <DashboardPage />,
        lazy: () => import('@/pages/DashboardPage')
      },
      {
        path: 'analytics',
        children: [
          {
            index: true,
            element: <AnalyticsDashboard />,
            lazy: () => import('@/pages/analytics/AnalyticsDashboard')
          },
          {
            path: 'performance',
            element: <PerformanceAnalytics />,
            lazy: () => import('@/pages/analytics/PerformanceAnalytics')
          },
          {
            path: 'usage',
            element: <UsageAnalytics />,
            lazy: () => import('@/pages/analytics/UsageAnalytics')
          }
        ]
      },
      {
        path: 'evaluation',
        children: [
          {
            index: true,
            element: <EvaluationManagement />,
            lazy: () => import('@/pages/evaluation/EvaluationManagement')
          },
          {
            path: 'create',
            element: <CreateEvaluation />,
            lazy: () => import('@/pages/evaluation/CreateEvaluation')
          },
          {
            path: ':id',
            element: <EvaluationDetails />,
            lazy: () => import('@/pages/evaluation/EvaluationDetails')
          },
          {
            path: 'compare',
            element: <CompareEvaluations />,
            lazy: () => import('@/pages/evaluation/CompareEvaluations')
          }
        ]
      },
      {
        path: 'ab-testing',
        children: [
          {
            index: true,
            element: <ABTestingDashboard />,
            lazy: () => import('@/pages/ab-testing/ABTestingDashboard')
          },
          {
            path: 'create',
            element: <CreateExperiment />,
            lazy: () => import('@/pages/ab-testing/CreateExperiment')
          },
          {
            path: ':id',
            element: <ExperimentDetails />,
            lazy: () => import('@/pages/ab-testing/ExperimentDetails')
          }
        ]
      },
      {
        path: 'search-analytics',
        element: <SearchAnalyticsPage />,
        lazy: () => import('@/pages/search-analytics/SearchAnalyticsPage')
      },
      {
        path: 'monitoring',
        children: [
          {
            index: true,
            element: <SystemMonitoring />,
            lazy: () => import('@/pages/monitoring/SystemMonitoring')
          },
          {
            path: 'alerts',
            element: <AlertManagement />,
            lazy: () => import('@/pages/monitoring/AlertManagement')
          }
        ]
      },
      {
        path: 'settings',
        children: [
          {
            index: true,
            element: <UserSettings />,
            lazy: () => import('@/pages/settings/UserSettings')
          },
          {
            path: 'organization',
            element: <OrganizationSettings />,
            lazy: () => import('@/pages/settings/OrganizationSettings')
          },
          {
            path: 'api-keys',
            element: <APIKeyManagement />,
            lazy: () => import('@/pages/settings/APIKeyManagement')
          }
        ]
      }
    ]
  }
];
```

## Data Fetching Patterns

### React Query Configuration

```typescript
// Query Client Configuration
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000, // 5 minutes
      cacheTime: 10 * 60 * 1000, // 10 minutes
      retry: (failureCount, error) => {
        if (error.status === 404) return false;
        return failureCount < 3;
      },
      refetchOnWindowFocus: false,
      refetchOnReconnect: true,
    },
    mutations: {
      retry: 1,
    },
  },
});
```

### Custom Hooks

```typescript
// Analytics Hooks
export const useRAGTriadMetrics = (timeRange: TimeRange) => {
  return useQuery({
    queryKey: ['rag-triad-metrics', timeRange],
    queryFn: () => analyticsService.getRAGTriadMetrics(timeRange),
    select: (data) => data.metrics,
    enabled: !!timeRange,
  });
};

export const usePerformanceAnalytics = (filters: PerformanceFilters) => {
  return useQuery({
    queryKey: ['performance-analytics', filters],
    queryFn: () => analyticsService.getPerformanceAnalytics(filters),
    select: (data) => ({
      ...data,
      chartData: transformDataForCharts(data),
    }),
  });
};

// Evaluation Hooks
export const useEvaluations = (params: EvaluationParams) => {
  return useInfiniteQuery({
    queryKey: ['evaluations', params],
    queryFn: ({ pageParam = 0 }) =>
      evaluationService.getEvaluations({ ...params, page: pageParam }),
    getNextPageParam: (lastPage) => lastPage.hasNext ? lastPage.page + 1 : undefined,
  });
};

// Real-time Updates
export const useRealTimeMetrics = () => {
  const queryClient = useQueryClient();

  useEffect(() => {
    const ws = new WebSocket(WS_URL);

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      queryClient.setQueryData(['real-time-metrics'], data);
    };

    return () => ws.close();
  }, [queryClient]);

  return useQuery({
    queryKey: ['real-time-metrics'],
    queryFn: () => analyticsService.getRealTimeMetrics(),
    refetchInterval: 30000, // Fallback polling
  });
};
```

## Component Architecture

### Analytics Dashboard Components

```typescript
// Main Dashboard Component
interface AnalyticsDashboardProps {
  timeRange: TimeRange;
  filters: AnalyticsFilters;
}

const AnalyticsDashboard: React.FC<AnalyticsDashboardProps> = ({
  timeRange,
  filters,
}) => {
  return (
    <div className="analytics-dashboard">
      <MetricsOverview timeRange={timeRange} />
      <RAGTriadCharts data={useRAGTriadMetrics(timeRange)} />
      <PerformanceTrends data={usePerformanceAnalytics(filters)} />
      <SystemHealth indicators={useSystemHealth()} />
    </div>
  );
};

// Chart Components
const RAGTriadCharts: React.FC<{ data: RAGTriadMetrics }> = ({ data }) => (
  <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
    <MetricCard
      title="Answer Relevancy"
      value={data.answer_relevancy}
      threshold={70}
      trend={data.answer_relevancy_trend}
    />
    <MetricCard
      title="Faithfulness"
      value={data.faithfulness}
      threshold={90}
      trend={data.faithfulness_trend}
    />
    <MetricCard
      title="Contextual Relevancy"
      value={data.contextual_relevancy}
      threshold={70}
      trend={data.contextual_relevancy_trend}
    />
  </div>
);
```

### Evaluation Management Components

```typescript
const EvaluationManagement: React.FC = () => {
  const [selectedEvaluations, setSelectedEvaluations] = useState<string[]>([]);
  const evaluations = useEvaluations({ status: 'all' });

  return (
    <div className="evaluation-management">
      <EvaluationHeader
        onCompare={handleCompare}
        selectedCount={selectedEvaluations.length}
      />
      <EvaluationTable
        evaluations={evaluations.data}
        onSelectionChange={setSelectedEvaluations}
        loading={evaluations.isLoading}
      />
      <EvaluationPagination
        hasNext={evaluations.hasNextPage}
        hasPrev={evaluations.hasPreviousPage}
        onNext={evaluations.fetchNextPage}
        onPrev={evaluations.fetchPreviousPage}
      />
    </div>
  );
};
```

## Accessibility Implementation

### ARIA Patterns

1. **Navigation** - Proper landmark roles and keyboard navigation
2. **Data Tables** - Sortable headers, row selection, and screen reader support
3. **Charts** - Alternative text and data tables for visual content
4. **Forms** - Proper labeling, error announcements, and validation
5. **Notifications** - Live regions for dynamic content updates

### Keyboard Navigation

```typescript
const useKeyboardNavigation = () => {
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      switch (event.key) {
        case 'Tab':
          // Ensure focus trapping in modals
          handleTabNavigation(event);
          break;
        case 'Escape':
          // Close modals and dropdowns
          handleEscapeKey(event);
          break;
        case 'Enter':
        case ' ':
          // Activate focused elements
          handleActivation(event);
          break;
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);
};
```

### Screen Reader Support

```typescript
const MetricCard: React.FC<MetricCardProps> = ({ title, value, threshold, trend }) => {
  const status = value >= threshold ? 'good' : 'warning';
  const ariaLabel = `${title}: ${value}%. Threshold: ${threshold}%. Status: ${status}`;

  return (
    <div
      className="metric-card"
      role="region"
      aria-label={ariaLabel}
      tabIndex={0}
    >
      <h3 className="metric-title">{title}</h3>
      <div className="metric-value" aria-label={`Current value: ${value}%`}>
        {value}%
      </div>
      <TrendIndicator trend={trend} aria-label={`Trend: ${trend.direction}`} />
    </div>
  );
};
```

## Performance Optimization

### Code Splitting

```typescript
// Lazy loaded components
const AnalyticsDashboard = lazy(() =>
  import('@/pages/analytics/AnalyticsDashboard').then(module => ({
    default: module.AnalyticsDashboard
  }))
);

const EvaluationManagement = lazy(() =>
  import('@/pages/evaluation/EvaluationManagement')
);

// Route-based splitting with Suspense
const AppRouter = () => (
  <Router>
    <Routes>
      <Route path="/" element={<AppLayout />}>
        <Route index element={
          <Suspense fallback={<PageSkeleton />}>
            <AnalyticsDashboard />
          </Suspense>
        } />
        {/* Other routes */}
      </Route>
    </Routes>
  </Router>
);
```

### Virtualization

```typescript
// Virtual table for large datasets
const VirtualizedTable: React.FC<VirtualizedTableProps> = ({ data }) => {
  const [visibleRange, setVisibleRange] = useState({ start: 0, end: 50 });

  return (
    <div className="virtualized-table">
      <div className="table-header">
        {/* Fixed header */}
      </div>
      <div
        className="table-body"
        onScroll={handleScroll}
        style={{ height: '400px', overflow: 'auto' }}
      >
        {data.slice(visibleRange.start, visibleRange.end).map(item => (
          <TableRow key={item.id} data={item} />
        ))}
      </div>
    </div>
  );
};
```

### Memoization

```typescript
// Expensive chart rendering
const ExpensiveChart = memo(({ data }: ChartProps) => {
  const chartData = useMemo(() =>
    transformDataForCharts(data), [data]
  );

  return <LineChart data={chartData} />;
});

// Optimized list rendering
const EvaluationList = memo(({ evaluations }: EvaluationListProps) => (
  <div className="evaluation-list">
    {evaluations.map(evaluation => (
      <EvaluationItem
        key={evaluation.id}
        evaluation={evaluation}
      />
    ))}
  </div>
));
```

## Real-time Updates

### WebSocket Integration

```typescript
const useWebSocket = (url: string) => {
  const [socket, setSocket] = useState<WebSocket | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const queryClient = useQueryClient();

  useEffect(() => {
    const ws = new WebSocket(url);

    ws.onopen = () => {
      setIsConnected(true);
      setSocket(ws);
    };

    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);

      // Update relevant queries based on message type
      switch (message.type) {
        case 'METRICS_UPDATE':
          queryClient.setQueryData(['real-time-metrics'], message.data);
          break;
        case 'EVALUATION_COMPLETE':
          queryClient.invalidateQueries(['evaluations']);
          break;
        case 'SYSTEM_ALERT':
          queryClient.setQueryData(['system-alerts'], message.data);
          break;
      }
    };

    ws.onclose = () => {
      setIsConnected(false);
      // Implement reconnection logic
      setTimeout(() => {
        setSocket(new WebSocket(url));
      }, 5000);
    };

    return () => ws.close();
  }, [url, queryClient]);

  return { socket, isConnected };
};
```

## Error Handling

### Error Boundaries

```typescript
class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean; error?: Error }
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    // Log error to monitoring service
    errorReporting.captureException(error, {
      extra: errorInfo,
    });
  }

  render() {
    if (this.state.hasError) {
      return <ErrorFallback error={this.state.error} />;
    }

    return this.props.children;
  }
}

const ErrorFallback: React.FC<{ error?: Error }> = ({ error }) => (
  <div className="error-fallback" role="alert">
    <h2>Something went wrong</h2>
    <p>We're sorry, but something unexpected happened.</p>
    {error && (
      <details className="error-details">
        <summary>Error details</summary>
        <pre>{error.message}</pre>
      </details>
    )}
    <button onClick={() => window.location.reload()}>
      Reload page
    </button>
  </div>
);
```

### Global Error Handling

```typescript
// Error toast notifications
const useErrorHandler = () => {
  const queryClient = useQueryClient();

  const handleError = useCallback((error: unknown) => {
    if (error instanceof APIErrorClass) {
      switch (error.error.type) {
        case 'validation_error':
          toast.error('Validation error: ' + error.error.message);
          break;
        case 'processing_error':
          toast.error('Processing error: ' + error.error.message);
          break;
        case 'auth_error':
          toast.error('Authentication error. Please log in again.');
          // Redirect to login
          break;
        case 'rate_limit':
          toast.error('Rate limit exceeded. Please try again later.');
          break;
        default:
          toast.error('An unexpected error occurred.');
      }
    } else {
      toast.error('An unexpected error occurred.');
    }

    // Log error for debugging
    console.error('Application error:', error);
  }, []);

  return { handleError };
};
```

## Testing Strategy

### Component Testing

```typescript
// Example component test
describe('MetricCard', () => {
  const defaultProps = {
    title: 'Answer Relevancy',
    value: 85,
    threshold: 70,
    trend: { direction: 'up', value: 5 },
  };

  it('renders metric information correctly', () => {
    render(<MetricCard {...defaultProps} />);

    expect(screen.getByText('Answer Relevancy')).toBeInTheDocument();
    expect(screen.getByText('85%')).toBeInTheDocument();
    expect(screen.getByLabelText(/Status: good/i)).toBeInTheDocument();
  });

  it('is accessible via keyboard', async () => {
    const user = userEvent.setup();
    render(<MetricCard {...defaultProps} />);

    const card = screen.getByRole('region');
    card.focus();

    expect(card).toHaveFocus();

    await user.keyboard('{Enter}');
    // Verify interaction behavior
  });

  it('shows warning status when below threshold', () => {
    const props = { ...defaultProps, value: 65 };
    render(<MetricCard {...props} />);

    expect(screen.getByLabelText(/Status: warning/i)).toBeInTheDocument();
  });
});
```

### Integration Testing

```typescript
describe('Analytics Dashboard Integration', () => {
  it('loads and displays analytics data', async () => {
    const mockData = {
      answer_relevancy: 85,
      faithfulness: 92,
      contextual_relevancy: 78,
    };

    (analyticsService.getRAGTriadMetrics as jest.Mock).mockResolvedValue({
      metrics: mockData,
    });

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <AnalyticsDashboard
          timeRange={{ start: '2024-01-01', end: '2024-01-31' }}
          filters={{}}
        />
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText('85%')).toBeInTheDocument();
      expect(screen.getByText('92%')).toBeInTheDocument();
      expect(screen.getByText('78%')).toBeInTheDocument();
    });
  });
});
```

### E2E Testing with Playwright

```typescript
// playwright tests
test('analytics dashboard flow', async ({ page }) => {
  await page.goto('/analytics');

  // Wait for dashboard to load
  await page.waitForSelector('[data-testid="metrics-overview"]');

  // Verify RAG triad metrics are displayed
  await expect(page.locator('[data-testid="answer-relevancy"]')).toBeVisible();
  await expect(page.locator('[data-testid="faithfulness"]')).toBeVisible();
  await expect(page.locator('[data-testid="contextual-relevancy"]')).toBeVisible();

  // Test date range filter
  await page.click('[data-testid="date-range-picker"]');
  await page.click('[data-testid="last-30-days"]');

  // Verify data refreshes
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-testid="metrics-overview"]')).toBeVisible();

  // Test chart interactions
  await page.click('[data-testid="performance-chart"]');
  await expect(page.locator('[data-testid="chart-tooltip"]')).toBeVisible();
});

test('evaluation management workflow', async ({ page }) => {
  await page.goto('/evaluation');

  // Create new evaluation
  await page.click('[data-testid="create-evaluation"]');
  await page.fill('[data-testid="evaluation-name"]', 'Test Evaluation');
  await page.selectOption('[data-testid="evaluation-type"]', 'rag-triad');
  await page.click('[data-testid="submit-evaluation"]');

  // Verify evaluation appears in list
  await expect(page.locator('text=Test Evaluation')).toBeVisible();

  // Select and compare evaluations
  await page.check('[data-testid="evaluation-checkbox"]');
  await page.click('[data-testid="compare-selected"]');

  // Verify comparison view
  await expect(page.locator('[data-testid="comparison-view"]')).toBeVisible();
});
```

## Monitoring and Analytics

### Performance Monitoring

```typescript
// Performance monitoring hook
const usePerformanceMonitoring = () => {
  useEffect(() => {
    // Monitor Core Web Vitals
    const observer = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        switch (entry.entryType) {
          case 'navigation':
            // Track page load performance
            trackPageLoadMetrics(entry);
            break;
          case 'measure':
            // Track custom performance marks
            trackCustomMetrics(entry);
            break;
        }
      }
    });

    observer.observe({ entryTypes: ['navigation', 'measure'] });

    return () => observer.disconnect();
  }, []);
};

// Component performance tracking
const TrackedComponent: React.FC<any> = (props) => {
  useEffect(() => {
    const startTime = performance.now();

    return () => {
      const endTime = performance.now();
      trackComponentPerformance(props.name, endTime - startTime);
    };
  }, [props.name]);

  return <Component {...props} />;
};
```

### User Analytics

```typescript
// User interaction tracking
const useAnalytics = () => {
  const trackEvent = useCallback((eventName: string, properties?: Record<string, any>) => {
    // Send to analytics service
    analyticsService.track(eventName, {
      timestamp: new Date().toISOString(),
      ...properties,
    });
  }, []);

  const trackPageView = useCallback((pageName: string) => {
    trackEvent('page_view', { page: pageName });
  }, [trackEvent]);

  const trackFeatureUsage = useCallback((featureName: string, action: string) => {
    trackEvent('feature_usage', { feature: featureName, action });
  }, [trackEvent]);

  return { trackEvent, trackPageView, trackFeatureUsage };
};
```

## Deployment and Build Optimization

### Build Configuration

```json
{
  "scripts": {
    "build": "craco build",
    "build:analyze": "npm run build && npx bundle-analyzer build/static/js/*.js",
    "build:production": "npm run build && npm run test:ci && npm run lint"
  }
}
```

### Code Splitting Strategy

1. **Route-based splitting** - Lazy load page components
2. **Feature-based splitting** - Group related components
3. **Vendor splitting** - Separate third-party libraries
4. **Dynamic imports** - Load heavy components on demand

### Bundle Optimization

```javascript
// craco.config.js
module.exports = {
  webpack: {
    configure: (webpackConfig) => {
      // Bundle optimization
      webpackConfig.optimization.splitChunks = {
        chunks: 'all',
        cacheGroups: {
          vendor: {
            test: /[\\/]node_modules[\\/]/,
            name: 'vendors',
            chunks: 'all',
          },
          charts: {
            test: /[\\/]node_modules[\\/]recharts[\\/]/,
            name: 'charts',
            chunks: 'all',
          },
        },
      };

      return webpackConfig;
    },
  },
};
```

## Security Considerations

1. **Content Security Policy** - Restrict resource loading
2. **XSS Prevention** - Proper input sanitization
3. **Authentication** - Secure token management
4. **API Security** - Request validation and rate limiting
5. **Data Privacy** - Sensitive data handling

## Internationalization

```typescript
// i18n setup
const resources = {
  en: {
    translation: {
      'analytics.title': 'Analytics Dashboard',
      'metrics.answer_relevancy': 'Answer Relevancy',
      'metrics.faithfulness': 'Faithfulness',
      // ... more translations
    },
  },
  // Add other languages
};

const i18n = createI18n({
  resources,
  lng: 'en',
  fallbackLng: 'en',
  interpolation: {
    escapeValue: false,
  },
});
```

## Conclusion

This frontend architecture provides a solid foundation for the Multimodal Enterprise RAG System evaluation and analytics platform. The design emphasizes:

- **Performance** through code splitting and virtualization
- **Accessibility** through WCAG compliance
- **Maintainability** through modular architecture
- **Scalability** through proper state management
- **User Experience** through real-time updates and responsive design

The architecture supports the complex requirements of analytics dashboards, evaluation management, and system monitoring while maintaining high performance and accessibility standards.