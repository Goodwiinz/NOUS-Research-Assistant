> **Dated design (2026-10-17 review): an upfront architecture proposal that assumed Next.js 15 and other libraries. Current stack is Next.js 16, React 18 and Zustand 5 (see `frontend/package.json`). Check the code before relying on this.**

# Knowledge Graph Analytics Dashboard - Frontend Architecture Design

## Overview

This document outlines the comprehensive frontend architecture for the Knowledge Graph Analytics Dashboard feature in the Multimodal Enterprise RAG System. The design leverages the existing Next.js 15 app router structure, component libraries, and state management patterns while introducing advanced analytics capabilities.

## 1. Component Tree Hierarchy

### 1.1 Root Structure
```
/app/
├── analytics/                          # Analytics Dashboard Routes
│   ├── page.tsx                       # Main Analytics Dashboard
│   ├── layout.tsx                     # Analytics Layout
│   ├── knowledge-graph/               # Knowledge Graph Analytics
│   │   ├── page.tsx                  # KG Analytics Dashboard
│   │   ├── layout.tsx                # KG-specific Layout
│   │   ├── entities/                 # Entity Analytics
│   │   │   ├── page.tsx             # Entity Analytics Dashboard
│   │   │   └── [entityId]/           # Entity Detail Page
│   │   ├── relationships/            # Relationship Analytics
│   │   │   └── page.tsx
│   │   ├── communities/              # Community Analytics
│   │   │   └── page.tsx
│   │   └── performance/              # Performance Analytics
│   │       └── page.tsx
│   ├── reports/                       # Report Generation
│   │   ├── page.tsx                  # Report Dashboard
│   │   ├── create/                   # Create Report
│   │   ├── scheduled/                # Scheduled Reports
│   │   └── [reportId]/               # Report Details
│   └── settings/                      # Analytics Settings
│       ├── page.tsx                  # Settings Dashboard
│       ├── dashboards/               # Dashboard Configuration
│       └── notifications/            # Alert Configuration
```

### 1.2 Component Architecture
```
/src/components/analytics/
├── Dashboard/                         # Core Dashboard Components
│   ├── AnalyticsDashboard.tsx        # Main Dashboard Container
│   ├── DashboardGrid.tsx             # Responsive Grid Layout
│   ├── DashboardHeader.tsx           # Header with Controls
│   ├── KPICards.tsx                  # KPI Metrics Display
│   ├── TimeRangeSelector.tsx         # Time Range Controls
│   └── FilterPanel.tsx               # Advanced Filters
├── Widgets/                          # Reusable Dashboard Widgets
│   ├── MetricWidget/                 # Metric Display Widget
│   │   ├── MetricWidget.tsx
│   │   ├── MetricCard.tsx
│   │   └── MetricChart.tsx
│   ├── ChartWidget/                  # Chart Widget Container
│   │   ├── ChartWidget.tsx
│   │   ├── LineChart.tsx
│   │   ├── BarChart.tsx
│   │   ├── HeatMap.tsx
│   │   ├── PieChart.tsx
│   │   └── ScatterPlot.tsx
│   ├── TableWidget/                  # Data Table Widget
│   │   ├── TableWidget.tsx
│   │   ├── DataTable.tsx
│   │   └── PaginatedTable.tsx
│   └── GraphWidget/                  # Network Graph Widget
│       ├── GraphWidget.tsx
│       ├── NetworkGraph.tsx
│       └── InteractiveGraph.tsx
├── KnowledgeGraph/                   # Knowledge Graph Specific Components
│   ├── GraphAnalyticsDashboard.tsx   # Main KG Analytics Dashboard
│   ├── GraphMetricsPanel.tsx         # Graph KPIs
│   ├── CentralityAnalysis.tsx        # Centrality Metrics
│   ├── CommunityDetection.tsx        # Community Analysis
│   ├── PathAnalysis.tsx              # Path Finding Analytics
│   ├── TemporalAnalysis.tsx          # Time-based Analytics
│   ├── GraphComparison.tsx           # Comparative Analysis
│   └── GraphExport.tsx               # Export Functionality
├── Reports/                          # Report Generation Components
│   ├── ReportBuilder.tsx             # Drag & Drop Report Builder
│   ├── ReportScheduler.tsx           # Scheduling Interface
│   ├── ReportTemplates.tsx           # Template Gallery
│   ├── ExportOptions.tsx             # Export Configuration
│   └── ShareDialog.tsx               # Sharing Interface
├── RealTime/                         # Real-time Components
│   ├── WebSocketProvider.tsx         # WebSocket Context
│   ├── LiveUpdates.tsx               # Live Update Handler
│   ├── RealTimeMetrics.tsx           # Real-time KPIs
│   └── AlertSystem.tsx               # Alert Notifications
└── Common/                           # Shared Analytics Components
    ├── LoadingStates.tsx             # Loading Components
    ├── ErrorBoundaries.tsx           # Error Handling
    ├── EmptyStates.tsx               # Empty State Displays
    ├── Tooltips.tsx                  # Enhanced Tooltips
    └── Legends.tsx                   # Chart Legends
```

