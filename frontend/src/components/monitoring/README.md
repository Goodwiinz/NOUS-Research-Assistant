> **Stale (2026-10-17 review): this README was written for an earlier component set and its feature list is aspirational. The directory currently holds only `AlertList`, `MetricCard`, `PerformanceMetricsDashboard`, `StatusGrid` and `SystemOverviewDashboard`; check those files for real behavior.**

# Monitoring Dashboard Components

This directory contains a comprehensive set of React components for building enterprise-grade monitoring dashboards for the Multimodal RAG System.

## 🚀 Features

- **Real-time Monitoring**: WebSocket integration for live data updates
- **Comprehensive Metrics**: System health, performance, business, and infrastructure metrics
- **Alert Management**: Full alert lifecycle management with filtering and actions
- **Responsive Design**: Mobile-first design that works on all screen sizes
- **Accessibility**: WCAG 2.1 AA compliance with keyboard navigation and screen reader support
- **TypeScript Support**: Full TypeScript definitions for type safety
- **Performance Optimized**: Virtual scrolling, memoization, and efficient re-rendering
- **Theme Support**: Dark/light mode compatibility
- **Internationalization**: Ready for multi-language support

## 📦 Components

### Core Dashboards

#### SystemOverviewDashboard
Main system health and status dashboard providing:
- Overall system health scores
- Component health grid
- SLI/SLO metrics
- Active alerts overview
- Real-time system trends

```tsx
import { SystemOverviewDashboard } from '@/components/monitoring';

<SystemOverviewDashboard
  autoRefresh={true}
  refreshInterval={30000}
  showDetails={true}
  onSystemHealthClick={(health) => console.log(health)}
  onAlertClick={(alert) => console.log(alert)}
/>
```

#### PerformanceMetricsDashboard
Detailed performance monitoring dashboard featuring:
- Latency metrics (P50, P90, P95, P99)
- Throughput analysis
- Error rate tracking
- Resource utilization (CPU, memory, disk, network)
- Database performance metrics

```tsx
import { PerformanceMetricsDashboard } from '@/components/monitoring';

<PerformanceMetricsDashboard
  timeRange="24h"
  showDetails={true}
  onTimeRangeChange={(range) => console.log(range)}
/>
```

### Reusable Components

#### MetricCard
Flexible metric display component with:
- Multiple display formats (number, percentage, currency, duration, bytes)
- Trend indicators
- Threshold-based status colors
- Custom icons and descriptions
- Interactive click handlers

```tsx
import { MetricCard } from '@/components/monitoring';

<MetricCard
  title="Response Time"
  value={245}
  unit="ms"
  status="warning"
  format="duration"
  threshold={{ value: 500, type: 'lte' }}
  trend={{ direction: 'up', percentage: 12.5, period: '24h' }}
  icon={<ClockIcon />}
/>
```

#### StatusGrid
Component health grid displaying:
- Component status indicators
- Health scores with progress bars
- Dependency relationships
- Last check timestamps
- Interactive drill-down capabilities

```tsx
import { StatusGrid } from '@/components/monitoring';

<StatusGrid
  components={components}
  showDetails={true}
  columns={3}
  onComponentClick={(component) => console.log(component)}
/>
```

#### AlertList
Comprehensive alert management interface with:
- Alert filtering and search
- Severity-based styling
- Alert acknowledgment and resolution
- Expansion for detailed information
- Real-time updates

```tsx
import { AlertList } from '@/components/monitoring';

<AlertList
  alerts={alerts}
  showFilters={true}
  showActions={true}
  maxHeight="500px"
  onAlertClick={(alert) => console.log(alert)}
/>
```

## 🔧 Configuration

### Store Setup
The monitoring system uses Zustand for state management:

```tsx
import { useMonitoringStore } from '@/stores/monitoringStore';

// Access monitoring state
const systemHealth = useMonitoringStore(state => state.systemHealth);
const activeAlerts = useMonitoringStore(state => state.activeAlerts);

// Update monitoring state
const updateSystemHealth = useMonitoringStore(state => state.updateSystemHealth);
```

### WebSocket Integration
Real-time updates via WebSocket:

```tsx
import { useMonitoringWebSocket } from '@/services/monitoringWebsocketService';

const { client, isConnected, subscribeToSystemHealth } = useMonitoringWebSocket();

// Subscribe to real-time updates
useEffect(() => {
  if (client && isConnected) {
    subscribeToSystemHealth();
  }
}, [client, isConnected]);
```

### Time Range Management
Flexible time range selection:

