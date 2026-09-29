-- Applied to the hosted project on 2026-06-30 without a repo file; recorded
-- here verbatim so the repo history matches supabase_migrations.schema_migrations.
-- Alembic revision uq_project_thread_constraint performs the same guarded repair.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'uq_project_thread'
      AND conrelid = 'public.project_threads'::regclass
  ) THEN
    DELETE FROM public.project_threads a
    USING public.project_threads b
    WHERE a.project_id = b.project_id
      AND a.thread_id = b.thread_id
      AND a.ctid < b.ctid;

    ALTER TABLE public.project_threads
      ADD CONSTRAINT uq_project_thread UNIQUE (project_id, thread_id);
  END IF;
END $$;