## 2. State Management Architecture

### 2.1 Global State Structure (Zustand)
```typescript
// /src/stores/analyticsStore.ts
interface AnalyticsState {
  // Dashboard Configuration
  dashboards: Record<string, DashboardConfig>;
  activeDashboard: string | null;
  widgetRegistry: Record<string, WidgetConfig>;

  // Data State
  metricsData: Record<string, MetricsData>;
  graphsData: KnowledgeGraphData | null;
  realTimeData: Record<string, any>;

  // UI State
  timeRange: TimeRange;
  filters: AnalyticsFilters;
  viewMode: 'desktop' | 'tablet' | 'mobile';

  // Loading and Error States
  loading: Record<string, boolean>;
  errors: Record<string, string | null>;

  // Real-time State
  wsConnected: boolean;
  subscriptions: Set<string>;
}

interface AnalyticsActions {
  // Dashboard Management
  createDashboard: (config: DashboardConfig) => void;
  updateDashboard: (id: string, config: Partial<DashboardConfig>) => void;
  deleteDashboard: (id: string) => void;
  setActiveDashboard: (id: string) => void;

  // Widget Management
  addWidget: (dashboardId: string, widget: WidgetConfig) => void;
  updateWidget: (dashboardId: string, widgetId: string, config: Partial<WidgetConfig>) => void;
  removeWidget: (dashboardId: string, widgetId: string) => void;

  // Data Management
  setMetricsData: (key: string, data: MetricsData) => void;
  setGraphsData: (data: KnowledgeGraphData) => void;
  updateRealTimeData: (key: string, data: any) => void;

  // Filter and Time Management
  setTimeRange: (range: TimeRange) => void;
  setFilters: (filters: Partial<AnalyticsFilters>) => void;
  clearFilters: () => void;

  // Real-time Management
  connectWebSocket: () => void;
  disconnectWebSocket: () => void;
  subscribe: (channel: string) => void;
  unsubscribe: (channel: string) => void;
}
```

### 2.2 Component-Level State
```typescript
// Local state patterns for component-specific data
interface ComponentStatePatterns {
  // Chart Component State
  ChartComponent: {
    data: ChartData[];
    loading: boolean;
    error: string | null;
    selectedDataPoint: DataPoint | null;
    zoomLevel: number;
    brushSelection: BrushRange | null;
  };

  // Graph Component State
  GraphComponent: {
    nodes: GraphNode[];
    edges: GraphEdge[];
    selectedNodes: Set<string>;
    layout: GraphLayout;
    viewport: ViewportConfig;
  };

  // Filter Component State
  FilterComponent: {
    activeFilters: FilterConfig[];
    availableOptions: FilterOptions[];
    tempFilters: FilterConfig[];
  };
}
```

### 2.3 Data Flow Architecture
```
Backend API → TanStack Query → Local State → Components
     ↓              ↓              ↓          ↓
WebSocket → Real-time Store → State Synchronization → UI Updates
```

## 3. Routing Configuration

### 3.1 Next.js App Router Structure
```typescript
// /app/analytics/layout.tsx
export default function AnalyticsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <AnalyticsProvider>
      <div className="analytics-layout">
        <AnalyticsNavigation />
        <main className="analytics-main">
          {children}
        </main>
        <RealTimeProvider />
      </div>
    </AnalyticsProvider>
  );
}

// /app/analytics/knowledge-graph/layout.tsx
export default function KnowledgeGraphLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <KnowledgeGraphProvider>
      <div className="kg-analytics-layout">
        <KGNavigation />
        {children}
      </div>
    </KnowledgeGraphProvider>
  );
}
```