```tsx
import { useMonitoringStore } from '@/stores/monitoringStore';

const setTimeRange = useMonitoringStore(state => state.setTimeRange);

// Set time range
setTimeRange({
  start: '2024-01-01T00:00:00Z',
  end: '2024-01-02T00:00:00Z',
  preset: '24h'
});
```

## 📊 Data Types

### Core Monitoring Types

```typescript
interface SystemHealthScore {
  overall: number;
  components: ComponentHealth[];
  timestamp: string;
  trend: HealthTrend;
}

interface PerformanceMetrics {
  latency: LatencyMetrics;
  throughput: ThroughputMetrics;
  error_rates: ErrorRateMetrics;
  resource_utilization: ResourceMetrics;
  cache_performance: CacheMetrics;
  database_performance: DatabaseMetrics;
  timestamp: string;
}

interface Alert {
  id: string;
  name: string;
  description: string;
  severity: 'critical' | 'warning' | 'info';
  status: 'active' | 'acknowledged' | 'resolved' | 'suppressed';
  source: string;
  triggered_at: string;
  metadata: Record<string, any>;
  labels: Record<string, string>;
  annotations: Record<string, string>;
}
```

## 🎨 Styling and Theming

### Tailwind CSS Classes
Components use Tailwind CSS with consistent design tokens:

```tsx
// Status colors
const statusColors = {
  success: 'bg-green-50 border-green-200 text-green-900',
  warning: 'bg-yellow-50 border-yellow-200 text-yellow-900',
  error: 'bg-red-50 border-red-200 text-red-900',
  info: 'bg-blue-50 border-blue-200 text-blue-900'
};

// Size variants
const sizeClasses = {
  sm: { card: 'p-4', title: 'text-sm', value: 'text-lg' },
  md: { card: 'p-6', title: 'text-lg', value: 'text-2xl' },
  lg: { card: 'p-8', title: 'text-xl', value: 'text-4xl' }
};
```

### Dark Mode Support
Components are designed to work with dark mode:

```tsx
// CSS variables for theme support
:root {
  --monitoring-bg-primary: theme('colors.white');
  --monitoring-bg-secondary: theme('colors.gray.50');
  --monitoring-text-primary: theme('colors.gray.900');
  --monitoring-text-secondary: theme('colors.gray.600');
}

[data-theme="dark"] {
  --monitoring-bg-primary: theme('colors.gray.900');
  --monitoring-bg-secondary: theme('colors.gray.800');
  --monitoring-text-primary: theme('colors.gray.100');
  --monitoring-text-secondary: theme('colors.gray.400');
}
```

## ♿ Accessibility

### Keyboard Navigation
All interactive elements support keyboard navigation:
- Tab order follows logical flow
- Enter/Space for activation
- Escape for closing modals/popovers
- Arrow keys for navigation within components

### Screen Reader Support
Comprehensive ARIA labels and semantic HTML:
- Proper heading hierarchy
- Descriptive button labels
- Live regions for dynamic content
- Status announcements for important changes

### Focus Management
Visible focus indicators and logical focus trapping:
- High contrast focus rings
- Focus restoration after interactions
- Skip links for keyboard users
- Focus trapping within modals

## 🚀 Performance Optimizations

### React Optimizations
- `React.memo` for component memoization
- `useMemo` for expensive calculations
- `useCallback` for stable function references
- Virtual scrolling for large data sets

### Data Optimizations
- Efficient data aggregation
- Incremental loading
- Background refresh
- Cached API responses

### Rendering Optimizations
- Lazy loading of dashboard sections
- Intersection Observer for viewport detection
- Debounced resize handlers
- Optimized re-render patterns

## 📱 Responsive Design

### Breakpoints
```css
/* Mobile */
@media (max-width: 768px) {
  .monitoring-dashboard {
    padding: 1rem;
  }
}

/* Tablet */
@media (min-width: 769px) and (max-width: 1024px) {
  .monitoring-dashboard {
    padding: 2rem;
  }
}

/* Desktop */
@media (min-width: 1025px) {
  .monitoring-dashboard {
    padding: 3rem;
  }
}
```

### Adaptive Layouts
- Single column on mobile
- Multi-column on tablet/desktop
- Collapsible sidebars
- Touch-friendly interaction targets

## 🧪 Testing

### Component Testing
Jest and React Testing Library setup:

