-- Applied to the hosted project on 2026-06-20 without a repo file; recorded
-- here verbatim so the repo history matches supabase_migrations.schema_migrations.

-- Functions grant EXECUTE to PUBLIC by default, so anon/authenticated inherit
-- it regardless of role-specific revokes. Revoke from PUBLIC to actually block
-- direct /rpc invocation of the SECURITY DEFINER signup trigger.
REVOKE EXECUTE ON FUNCTION public.handle_new_user() FROM PUBLIC;