### 3.2 Dynamic Routes and Parameters
```typescript
// /app/analytics/knowledge-graph/entities/[entityId]/page.tsx
interface EntityPageProps {
  params: { entityId: string };
  searchParams: {
    timeRange?: string;
    compareWith?: string;
    view?: 'overview' | 'relationships' | 'timeline';
  };
}

export default function EntityDetailPage({ params, searchParams }: EntityPageProps) {
  // Entity detail analytics implementation
}
```

### 3.3 Navigation Structure
```typescript
// Navigation Configuration
const analyticsNavigation = [
  {
    name: 'Dashboard',
    href: '/analytics',
    icon: 'Dashboard',
    children: [
      { name: 'Overview', href: '/analytics' },
      { name: 'Knowledge Graph', href: '/analytics/knowledge-graph' },
    ]
  },
  {
    name: 'Knowledge Graph',
    href: '/analytics/knowledge-graph',
    icon: 'Network',
    children: [
      { name: 'Entities', href: '/analytics/knowledge-graph/entities' },
      { name: 'Relationships', href: '/analytics/knowledge-graph/relationships' },
      { name: 'Communities', href: '/analytics/knowledge-graph/communities' },
      { name: 'Performance', href: '/analytics/knowledge-graph/performance' },
    ]
  },
  {
    name: 'Reports',
    href: '/analytics/reports',
    icon: 'Document',
    children: [
      { name: 'Dashboard', href: '/analytics/reports' },
      { name: 'Create', href: '/analytics/reports/create' },
      { name: 'Scheduled', href: '/analytics/reports/scheduled' },
    ]
  },
  {
    name: 'Settings',
    href: '/analytics/settings',
    icon: 'Settings',
    children: [
      { name: 'Dashboards', href: '/analytics/settings/dashboards' },
      { name: 'Notifications', href: '/analytics/settings/notifications' },
    ]
  },
];
```

## 4. Data Fetching Patterns and Caching Strategies

### 4.1 TanStack Query Configuration
```typescript
// /src/hooks/analytics/useAnalyticsData.ts
export const useAnalyticsData = (timeRange: TimeRange, filters: AnalyticsFilters) => {
  return useQuery({
    queryKey: ['analytics', timeRange, filters],
    queryFn: () => analyticsService.getAnalytics(timeRange, filters),
    staleTime: 5 * 60 * 1000, // 5 minutes
    cacheTime: 30 * 60 * 1000, // 30 minutes
    refetchInterval: 60 * 1000, // 1 minute
    refetchOnWindowFocus: false,
    retry: 3,
    retryDelay: attemptIndex => Math.min(1000 * 2 ** attemptIndex, 30000),
  });
};

// Knowledge Graph specific hook
export const useKnowledgeGraphData = (filters: GraphFilters) => {
  return useQuery({
    queryKey: ['knowledge-graph', filters],
    queryFn: () => graphService.getGraphData(filters),
    staleTime: 10 * 60 * 1000, // 10 minutes
    cacheTime: 60 * 60 * 1000, // 1 hour
    enabled: Object.keys(filters).length > 0,
  });
};
```

### 4.2 WebSocket Integration for Real-time Data
```typescript
// /src/hooks/useRealTimeAnalytics.ts
export const useRealTimeAnalytics = (channel: string) => {
  const [data, setData] = useState<RealTimeData | null>(null);
  const { isConnected, subscribe, unsubscribe } = useWebSocket();

  useEffect(() => {
    if (isConnected) {
      subscribe(channel, handleRealTimeUpdate);
      return () => unsubscribe(channel);
    }
  }, [channel, isConnected]);

  const handleRealTimeUpdate = (update: RealTimeUpdate) => {
    setData(prev => ({
      ...prev,
      ...update.data,
      lastUpdated: update.timestamp,
    }));
  };

  return { data, isConnected };
};
```

### 4.3 Optimistic Updates and Mutation Strategies
```typescript
// /src/hooks/useAnalyticsMutations.ts
export const useCreateReport = () => {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: reportService.createReport,
    onMutate: async (newReport) => {
      await queryClient.cancelQueries(['reports']);
      const previousReports = queryClient.getQueryData(['reports']);

      queryClient.setQueryData(['reports'], (old: Report[]) =>
        old ? [...old, newReport] : [newReport]
      );

      return { previousReports };
    },
    onError: (err, newReport, context) => {
      queryClient.setQueryData(['reports'], context?.previousReports);
    },
    onSettled: () => {
      queryClient.invalidateQueries(['reports']);
    },
  });
};
```

