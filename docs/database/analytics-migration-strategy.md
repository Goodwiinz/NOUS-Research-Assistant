# Analytics Dashboard Migration Strategy

> **Stale / historical (2026-10-17):** Qdrant is fully removed and live retrieval is PostgreSQL full-text search (`DO_KB_ENABLED` defaults to `False`; pgvector semantic search is flag-gated). Treat Qdrant, DO KB and vector-store statements below as historical. Current contracts: [`docs/engineering/`](../engineering/README.md).

## Overview

This document outlines the comprehensive migration strategy for implementing the Knowledge Graph Analytics Dashboard schema in the existing Multimodal Enterprise RAG System. The migration is designed to be non-disruptive, backwards-compatible, and supports gradual rollout.

## Migration Phases

### Phase 1: Foundation (Week 1-2)

#### 1.1 Schema Deployment
- **Duration**: 2-4 hours
- **Downtime**: None (schema changes are non-disruptive)
- **Risk**: Low

**Steps**:
1. Deploy migration `007_knowledge_graph_analytics_dashboard.sql`
2. Create indexes and partitions
3. Set up materialized views
4. Configure Row Level Security policies

**Validation**:
```sql
-- Verify schema creation
SELECT table_name, table_type
FROM information_schema.tables
WHERE table_name LIKE '%analytics%'
   OR table_name LIKE '%dashboard%'
   OR table_name LIKE '%widget%';

-- Verify indexes
SELECT indexname, tablename
FROM pg_indexes
WHERE tablename LIKE '%analytics%';

-- Test RLS policies
SELECT policyname, tablename, permissive
FROM pg_policies
WHERE tablename LIKE '%analytics%';
```

#### 1.2 Backfill Process Initialization
- **Duration**: Depends on data volume (typically 1-6 hours)
- **Downtime**: None
- **Risk**: Low-Medium

**Steps**:
1. Create backfill jobs for historical data
2. Initialize entity analytics aggregations
3. Process relationship analytics
4. Generate initial graph metrics

**Backfill Script**:
```sql
-- Create backfill function
CREATE OR REPLACE FUNCTION backfill_analytics_data(
    p_organization_id UUID,
    p_start_date DATE,
    p_end_date DATE
) RETURNS VOID AS $$
DECLARE
    current_date DATE := p_start_date;
BEGIN
    WHILE current_date <= p_end_date LOOP
        -- Process entity analytics for each day
        PERFORM aggregate_entity_analytics(
            p_organization_id,
            current_date::TIMESTAMPTZ,
            (current_date + INTERVAL '1 day')::TIMESTAMPTZ,
            'day'
        );

        -- Process other analytics
        PERFORM aggregate_relationship_analytics(
            p_organization_id,
            current_date::TIMESTAMPTZ,
            (current_date + INTERVAL '1 day')::TIMESTAMPTZ,
            'day'
        );

        current_date := current_date + INTERVAL '1 day';

        -- Commit every 10 days to avoid long transactions
        IF MOD(EXTRACT(DAY FROM current_date - p_start_date), 10) = 0 THEN
            COMMIT;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;
```

### Phase 2: Data Processing Integration (Week 2-3)

#### 2.1 Trigger Integration
- **Duration**: 2-3 hours
- **Downtime**: None
- **Risk**: Low

**Steps**:
1. Install analytics update triggers
2. Configure notification system for real-time updates
3. Set up background job processing
4. Test trigger performance impact

**Trigger Testing**:
```sql
-- Test trigger performance
EXPLAIN ANALYZE
INSERT INTO entities (
    organization_id, document_id, entity_type, entity_name,
    extraction_confidence, created_at
) VALUES (
    test_org_id, test_doc_id, 'person', 'Test Entity', 0.9, NOW()
);

-- Verify analytics update
SELECT * FROM entity_analytics
WHERE organization_id = test_org_id
  AND time_bucket >= NOW() - INTERVAL '1 hour';
```

