-- Deny PostgREST access to backend-owned product tables.
--
-- The backend talks to Postgres directly (or through service_role), while the
-- browser uses Supabase Auth only.  The older blanket RLS migration granted
-- authenticated SELECT on these tables for Realtime compatibility.  Product
-- data must not be available through the browser data API, so remove that
-- policy and the role grants.  Keep this migration safe for Supabase-only
-- resets where an Alembic-managed table may not exist yet.

BEGIN;

DO $$
DECLARE
  tbl text;
  tables text[] := ARRAY[
    'ab_assignments', 'ab_experiment_metrics', 'ab_experiment_segments',
    'ab_experiments', 'ab_query_routing', 'ab_user_segment_memberships',
    'ab_user_segments', 'ab_variants', 'alembic_version', 'analytics_events',
    'api_key_usage_log', 'api_keys', 'audit_events', 'chat_messages',
    'citation_relationships', 'citations', 'collection_documents',
    'collections', 'compliance_reports', 'connection_events', 'conversations',
    'data_retention_policies', 'document_access_log',
    'document_processing_stages', 'document_quality_metrics',
    'document_versions', 'documents', 'draft_citations',
    'encrypted_organization_profiles', 'encrypted_user_profiles',
    'encryption_audit_logs', 'entities', 'entity_relationships',
    'evaluation_comparisons', 'evaluation_datasets', 'evaluation_jobs',
    'evaluation_metrics', 'evaluation_reports', 'evaluation_thresholds',
    'extraction_cells', 'extraction_matrices', 'generated_drafts',
    'integrity_scores', 'message_attachments', 'metric_aggregations',
    'multimodal_content', 'notification_templates', 'organizations',
    'performance_logs', 'permissions', 'processing_history',
    'processing_jobs', 'project_notes', 'project_threads', 'quality_alerts',
    'quality_metrics', 'quality_thresholds', 'realtime_performance_metrics',
    'realtime_status_updates', 'research_blueprints', 'research_evidence',
    'research_pipelines', 'research_projects', 'research_runs',
    'research_sources', 'research_steps', 'role_permissions', 'roles',
    'search_events', 'search_queries', 'search_results', 'search_sessions',
    'security_incidents', 'stance_classifications', 'status_updates',
    'system_metrics', 'system_status_broadcasts', 'threads',
    'user_realtime_subscriptions', 'user_role_assignments', 'user_sessions',
    'users', 'websocket_connections', 'workspace_members', 'workspaces'
  ];
BEGIN
  FOREACH tbl IN ARRAY tables LOOP
    IF to_regclass(format('public.%I', tbl)) IS NOT NULL THEN
      EXECUTE format(
        'ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',
        tbl
      );
      EXECUTE format(
        'DROP POLICY IF EXISTS "authenticated_read_only" ON public.%I',
        tbl
      );
      EXECUTE format(
        'REVOKE ALL ON TABLE public.%I FROM anon, authenticated',
        tbl
      );
    END IF;
  END LOOP;
END
$$;

COMMIT;