### 4.4 Data Preloading and Background Fetching
```typescript
// /src/components/analytics/AnalyticsDashboard.tsx
const AnalyticsDashboard = () => {
  const router = useRouter();
  const queryClient = useQueryClient();

  // Preload data for likely navigation targets
  useEffect(() => {
    // Preload knowledge graph data
    queryClient.prefetchQuery({
      queryKey: ['knowledge-graph', {}],
      queryFn: () => graphService.getGraphData({}),
    });

    // Preload common reports
    queryClient.prefetchQuery({
      queryKey: ['reports'],
      queryFn: () => reportService.getReports(),
    });
  }, []);
};
```

## 5. Design System Integration

### 5.1 Component Library Structure
```typescript
// /src/components/ui/analytics/ (Extended Design System)
export const AnalyticsDesignSystem = {
  // Color Palette Extensions
  colors: {
    // Semantic colors for analytics
    success: '#10B981',
    warning: '#F59E0B',
    error: '#EF4444',
    info: '#3B82F6',

    // Graph visualization colors
    graphNodes: {
      person: '#8B5CF6',
      organization: '#06B6D4',
      location: '#10B981',
      concept: '#F59E0B',
      event: '#EF4444',
    },

    // Chart colors
    charts: [
      '#3B82F6', '#8B5CF6', '#06B6D4', '#10B981',
      '#F59E0B', '#EF4444', '#EC4899', '#6366F1'
    ],
  },

  // Typography Scale
  typography: {
    chartLabel: {
      fontSize: '12px',
      fontWeight: '500',
      lineHeight: '16px',
    },
    metricValue: {
      fontSize: '32px',
      fontWeight: '700',
      lineHeight: '40px',
    },
    kpiTitle: {
      fontSize: '14px',
      fontWeight: '600',
      lineHeight: '20px',
    },
  },

  // Spacing System
  spacing: {
    dashboard: '24px',
    widget: '16px',
    chart: '12px',
  },
};
```

### 5.2 Responsive Grid System
```typescript
// /src/components/analytics/DashboardGrid.tsx
const DashboardGrid: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  return (
    <div className="grid grid-cols-12 gap-6 p-6">
      {children}
    </div>
  );
};

// Widget Grid Configuration
const widgetGridClasses = {
  full: 'col-span-12',
  half: 'col-span-12 lg:col-span-6',
  third: 'col-span-12 md:col-span-6 lg:col-span-4',
  quarter: 'col-span-12 sm:col-span-6 md:col-span-4 lg:col-span-3',
  twoThirds: 'col-span-12 md:col-span-8',
  threeQuarters: 'col-span-12 md:col-span-9',
};
```

### 5.3 Theme Customization
```typescript
// /src/styles/analytics-theme.css
:root {
  /* Analytics-specific theme variables */
  --analytics-primary: 99 102 241;
  --analytics-secondary: 139 92 246;
  --analytics-success: 16 185 129;
  --analytics-warning: 245 158 11;
  --analytics-error: 239 68 68;

  /* Chart colors */
  --chart-color-1: 59 130 246;
  --chart-color-2: 139 92 246;
  --chart-color-3: 6 182 212;
  --chart-color-4: 16 185 129;
  --chart-color-5: 245 158 11;

  /* Graph visualization */
  --graph-node-size-multiplier: 1;
  --graph-edge-width-multiplier: 1;
  --graph-label-size-multiplier: 1;
}

[data-theme="dark"] {
  --analytics-primary: 129 140 248;
  --analytics-secondary: 167 139 250;
  /* Dark theme adjustments */
}
```

## 6. Accessibility Requirements and WCAG 2.1 AA Compliance

### 6.1 Accessibility Checklist

#### Visual Requirements
- **Color Contrast**: All text meets 4.5:1 contrast ratio for normal text, 3:1 for large text
- **Focus Indicators**: Visible focus indicators on all interactive elements (2px solid, high contrast)
- **Color Independence**: Information not conveyed by color alone; patterns and textures used in charts
- **Text Scaling**: Layout supports 200% zoom without loss of functionality
- **Responsive Text**: Text reflows properly on different screen sizes