#### 2.2 Background Processing Setup
- **Duration**: 4-6 hours
- **Downtime**: None
- **Risk**: Medium

**Components**:
1. **Graph Metrics Calculator**: Scheduled job for expensive computations
2. **Cache Manager**: Background cleanup and refresh
3. **Partition Manager**: Automated partition creation/cleanup
4. **Aggregation Worker**: Processes time-based aggregations

**Background Job Setup**:
```sql
-- Create scheduled jobs using pg_cron extension
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Schedule graph metrics calculation (every 6 hours)
SELECT cron.schedule(
    'calculate-graph-metrics',
    '0 */6 * * *',
    $$
    SELECT calculate_graph_metrics_for_organizations();
    $$
);

-- Schedule cache cleanup (every hour)
SELECT cron.schedule(
    'cleanup-analytics-cache',
    '0 * * * *',
    $$
    SELECT cleanup_analytics_cache();
    $$
);

-- Schedule partition creation (daily at 2 AM)
SELECT cron.schedule(
    'create-analytics-partitions',
    '0 2 * * *',
    $$
    SELECT create_analytics_partitions();
    $$
);
```

### Phase 3: API Integration (Week 3-4)

#### 3.1 API Endpoints
- **Duration**: 8-12 hours
- **Downtime**: None
- **Risk**: Low-Medium

**Required Endpoints**:

```python
# Analytics API endpoints structure
GET /api/v1/analytics/dashboard/metrics
GET /api/v1/analytics/entities/trends
GET /api/v1/analytics/relationships/analysis
GET /api/v1/analytics/graph/metrics
GET /api/v1/analytics/documents/performance
GET /api/v1/analytics/users/engagement
GET /api/v1/analytics/reports/generate
POST /api/v1/analytics/reports/schedule
GET /api/v1/analytics/widgets/definitions
POST /api/v1/analytics/dashboard/configure
```

#### 3.2 WebSocket Integration
- **Duration**: 6-8 hours
- **Downtime**: None
- **Risk**: Medium

**Real-time Updates**:
```python
# WebSocket event types
analytics_events = {
    'entity_analytics_updated',
    'relationship_analytics_updated',
    'graph_metrics_calculated',
    'dashboard_metrics_refreshed',
    'report_generation_completed',
    'alert_triggered'
}

# Event payload structure
analytics_event = {
    'event_type': 'entity_analytics_updated',
    'organization_id': 'uuid',
    'data': {
        'time_bucket': '2024-01-01T00:00:00Z',
        'total_entities': 1000,
        'new_entities': 50,
        'growth_rate': 0.05
    },
    'timestamp': '2024-01-01T12:00:00Z'
}
```

### Phase 4: Frontend Integration (Week 4-5)

#### 4.1 Dashboard Components
- **Duration**: 12-16 hours
- **Downtime**: None
- **Risk**: Low

**Component Library**:
```typescript
// React component structure
interface DashboardWidget {
  id: string;
  type: 'chart' | 'metric' | 'table' | 'heatmap';
  title: string;
  dataSource: string;
  config: WidgetConfig;
  position: WidgetPosition;
}

interface AnalyticsChartProps {
  widget: DashboardWidget;
  timeRange: TimeRange;
  filters: FilterConfig;
  onInteraction: (event: ChartEvent) => void;
}
```

#### 4.2 State Management
- **Duration**: 4-6 hours
- **Downtime**: None
- **Risk**: Low

**Redux Store Structure**:
```typescript
interface AnalyticsState {
  dashboard: {
    metrics: RealTimeMetrics;
    widgets: DashboardWidget[];
    config: DashboardConfig;
    loading: boolean;
    error: string | null;
  };
  analytics: {
    entityTrends: EntityAnalytics[];
    relationshipAnalysis: RelationshipAnalytics[];
    graphMetrics: GraphMetricsAnalytics[];
    documentPerformance: DocumentAnalytics[];
    userEngagement: UserInteractionAnalytics[];
  };
  reports: {
    customReports: CustomAnalyticsReport[];
    executions: ReportExecution[];
    schedules: ReportSchedule[];
  };
}
```

