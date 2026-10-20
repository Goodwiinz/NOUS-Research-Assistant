> **Archived 2026-10-17: one-off migration note about moving graph algorithms out of the frontend; kept as a decision record.**
> Historical record, not a live contract. Originally at `frontend/MIGRATION_GUIDE_GRAPH_ARCHITECTURE.md`; relative links and paths below were not updated.

# Migration Guide: Knowledge Graph Frontend Architecture Correction

## Overview

This migration guide addresses the critical constitutional violation where Tasks 3.1.1-3.3.5 incorrectly implemented graph algorithms in the frontend. The migration transitions the frontend to a pure data consumer model that only handles visualization and user interaction.

## Critical Architecture Issue

**❌ CONSTITUTIONAL VIOLATION DETECTED**
- **Issue**: Graph algorithms were incorrectly implemented in frontend components
- **Impact**: Frontend was performing backend processing responsibilities
- **Resolution**: Complete architectural correction to API-first design

## Migration Strategy

### Phase 1: Service Layer Migration (Days 1-2)

#### 1.1 Replace Existing Service Files

**Remove incorrect implementations:**
```bash
# Delete files that contain graph algorithm logic
rm src/services/oldGraphService.ts
rm src/services/oldEntityService.ts
rm src/services/oldAnalyticsService.ts
```

**Implement new service files:**
- ✅ `src/services/graphService.ts` - API client for graph operations
- ✅ `src/services/entityService.ts` - Entity CRUD and relationships
- ✅ `src/services/graphAnalyticsService.ts` - Analytics data consumption
- ✅ `src/services/websocketService.ts` - Real-time updates

#### 1.2 Update Service Dependencies

**Before (Incorrect):**
```typescript
// Frontend processing graph algorithms
const calculateCentrality = (nodes, edges) => {
  // Complex algorithm logic in frontend - VIOLATION!
  const centralities = {};
  // ... 50+ lines of algorithm implementation
  return centralities;
};
```

**After (Correct):**
```typescript
// API consumption only
const getCentralityMetrics = async (filters) => {
  const response = await apiClient.post('/api/v1/centrality', { filters });
  return response.data.metrics; // Backend-computed data only
};
```

### Phase 2: Component Architecture Migration (Days 3-5)

#### 2.1 Update Component Hierarchy

**Old Incorrect Structure:**
```
src/components/graph/
├── GraphProcessor.tsx           # ❌ Contains algorithms
├── CentralityCalculator.tsx    # ❌ Computes metrics
├── PathFinder.tsx              # ❌ Path algorithms
└── CommunityDetector.tsx       # ❌ Community detection
```

**New Correct Structure:**
```
src/components/graph/
├── KnowledgeGraphViewer.tsx    # ✅ Visualization only
├── EntityDetailsPanel.tsx      # ✅ Data display only
├── AnalyticsDashboard.tsx      # ✅ Metrics display only
├── GraphPerformanceOptimizer.tsx # ✅ Performance utilities
├── GraphWebSocketProvider.tsx  # ✅ Real-time updates
├── GraphAccessibilityProvider.tsx # ✅ Accessibility
└── ResponsiveGraphLayout.tsx   # ✅ Responsive design
```

#### 2.2 Component Migration Pattern

**Example: Centrality Display Component**

**Before (Incorrect):**
```typescript
const CentralityDisplay = ({ graphData }) => {
  const [centralities, setCentralities] = useState({});

  useEffect(() => {
    // ❌ Algorithm processing in frontend
    const calculated = calculateBetweennessCentrality(graphData);
    setCentralities(calculated);
  }, [graphData]);

  return <div>{/* Display calculated centralities */}</div>;
};
```

**After (Correct):**
```typescript
const CentralityDisplay = ({ filters }) => {
  const { data: centralities, isLoading } = useQuery({
    queryKey: ['centrality', filters],
    queryFn: () => analyticsService.getCentralityMetrics(filters),
  });

  if (isLoading) return <LoadingSpinner />;
  return <div>{/* Display backend-provided centralities */}</div>;
};
```