#### Keyboard Navigation
- **Tab Order**: Logical tab sequence through all interactive elements
- **Skip Links**: Skip to main content and skip navigation links available
- **Keyboard Traps**: No focus traps in modal dialogs without escape mechanisms
- **Shortcuts**: Keyboard shortcuts for common actions (Ctrl+S for save, etc.)

#### Screen Reader Support
- **ARIA Labels**: Comprehensive ARIA labeling for complex widgets
- **Live Regions**: ARIA live regions for dynamic content updates
- **Table Headers**: Proper table headers and associations for data tables
- **Chart Accessibility**: Alternative text and data tables for all charts

#### Cognitive Accessibility
- **Clear Language**: Simple, clear language with consistent terminology
- **Error Prevention**: Clear error messages and confirmation for destructive actions
- **Help Documentation**: Context-sensitive help available throughout
- **Consistent Layout**: Predictable navigation and interaction patterns

### 6.2 Implementation Examples

#### Accessible Chart Component
```typescript
// /src/components/analytics/charts/AccessibleChart.tsx
const AccessibleChart: React.FC<ChartProps> = ({ data, type, ...props }) => {
  const chartRef = useRef<HTMLDivElement>(null);
  const [selectedDataPoint, setSelectedDataPoint] = useState<DataPoint | null>(null);

  return (
    <div
      ref={chartRef}
      role="img"
      aria-label={`${type} chart showing ${data.length} data points`}
      aria-describedby="chart-description"
    >
      <div id="chart-description" className="sr-only">
        {generateChartDescription(data, type)}
      </div>

      <Chart
        data={data}
        type={type}
        accessibility={{
          enableAria: true,
          keyboardNavigation: true,
          focusableElements: true,
          ariaLabel: (point) => `${point.label}: ${point.value}`,
        }}
        {...props}
      />

      {/* Alternative data table for screen readers */}
      <div className="sr-only" role="table" aria-label="Chart data table">
        {renderDataTable(data)}
      </div>

      {/* Keyboard navigation hints */}
      <div className="text-sm text-gray-600 mt-2">
        Use arrow keys to navigate chart data points, Enter to select
      </div>
    </div>
  );
};
```

#### Accessible Filter Interface
```typescript
// /src/components/analytics/filters/AccessibleFilters.tsx
const AccessibleFilters: React.FC = () => {
  return (
    <section aria-label="Analytics filters">
      <h2 className="sr-only">Filter analytics data</h2>

      <form role="search" aria-label="Filter form">
        <fieldset>
          <legend className="font-semibold mb-2">Time Range</legend>
          <select
            aria-label="Select time range"
            aria-describedby="time-range-help"
          >
            <option>Last 24 hours</option>
            <option>Last 7 days</option>
            <option>Last 30 days</option>
          </select>
          <div id="time-range-help" className="sr-only">
            Select the time period for analytics data
          </div>
        </fieldset>

        <fieldset>
          <legend className="font-semibold mb-2">Entity Types</legend>
          {entityTypes.map(type => (
            <div key={type.id} className="flex items-center">
              <input
                type="checkbox"
                id={`entity-${type.id}`}
                aria-describedby={`entity-${type.id}-description`}
              />
              <label htmlFor={`entity-${type.id}`} className="ml-2">
                {type.name}
              </label>
              <div id={`entity-${type.id}-description`} className="sr-only">
                Include {type.name} entities in analysis
              </div>
            </div>
          ))}
        </fieldset>

        <button
          type="submit"
          aria-label="Apply filters to update analytics data"
        >
          Apply Filters
        </button>
      </form>
    </section>
  );
};
```

### 6.3 Testing Strategy
```typescript
// /src/__tests__/analytics/accessibility.test.tsx
describe('Analytics Dashboard Accessibility', () => {
  test('Charts have proper ARIA labels', () => {
    render(<AnalyticsDashboard />);
    const charts = screen.getAllByRole('img');
    charts.forEach(chart => {
      expect(chart).toHaveAttribute('aria-label');
    });
  });

  test('Keyboard navigation works', async () => {
    const user = userEvent.setup();
    render(<AnalyticsDashboard />);

    await user.tab();
    expect(screen.getByRole('button', { name: /filter/i })).toHaveFocus();

    await user.keyboard('{Enter}');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  test('Color contrast meets WCAG standards', async () => {
    const { container } = render(<AnalyticsDashboard />);
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});
```

