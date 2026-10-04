-- Migration: Secure server-only LangGraph agent-state tables
-- Date: 2026-06-20
-- Purpose: Fix Supabase linter ERROR rls_disabled_in_public on 11 tables that
--          were created AFTER the blanket 20260304232600_enable_rls_all_tables
--          migration (LangGraph checkpointer/store + agent_hitl_audit). They
--          had RLS disabled AND live anon/authenticated PostgREST grants, i.e.
--          anyone with the public anon key could read/write all agent
--          conversation state + long-term memory via /rest/v1/*.
--
-- These tables are INTERNAL: written/read by the backend over the direct
-- Postgres connection as `postgres` (BYPASSRLS + table owner), never via the
-- PostgREST data API. Unlike user-facing tables, they get NO permissive
-- authenticated policy — enabling RLS with no policy denies both anon and
-- authenticated, and we revoke the PostgREST grants outright. The backend is
-- unaffected (postgres bypasses RLS).

BEGIN;

-- ============================================================================
-- 1. ENABLE ROW LEVEL SECURITY (no policies ⇒ PostgREST anon/authenticated denied)
-- ============================================================================
ALTER TABLE public.checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.checkpoint_blobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.checkpoint_writes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.checkpoint_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.store ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.store_vectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.store_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.vector_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_hitl_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.do_kb_backfill_progress ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- 2. REVOKE PostgREST GRANTS (defense-in-depth; backend uses the postgres role)
-- ============================================================================
REVOKE ALL ON TABLE
  public.checkpoints,
  public.checkpoint_blobs,
  public.checkpoint_writes,
  public.checkpoint_migrations,
  public.store,
  public.store_vectors,
  public.store_migrations,
  public.vector_migrations,
  public.project_memories,
  public.agent_hitl_audit,
  public.do_kb_backfill_progress
FROM anon, authenticated;

-- ============================================================================
-- 3. REVOKE direct RPC on the auth signup trigger function
-- ============================================================================
-- handle_new_user() is the SECURITY DEFINER trigger that provisions a
-- public.users row on auth.users insert. It must not be directly callable via
-- /rest/v1/rpc/handle_new_user. Functions grant EXECUTE to PUBLIC by default,
-- so anon/authenticated inherit it regardless of role-specific revokes — revoke
-- from PUBLIC (and the named roles) to actually block direct invocation.
REVOKE EXECUTE ON FUNCTION public.handle_new_user() FROM PUBLIC, anon, authenticated;

COMMIT;