```tsx
import { render, screen, fireEvent } from '@testing-library/react';
import { SystemOverviewDashboard } from '../SystemOverviewDashboard';

describe('SystemOverviewDashboard', () => {
  it('renders system health score', () => {
    render(<SystemOverviewDashboard />);
    expect(screen.getByText('System Health')).toBeInTheDocument();
  });

  it('handles component clicks', () => {
    const onComponentClick = jest.fn();
    render(<SystemOverviewDashboard onComponentClick={onComponentClick} />);

    fireEvent.click(screen.getByText('API Gateway'));
    expect(onComponentClick).toHaveBeenCalled();
  });
});
```

### Storybook Stories
Comprehensive Storybook coverage:
- Default states
- Loading/error states
- Interactive examples
- Responsive variants
- Accessibility testing

### E2E Testing
Playwright test coverage:
- User workflows
- Cross-browser testing
- Mobile interaction testing
- Performance regression testing

## 🔌 Integration Examples

### Complete Dashboard Setup
```tsx
import React from 'react';
import { SystemOverviewDashboard, PerformanceMetricsDashboard } from '@/components/monitoring';
import { useMonitoringStore } from '@/stores/monitoringStore';
import { useMonitoringWebSocket } from '@/services/monitoringWebsocketService';

const MonitoringDashboard: React.FC = () => {
  const { isConnected } = useMonitoringWebSocket();
  const { autoRefresh, toggleAutoRefresh } = useMonitoringStore();

  return (
    <div className="monitoring-dashboard">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-3xl font-bold">System Monitoring</h1>
        <div className="flex items-center space-x-4">
          <div className={`h-2 w-2 rounded-full ${isConnected ? 'bg-green-500' : 'bg-red-500'}`} />
          <span>{isConnected ? 'Connected' : 'Disconnected'}</span>
          <button onClick={toggleAutoRefresh}>
            {autoRefresh ? 'Pause' : 'Resume'} Updates
          </button>
        </div>
      </div>

      <div className="space-y-6">
        <SystemOverviewDashboard />
        <PerformanceMetricsDashboard />
      </div>
    </div>
  );
};
```

### Custom Metrics Integration
```tsx
import { MetricCard } from '@/components/monitoring';

const CustomMetrics: React.FC = () => {
  const metrics = [
    { name: 'Custom Metric 1', value: 85, unit: '%' },
    { name: 'Custom Metric 2', value: 1234, unit: 'req/s' },
    { name: 'Custom Metric 3', value: 45.6, unit: 'ms' }
  ];

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
      {metrics.map(metric => (
        <MetricCard
          key={metric.name}
          title={metric.name}
          value={metric.value}
          unit={metric.unit}
          status={metric.value > 80 ? 'success' : 'warning'}
        />
      ))}
    </div>
  );
};
```

## 📚 Best Practices

### Component Design
- Keep components focused and reusable
- Use proper TypeScript typing
- Implement proper error boundaries
- Follow React best practices

### Performance
- Use React.memo appropriately
- Implement virtual scrolling for large lists
- Optimize re-renders with proper dependencies
- Use lazy loading for heavy components

### Accessibility
- Ensure keyboard navigation
- Provide proper ARIA labels
- Test with screen readers
- Maintain good color contrast

### State Management
- Use Zustand for global state
- Keep state normalized
- Implement proper error handling
- Use optimistic updates where appropriate

## 🔄 Data Flow

```
WebSocket Service → Monitoring Store → React Components → UI Updates
        ↓                      ↓                    ↓
Real-time Updates    State Management   User Interactions
        ↓                      ↓                    ↓
Processing Queue    Action Handlers     Event Dispatchers
```

## 🛠️ Development Guidelines

### Adding New Metrics
1. Define TypeScript interfaces in `types/monitoring.ts`
2. Update store with new state and actions
3. Create or update components to display metrics
4. Add WebSocket message handlers
5. Write tests and documentation

### Component Structure
```
ComponentName/
├── ComponentName.tsx          # Main component
├── ComponentName.stories.tsx  # Storybook stories
├── ComponentName.test.tsx     # Unit tests
├── hooks/                     # Custom hooks
│   └── useComponentLogic.ts
├── utils/                     # Helper functions
│   └── formatters.ts
└── types/                     # Component-specific types
    └── index.ts
```

### Code Style
- Use TypeScript strict mode
- Follow ESLint configuration
- Write descriptive JSDoc comments
- Use meaningful variable names
- Keep functions small and focused

## 📖 Additional Resources

- [Zustand Documentation](https://github.com/pmndrs/zustand)
- [Recharts Documentation](https://recharts.org/)
- [Heroicons](https://heroicons.com/)
- [Tailwind CSS](https://tailwindcss.com/)
- [Storybook Documentation](https://storybook.js.org/)

For questions or contributions, please refer to the project's contribution guidelines.