## 7. Mobile-First Responsive Design Strategy

### 7.1 Breakpoint System
```css
/* /src/styles/responsive.css */
:root {
  /* Consistent breakpoint system */
  --breakpoint-xs: 0px;      /* Small phones */
  --breakpoint-sm: 640px;    /* Large phones */
  --breakpoint-md: 768px;    /* Tablets */
  --breakpoint-lg: 1024px;   /* Small desktops */
  --breakpoint-xl: 1280px;   /* Desktops */
  --breakpoint-2xl: 1536px;  /* Large desktops */
}

/* Mobile-first approach */
.analytics-dashboard {
  /* Base mobile styles */
  padding: 1rem;
  gap: 1rem;
}

@media (min-width: 768px) {
  .analytics-dashboard {
    padding: 1.5rem;
    gap: 1.5rem;
  }
}

@media (min-width: 1024px) {
  .analytics-dashboard {
    padding: 2rem;
    gap: 2rem;
  }
}
```

### 7.2 Responsive Component Patterns

#### Mobile-Optimized Dashboard Grid
```typescript
// /src/components/analytics/ResponsiveDashboard.tsx
const ResponsiveDashboard: React.FC = () => {
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid');
  const isMobile = useMediaQuery('(max-width: 768px)');

  return (
    <div className="responsive-dashboard">
      {/* Mobile-specific header */}
      {isMobile && (
        <div className="mobile-header">
          <button
            className="menu-toggle"
            aria-label="Toggle navigation"
            onClick={() => setMenuOpen(!menuOpen)}
          >
            <MenuIcon />
          </button>
          <h1>Analytics</h1>
          <button
            className="view-toggle"
            aria-label={`Switch to ${viewMode === 'grid' ? 'list' : 'grid'} view`}
            onClick={() => setViewMode(viewMode === 'grid' ? 'list' : 'grid')}
          >
            {viewMode === 'grid' ? <ListIcon /> : <GridIcon />}
          </button>
        </div>
      )}

      {/* Responsive grid layout */}
      <div className="responsive-grid">
        <div className="grid-item span-12">
          <KPICard mobile={isMobile} />
        </div>

        {/* Stack charts on mobile, side-by-side on desktop */}
        <div className={`grid-item ${isMobile ? 'span-12' : 'span-6'}`}>
          <ChartWidget mobile={isMobile} />
        </div>

        <div className={`grid-item ${isMobile ? 'span-12' : 'span-6'}`}>
          <TableWidget mobile={isMobile} />
        </div>
      </div>
    </div>
  );
};
```

#### Touch-Optimized Interactions
```typescript
// /src/components/analytics/TouchOptimizedChart.tsx
const TouchOptimizedChart: React.FC = ({ data }) => {
  const [isTouch, setIsTouch] = useState(false);
  const chartRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setIsTouch('ontouchstart' in window);
  }, []);

  const handleTouchStart = (event: TouchEvent) => {
    // Handle touch interactions for mobile
    const touch = event.touches[0];
    handleInteraction(touch.clientX, touch.clientY);
  };

  return (
    <div
      ref={chartRef}
      className={`touch-chart ${isTouch ? 'touch-enabled' : 'mouse-enabled'}`}
      onTouchStart={handleTouchStart}
      style={{
        // Larger touch targets on mobile
        '--touch-target-size': isTouch ? '44px' : 'auto',
        '--chart-font-size': isTouch ? '14px' : '12px',
      } as React.CSSProperties}
    >
      <ResponsiveContainer width="100%" height={isTouch ? 300 : 400}>
        <Chart data={data} />
      </ResponsiveContainer>
    </div>
  );
};
```