### Phase 5: Testing & Validation (Week 5-6)

#### 5.1 Performance Testing
- **Duration**: 8-12 hours
- **Downtime**: None
- **Risk**: Low

**Load Testing Scenarios**:
```sql
-- Performance test queries
-- 1. Dashboard loading (should be <500ms)
SELECT * FROM real_time_dashboard_metrics
WHERE organization_id = $1;

-- 2. Trend analysis (should be <2s)
SELECT time_bucket, total_entities, entity_growth_rate
FROM entity_analytics
WHERE organization_id = $1
  AND bucket_type = 'day'
  AND time_bucket >= NOW() - INTERVAL '30 days'
ORDER BY time_bucket DESC;

-- 3. Graph metrics (should be <30s)
SELECT * FROM graph_metrics_analytics
WHERE organization_id = $1
ORDER BY computed_at DESC
LIMIT 10;
```

#### 5.2 Data Validation
- **Duration**: 6-8 hours
- **Downtime**: None
- **Risk**: Low

**Validation Queries**:
```sql
-- Validate entity analytics consistency
SELECT
    COUNT(*) as actual_entities,
    (SELECT total_entities
     FROM entity_analytics
     WHERE organization_id = $1
       AND bucket_type = 'day'
       AND time_bucket = DATE_TRUNC('day', NOW())
    ) as reported_entities,
    ABS(COUNT(*) - (SELECT total_entities FROM entity_analytics ...)) as difference
FROM entities
WHERE organization_id = $1;

-- Validate relationship analytics
SELECT
    COUNT(*) as actual_relationships,
    (SELECT total_relationships
     FROM relationship_analytics
     WHERE organization_id = $1
       AND bucket_type = 'day'
       AND time_bucket = DATE_TRUNC('day', NOW())
    ) as reported_relationships
FROM entity_relationships
WHERE organization_id = $1;
```

## Rollout Strategy

### Canary Release (Week 6)

#### Organization Selection Criteria
1. **Small Organizations**: <1,000 documents
2. **Active Users**: Regular platform usage
3. **Technical Comfort**: Willing to test new features
4. **Good Data Quality**: Complete processing history

#### Monitoring During Canary
```sql
-- Monitor query performance
SELECT
    query,
    calls,
    total_time,
    mean_time,
    stddev_time
FROM pg_stat_statements
WHERE query LIKE '%analytics_%'
  AND calls > 0
ORDER BY total_time DESC;

-- Monitor error rates
SELECT
    error_type,
    COUNT(*) as error_count,
    MAX(created_at) as last_occurrence
FROM error_logs
WHERE component = 'analytics'
  AND created_at >= NOW() - INTERVAL '24 hours'
GROUP BY error_type;

-- Monitor cache performance
SELECT
    cache_type,
    COUNT(*) as total_entries,
    SUM(hit_count) as total_hits,
    AVG(computation_time_ms) as avg_computation_time
FROM analytics_cache
WHERE created_at >= NOW() - INTERVAL '24 hours'
GROUP BY cache_type;
```

### Gradual Rollout (Week 7-8)

#### Rollout Schedule
- **Week 7**: 10% of organizations
- **Week 8**: 25% of organizations
- **Week 9**: 50% of organizations
- **Week 10**: 100% of organizations

#### Feature Flags
```sql
-- Organization feature flags
UPDATE organizations
SET features_enabled = features_enabled || '{"analytics_dashboard": true}'
WHERE id IN (
    -- Selected organizations for rollout
    SELECT id FROM organizations
    WHERE current_document_count < 10000
    ORDER BY created_at ASC
    LIMIT 100
);
```