### Phase 3: State Management Migration (Days 6-7)

#### 3.1 Replace Algorithm State with API State

**Remove algorithm state:**
```typescript
// ❌ Remove these state variables
const [graphMetrics, setGraphMetrics] = useState(null);
const [communities, setCommunities] = useState([]);
const [centralities, setCentralities] = useState({});
```

**Implement API-first state:**
```typescript
// ✅ Use store for API data management
const { graphData, setGraphData, filters, setFilters } = useGraphStore();
const { data: analytics } = useQuery(['analytics', filters], () =>
  analyticsService.getAnalyticsDashboard(filters)
);
```

#### 3.2 Update State Management

**Migration Steps:**
1. Install Zustand if not present: `npm install zustand`
2. Implement `src/stores/graphStore.ts` (already created)
3. Replace all local algorithm state with store state
4. Update all components to use store selectors

### Phase 4: API Integration (Days 8-9)

#### 4.1 Backend API Endpoints

**Ensure these endpoints are available:**
- `GET /api/v1/entities/:id` - Entity details
- `POST /api/v1/layout` - Graph layout computation
- `POST /api/v1/centrality` - Centrality metrics
- `POST /api/v1/communities` - Community detection
- `POST /api/v1/pathfinding/shortest` - Path algorithms
- `GET /api/v1/analytics/dashboard` - Analytics dashboard

#### 4.2 Update API Configuration

**Environment Variables:**
```env
REACT_APP_GRAPH_SERVICE_URL=http://localhost:8003
REACT_APP_GRAPH_ANALYTICS_URL=http://localhost:8009
REACT_APP_GRAPH_VISUALIZATION_URL=http://localhost:8010
REACT_APP_GRAPH_WS_URL=ws://localhost:8010/ws/graph-updates
```

### Phase 5: Testing Migration (Days 10-11)

#### 5.1 Update Test Structure

**Remove algorithm tests:**
```bash
# Delete tests that verify algorithm implementations
rm src/components/graph/__tests__/CentralityCalculator.test.tsx
rm src/components/graph/__tests__/PathFinder.test.tsx
```

**Add API integration tests:**
```typescript
// ✅ Test API consumption
describe('Graph Service', () => {
  it('should fetch centrality metrics from backend', async () => {
    const metrics = await analyticsService.getCentralityMetrics();
    expect(metrics).toBeDefined();
    expect(Array.isArray(metrics)).toBe(true);
  });
});
```

#### 5.2 Mock API Responses

**Create comprehensive mocks:**
```typescript
// src/mocks/graphMocks.ts
export const mockGraphData = {
  nodes: [
    { id: '1', label: 'Entity 1', type: 'person', confidence: 0.9 },
    // ... more nodes
  ],
  edges: [
    { id: 'e1', source: '1', target: '2', type: 'knows', weight: 0.8 },
    // ... more edges
  ]
};

export const mockAnalyticsData = {
  centralities: [
    { nodeId: '1', degree: 5, betweenness: 0.3, closeness: 0.7 },
    // ... more metrics
  ],
  // ... other analytics
};
```

## Detailed Migration Steps

### Step 1: Audit Current Implementation

**Identify files with algorithm violations:**
```bash
# Search for algorithm implementations in frontend
grep -r "calculateCentrality\|betweenness\|shortestPath\|communityDetection" src/components/
grep -r "graph.*algorithm\|centrality.*compute" src/
```

**Expected files to modify:**
- All components in `src/components/graph/`
- Any utility files with graph processing
- State management files with algorithm logic

### Step 2: Component-by-Component Migration

#### KnowledgeGraphViewer Component
**Migration Checklist:**
- [ ] Remove local layout calculation
- [ ] Implement API call to `/api/v1/layout`
- [ ] Use backend-provided coordinates
- [ ] Remove physics simulation if not needed for interaction
- [ ] Update event handlers to use API data