### 7.3 Performance Optimization for Mobile
```typescript
// /src/hooks/useMobileOptimization.ts
export const useMobileOptimization = () => {
  const [isLowEndDevice, setIsLowEndDevice] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);

  useEffect(() => {
    // Detect device capabilities
    const connection = (navigator as any).connection;
    const isSlowConnection = connection?.effectiveType === 'slow-2g' ||
                            connection?.effectiveType === '2g';

    const hardwareConcurrency = navigator.hardwareConcurrency || 4;
    const isLowCPU = hardwareConcurrency <= 2;

    setIsLowEndDevice(isSlowConnection || isLowCPU);
    setReducedMotion(window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }, []);

  return {
    isLowEndDevice,
    reducedMotion,
    chartResolution: isLowEndDevice ? 'low' : 'high',
    animationDuration: reducedMotion ? 0 : 300,
    maxDataPoints: isLowEndDevice ? 100 : 1000,
  };
};
```

### 7.4 Progressive Enhancement Strategy
```typescript
// /src/components/analytics/ProgressiveChart.tsx
const ProgressiveChart: React.FC = ({ data, type }) => {
  const [supportsAdvancedFeatures, setSupportsAdvancedFeatures] = useState(false);

  useEffect(() => {
    // Detect advanced feature support
    const hasWebGL = checkWebGLSupport();
    const hasGoodPerformance = checkPerformanceCapability();
    setSupportsAdvancedFeatures(hasWebGL && hasGoodPerformance);
  }, []);

  if (!supportsAdvancedFeatures) {
    // Fallback to simple chart
    return <SimpleChart data={data} type={type} />;
  }

  return (
    <AdvancedChart
      data={data}
      type={type}
      features={{
        animations: true,
        interactions: true,
        realTimeUpdates: true,
      }}
    />
  );
};
```

## 8. Storybook Documentation Plan

### 8.1 Storybook Configuration
```javascript
// /.storybook/main.js
module.exports = {
  stories: [
    '../src/components/analytics/**/*.stories.@(js|jsx|ts|tsx|mdx)',
  ],
  addons: [
    '@storybook/addon-essentials',
    '@storybook/addon-a11y',
    '@storybook/addon-backgrounds',
    '@storybook/addon-viewport',
    '@storybook/addon-controls',
    '@storybook/addon-docs',
  ],
  framework: {
    name: '@storybook/nextjs',
    options: {},
  },
};

// /.storybook/preview.js
export const decorators = [
  (Story) => (
    <div className="p-4 bg-gray-50">
      <Story />
    </div>
  ),
];

export const parameters = {
  layout: 'centered',
  backgrounds: {
    default: 'light',
    values: [
      { name: 'light', value: '#ffffff' },
      { name: 'dark', value: '#1a1a1a' },
    ],
  },
  viewport: {
    viewports: {
      mobile: { name: 'Mobile', styles: { width: '375px', height: '667px' } },
      tablet: { name: 'Tablet', styles: { width: '768px', height: '1024px' } },
      desktop: { name: 'Desktop', styles: { width: '1024px', height: '768px' } },
    },
  },
};
```

### 8.2 Component Stories Structure
```typescript
// /src/components/analytics/charts/MetricCard.stories.tsx
import type { Meta, StoryObj } from '@storybook/react';
import { MetricCard } from './MetricCard';

const meta: Meta<typeof MetricCard> = {
  title: 'Analytics/MetricCard',
  component: MetricCard,
  parameters: {
    layout: 'centered',
    docs: {
      description: {
        component: 'Metric cards display key performance indicators with trends and visual indicators.',
      },
    },
  },
  tags: ['autodocs'],
  argTypes: {
    title: {
      control: 'text',
      description: 'The metric title displayed above the value',
    },
    value: {
      control: 'number',
      description: 'The primary metric value',
    },
    unit: {
      control: 'text',
      description: 'Unit of measurement (e.g., "%", "ms", "count")',
    },
    trend: {
      control: 'select',
      options: ['up', 'down', 'stable'],
      description: 'Trend direction compared to previous period',
    },
    status: {
      control: 'select',
      options: ['success', 'warning', 'error', 'info'],
      description: 'Visual status indicator',
    },
    loading: {
      control: 'boolean',
      description: 'Show loading state',
    },
  },
};

export default meta;
type Story = StoryObj<typeof meta>;

// Primary story
export const Default: Story = {
  args: {
    title: 'Response Time',
    value: 245,
    unit: 'ms',
    trend: 'down',
    trendValue: '12%',
    status: 'success',
    loading: false,
  },
};

// Variations
export const WithTrendUp: Story = {
  args: {
    ...Default.args,
    title: 'Error Rate',
    value: 2.4,
    unit: '%',
    trend: 'up',
    trendValue: '0.5%',
    status: 'warning',
  },
};

export const Loading: Story = {
  args: {
    ...Default.args,
    loading: true,
  },
};

export const NoTrend: Story = {
  args: {
    ...Default.args,
    trend: undefined,
    trendValue: undefined,
  },
};

// Responsive stories
export const Mobile: Story = {
  args: Default.args,
  parameters: {
    viewport: {
      defaultViewport: 'mobile',
    },
  },
};

// Interactive story with controls
export const Interactive: Story = {
  args: Default.args,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(canvas.getByRole('button', { name: /details/i }));
  },
};
```