## Performance Optimization

### Query Optimization

#### 1. Partition Pruning
```sql
-- Good query (uses partition pruning)
SELECT * FROM entity_analytics
WHERE organization_id = $1
  AND time_bucket >= '2024-01-01'::TIMESTAMPTZ
  AND time_bucket < '2024-02-01'::TIMESTAMPTZ;

-- Bad query (scans all partitions)
SELECT * FROM entity_analytics
WHERE organization_id = $1
  AND DATE_TRUNC('month', time_bucket) = '2024-01-01';
```

#### 2. Index Utilization
```sql
-- Monitor index usage
SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch,
    pg_size_pretty(pg_relation_size(indexrelid)) as index_size
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
  AND tablename LIKE '%analytics%'
ORDER BY idx_scan DESC;
```

#### 3. Materialized View Refresh Strategy
```sql
-- Concurrent refresh for minimal downtime
REFRESH MATERIALIZED VIEW CONCURRENTLY real_time_dashboard_metrics;

-- Schedule refresh based on usage patterns
SELECT cron.schedule(
    'refresh-dashboard-metrics',
    '*/5 * * * *', -- Every 5 minutes during business hours
    $$
    SELECT refresh_real_time_dashboard_metrics();
    $$,
    TRUE -- Include seconds precision
);
```

### Caching Strategy

#### 1. Multi-Level Cache Hierarchy
```sql
-- Application cache configuration
-- Level 1: In-memory (Redis) - 5 minutes TTL
-- Level 2: Database cache table - 1 hour TTL
-- Level 3: Materialized views - 5 minutes refresh

-- Cache warming queries
WARM_CACHE_QUERIES = [
    "SELECT * FROM real_time_dashboard_metrics WHERE organization_id = ?",
    "SELECT time_bucket, total_entities FROM entity_analytics WHERE organization_id = ? AND bucket_type = 'day' AND time_bucket >= NOW() - INTERVAL '7 days'",
    "SELECT * FROM graph_metrics_analytics WHERE organization_id = ? ORDER BY computed_at DESC LIMIT 1"
];
```

#### 2. Cache Invalidation Strategy
```sql
-- Smart cache invalidation
CREATE OR REPLACE FUNCTION invalidate_relevant_cache(
    p_organization_id UUID,
    p_change_type VARCHAR(50) -- 'entity', 'relationship', 'document', 'user'
) RETURNS VOID AS $$
BEGIN
    -- Invalidate specific cache entries based on change type
    CASE p_change_type
        WHEN 'entity' THEN
            DELETE FROM analytics_cache
            WHERE organization_id = p_organization_id
              AND cache_type IN ('entity_analytics', 'entity_trends', 'graph_metrics');

        WHEN 'relationship' THEN
            DELETE FROM analytics_cache
            WHERE organization_id = p_organization_id
              AND cache_type IN ('relationship_analytics', 'graph_connectivity', 'graph_metrics');

        WHEN 'document' THEN
            DELETE FROM analytics_cache
            WHERE organization_id = p_organization_id
              AND cache_type IN ('document_analytics', 'processing_metrics');

        WHEN 'user' THEN
            DELETE FROM analytics_cache
            WHERE organization_id = p_organization_id
              AND cache_type IN ('user_analytics', 'engagement_metrics');
    END CASE;

    -- Always refresh real-time metrics
    REFRESH MATERIALIZED VIEW CONCURRENTLY real_time_dashboard_metrics;
END;
$$ LANGUAGE plpgsql;
```

## Monitoring and Alerting

### Key Performance Indicators

