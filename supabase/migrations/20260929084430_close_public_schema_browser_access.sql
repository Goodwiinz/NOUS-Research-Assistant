-- Close the public schema to browser roles, including tables created later.
--
-- The browser uses Supabase Auth only: no frontend code reads public tables
-- through PostgREST, Realtime or RPC, and the backend connects as the table
-- owner (postgres) or service_role. 20260929084412 revoked a fixed table
-- list, but the postgres role's default privileges re-grant ALL on every new
-- public table and sequence to anon and authenticated. Alembic creates tables
-- as postgres without RLS, so tables added after that list (for example
-- agent_tool_receipts and search_feedback) were readable and writable with
-- the public anon key. Revoke schema-wide and fix the default.

BEGIN;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM anon, authenticated;

-- Defense in depth if a grant is ever re-added: RLS on everywhere and no
-- blanket read policy. The table owner and service_role bypass RLS.
DO $$
DECLARE
  tbl text;
BEGIN
  FOR tbl IN
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
    EXECUTE format(
      'DROP POLICY IF EXISTS "authenticated_read_only" ON public.%I',
      tbl
    );
  END LOOP;
END
$$;

COMMIT;