### 8.3 Complex Component Documentation
```typescript
// /src/components/analytics/dashboard/AnalyticsDashboard.stories.tsx
import type { Meta, StoryObj } from '@storybook/react';
import { within, userEvent } from '@storybook/test';
import { AnalyticsDashboard } from './AnalyticsDashboard';

const meta: Meta<typeof AnalyticsDashboard> = {
  title: 'Analytics/Dashboard',
  component: AnalyticsDashboard,
  parameters: {
    layout: 'fullscreen',
    docs: {
      description: {
        component: `
The main analytics dashboard that displays various widgets including charts, metrics, and knowledge graph visualizations.
Features real-time updates, responsive design, and comprehensive accessibility support.

## Features
- Real-time data updates via WebSocket
- Responsive grid layout
- Accessible charts and widgets
- Mobile-optimized interactions
- Customizable widget layout
- Export functionality
        `,
      },
    },
  },
  decorators: [
    (Story) => (
      <div style={{ height: '100vh' }}>
        <Story />
      </div>
    ),
  ],
};

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {
  args: {
    timeRange: '7d',
    refreshInterval: 60000,
    showRealTimeUpdates: true,
  },
};

export const MobileView: Story = {
  args: Default.args,
  parameters: {
    viewport: {
      defaultViewport: 'mobile',
    },
  },
};

export const WithCustomLayout: Story = {
  args: {
    ...Default.args,
    layout: {
      widgets: [
        { id: 'kpi-1', type: 'metric', position: { x: 0, y: 0, w: 6, h: 2 } },
        { id: 'chart-1', type: 'line', position: { x: 6, y: 0, w: 6, h: 4 } },
        { id: 'graph-1', type: 'network', position: { x: 0, y: 2, w: 12, h: 6 } },
      ],
    },
  },
};

// Interaction testing
export const InteractiveDemo: Story = {
  args: Default.args,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // Test time range selector
    await userEvent.click(canvas.getByLabelText('Select time range'));
    await userEvent.click(canvas.getByText('Last 30 days'));

    // Test widget interactions
    await userEvent.hover(canvas.getByTestId('metric-card-response-time'));
    await expect(canvas.getByText('Average response time over time')).toBeInTheDocument();

    // Test filter panel
    await userEvent.click(canvas.getByRole('button', { name: /filters/i }));
    await userEvent.type(canvas.getByLabelText('Search entities'), 'test');
  },
};
```

### 8.4 Documentation Structure
```
.storybook/
├── main.js                          # Main Storybook configuration
├── preview.js                       # Global decorators and parameters
└── docs/                           # Auto-generated documentation
    ├── intro.md                    # Introduction to analytics components
    ├── design-system.md            # Design system integration
    ├── accessibility.md            # Accessibility guidelines
    ├── performance.md              # Performance considerations
    └── migration-guide.md          # Migration guide for existing components

src/components/analytics/
├── **/*.stories.tsx                # Component stories
├── **/*.stories.mdx               # Complex documentation with MDX
└── __docs__/                      # Additional documentation files
    ├── getting-started.mdx
    ├── best-practices.mdx
    └── troubleshooting.mdx
```

This comprehensive frontend architecture design provides:

1. **Clear component hierarchy** with separation of concerns
2. **Scalable state management** using Zustand with TanStack Query
3. **Flexible routing** structure supporting dynamic parameters
4. **Efficient data fetching** with caching and real-time updates
5. **Consistent design system** integration with accessibility focus
6. **Mobile-first responsive design** with performance optimization
7. **Comprehensive Storybook documentation** for component library

The architecture is designed to be maintainable, scalable, and accessible while providing excellent performance across all device types.