#### 1. Dashboard Performance
```sql
-- Dashboard loading time monitor
CREATE OR REPLACE FUNCTION monitor_dashboard_performance()
RETURNS TABLE (
    metric_name TEXT,
    current_value DECIMAL,
    threshold_value DECIMAL,
    status TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        'dashboard_load_time'::TEXT,
        AVG(total_time)::DECIMAL,
        500::DECIMAL, -- 500ms threshold
        CASE
            WHEN AVG(total_time) <= 500 THEN 'good'
            WHEN AVG(total_time) <= 1000 THEN 'warning'
            ELSE 'critical'
        END::TEXT
    FROM pg_stat_statements
    WHERE query LIKE '%real_time_dashboard_metrics%'
      AND calls > 0;
END;
$$ LANGUAGE plpgsql;
```

#### 2. Data Freshness
```sql
-- Data freshness monitoring
SELECT
    'entity_analytics_freshness' as metric_name,
    EXTRACT(EPOCH FROM (NOW() - MAX(time_bucket))) as freshness_seconds,
    3600 as threshold_seconds,
    CASE
        WHEN EXTRACT(EPOCH FROM (NOW() - MAX(time_bucket))) <= 3600 THEN 'fresh'
        WHEN EXTRACT(EPOCH FROM (NOW() - MAX(time_bucket))) <= 7200 THEN 'stale'
        ELSE 'critical'
    END as status
FROM entity_analytics
WHERE organization_id = $1;
```

### Automated Alerts

#### 1. Performance Alerts
```sql
-- Performance degradation alert
INSERT INTO analytics_alerts (
    organization_id,
    alert_name,
    alert_type,
    alert_condition,
    severity,
    notification_channels
) VALUES (
    'system-org-id',
    'Dashboard Performance Degradation',
    'metric_threshold',
    '{
        "metric": "dashboard_load_time",
        "operator": ">",
        "threshold": 1000,
        "consecutive_periods": 3
    }',
    'warning',
    '["email", "slack", "dashboard"]'
);
```

#### 2. Data Quality Alerts
```sql
-- Data consistency monitoring
CREATE OR REPLACE FUNCTION check_data_consistency()
RETURNS VOID AS $$
DECLARE
    consistency_issues INTEGER;
BEGIN
    -- Check entity analytics consistency
    SELECT COUNT(*) INTO consistency_issues
    FROM (
        SELECT
            o.id as org_id,
            COUNT(DISTINCT e.id) as actual_entities,
            COALESCE(ea.total_entities, 0) as reported_entities
        FROM organizations o
        LEFT JOIN entities e ON o.id = e.organization_id
        LEFT JOIN entity_analytics ea ON o.id = ea.organization_id
            AND ea.bucket_type = 'day'
            AND ea.time_bucket = DATE_TRUNC('day', NOW())
        GROUP BY o.id, ea.total_entities
        HAVING ABS(COUNT(DISTINCT e.id) - COALESCE(ea.total_entities, 0)) > 10
    ) inconsistencies;

    -- Create alert if inconsistencies found
    IF consistency_issues > 0 THEN
        INSERT INTO analytics_alerts (
            organization_id,
            alert_name,
            alert_type,
            alert_condition,
            severity,
            alert_details
        ) VALUES (
            (SELECT id FROM organizations WHERE slug = 'system-default'),
            'Data Consistency Issues Detected',
            'data_quality',
            '{"inconsistency_count": ' || consistency_issues || '}',
            'warning',
            '{"inconsistency_count": ' || consistency_issues || ', "detected_at": "' || NOW() || '"}'
        );
    END IF;
END;
$$ LANGUAGE plpgsql;
```

## Backup and Recovery

### Backup Strategy

#### 1. Regular Backups
```bash
# Daily backup of analytics tables
pg_dump -h localhost -U postgres -d rag_system \
    --schema=public \
    --table=entity_analytics \
    --table=relationship_analytics \
    --table=graph_metrics_analytics \
    --table=document_analytics \
    --table=user_interaction_analytics \
    --table=dashboard_configurations \
    --table=custom_analytics_reports \
    --format=custom \
    --compress=9 \
    --file=/backups/analytics_$(date +%Y%m%d).dump
```