#### EntityDetailsPanel Component
**Migration Checklist:**
- [ ] Remove local relationship computation
- [ ] Use `entityService.getEntityDetails()`
- [ ] Display backend-provided relationship strength
- [ ] Remove similarity calculations

#### AnalyticsDashboard Component
**Migration Checklist:**
- [ ] Remove all metric calculations
- [ ] Use `analyticsService.getAnalyticsDashboard()`
- [ ] Display backend-computed centralities
- [ ] Show community detection results from API

### Step 3: Performance Optimization Migration

**Replace client-side optimizations:**
```typescript
// ❌ Remove expensive client computations
const optimizeGraph = (graph) => {
  const clusters = detectCommunities(graph); // Remove this
  const layout = calculateLayout(graph);     // Remove this
  return { clusters, layout };
};

// ✅ Use backend optimizations
const fetchOptimizedGraph = async (filters) => {
  const data = await graphService.getGraphData(filters);
  return data; // Already optimized by backend
};
```

### Step 4: WebSocket Integration Migration

**Update WebSocket handlers:**
```typescript
// Before: Process updates locally
const handleNodeUpdate = (update) => {
  const updatedGraph = recalculateGraph(graph, update); // Remove
  setGraph(updatedGraph);
};

// After: Apply backend updates directly
const handleNodeUpdate = (update) => {
  handleWebSocketMessage(update); // Store update only
};
```

## Validation Checklist

### ✅ Architecture Compliance
- [ ] No graph algorithms in frontend code
- [ ] All computations delegated to backend APIs
- [ ] Frontend only handles visualization and interaction
- [ ] State management stores API responses only

### ✅ API Integration
- [ ] All required backend endpoints are consumed
- [ ] Error handling implemented for API failures
- [ ] Loading states for all API calls
- [ ] Caching strategy implemented

### ✅ Performance
- [ ] No heavy computations on main thread
- [ ] Virtual rendering for large graphs
- [ ] Lazy loading implemented
- [ ] Memory usage optimized

### ✅ Accessibility
- [ ] WCAG 2.1 AA compliance maintained
- [ ] Keyboard navigation works
- [ ] Screen reader support implemented
- [ ] Color contrast meets standards

### ✅ Testing
- [ ] Algorithm tests removed
- [ ] API integration tests added
- [ ] Component tests updated
- [ ] E2E tests verify API consumption

## Rollback Plan

If migration issues arise:

### Immediate Rollback
```bash
git checkout main
git checkout -b rollback-graph-migration
# Revert to previous working state
```

### Partial Rollback
1. Keep new service layer
2. Temporarily restore algorithm components with API calls
3. Gradually migrate individual components

## Post-Migration Validation

### 1. Functional Testing
- Verify all graph features work with backend APIs
- Test real-time updates via WebSocket
- Validate filter and search functionality
- Check export/import operations

### 2. Performance Testing
- Measure rendering performance with large graphs
- Monitor memory usage
- Test API response times
- Verify WebSocket update performance

### 3. Accessibility Testing
- Run automated accessibility tests
- Manual keyboard navigation testing
- Screen reader compatibility verification
- Color contrast validation

### 4. Load Testing
- Test with concurrent users
- Verify WebSocket connection handling
- Stress test API endpoints
- Monitor resource usage

## Support and Documentation

### Developer Resources
- Updated component documentation
- API integration examples
- Performance optimization guidelines
- Accessibility implementation guide

### Troubleshooting
- Common migration issues and solutions
- API debugging techniques
- Performance problem diagnosis
- Accessibility testing tools

## Success Metrics

### Technical Metrics
- ✅ Zero graph algorithms in frontend code
- ✅ <100ms API response times
- ✅ <2000ms initial graph load
- ✅ WCAG 2.1 AA compliance score >95%

### User Experience Metrics
- ✅ Smooth graph interaction
- ✅ Real-time updates working
- ✅ Responsive design on all devices
- ✅ Accessibility features functional

This migration resolves the constitutional violation by establishing a proper separation of concerns, with the frontend serving as a pure data consumer and the backend handling all graph algorithm processing.