#### 2. Point-in-Time Recovery
```sql
-- Enable point-in-time recovery for analytics
ALTER SYSTEM SET wal_level = replica;
ALTER SYSTEM SET archive_mode = on;
ALTER SYSTEM SET archive_command = 'cp %p /archive/wal/%f';

-- Create restore point before major operations
SELECT pg_create_restore_point('before_analytics_deployment');
```

### Disaster Recovery

#### 1. Partition Recovery
```sql
-- Restore specific partition if corrupted
CREATE TABLE entity_analytics_y2024m01_restored (LIKE entity_analytics_y2024m01 INCLUDING ALL);

-- Restore from backup
pg_restore -h localhost -U postgres -d rag_system \
    --table=entity_analytics_y2024m01 \
    --data-only \
    /backups/analytics_20240101.dump

-- Switch to restored partition
ALTER TABLE entity_analytics DETACH PARTITION entity_analytics_y2024m01;
ALTER TABLE entity_analytics ATTACH PARTITION entity_analytics_y2024m01_restored
    FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');
```

## Security Considerations

### Data Privacy

#### 1. Sensitive Data Handling
```sql
-- Encrypt sensitive analytics data
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Encrypt user-specific analytics
ALTER TABLE user_interaction_analytics
ADD COLUMN encrypted_user_data BYTEB;

UPDATE user_interaction_analytics
SET encrypted_user_data = pgp_sym_encrypt(
    jsonb_build_object(
        'user_id', user_id,
        'session_id', session_id,
        'detailed_metrics', detailed_metrics
    )::TEXT,
    current_setting('app.analytics_encryption_key')
);

-- Drop unencrypted columns after verification
-- ALTER TABLE user_interaction_analytics DROP COLUMN user_id;
-- ALTER TABLE user_interaction_analytics DROP COLUMN session_id;
```

#### 2. Access Control
```sql
-- Create analytics-specific roles
CREATE ROLE analytics_viewer;
GRANT SELECT ON analytics_tables TO analytics_viewer;

CREATE ROLE analytics_editor;
GRANT SELECT, INSERT, UPDATE ON analytics_tables TO analytics_editor;

CREATE ROLE analytics_admin;
GRANT ALL PRIVILEGES ON analytics_tables TO analytics_admin;

-- Row-level security for sensitive organizations
CREATE POLICY sensitive_org_policy ON entity_analytics
    FOR ALL TO analytics_viewer
    USING (
        organization_id NOT IN (
            SELECT id FROM organizations
            WHERE settings->>'sensitive_data' = 'true'
        )
    );
```

### Audit Logging

#### 1. Analytics Access Logging
```sql
-- Create audit table for analytics access
CREATE TABLE analytics_access_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    organization_id UUID REFERENCES organizations(id),
    accessed_table VARCHAR(255),
    query_type VARCHAR(100), -- SELECT, INSERT, UPDATE, DELETE
    query_parameters JSONB,
    ip_address INET,
    user_agent TEXT,
    accessed_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create trigger for audit logging
CREATE OR REPLACE FUNCTION log_analytics_access()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO analytics_access_log (
        user_id,
        organization_id,
        accessed_table,
        query_type,
        accessed_at
    ) VALUES (
        current_setting('app.current_user_id', true)::UUID,
        current_setting('app.current_organization_id', true)::UUID,
        TG_TABLE_NAME,
        TG_OP,
        NOW()
    );

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

-- Apply audit triggers
CREATE TRIGGER entity_analytics_audit_trigger
    AFTER INSERT OR UPDATE OR DELETE ON entity_analytics
    FOR EACH STATEMENT
    EXECUTE FUNCTION log_analytics_access();
```

This comprehensive migration strategy ensures a smooth, non-disruptive rollout of the Knowledge Graph Analytics Dashboard while maintaining system performance, data integrity, and security.